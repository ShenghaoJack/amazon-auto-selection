"""浏览器自动化封装 (4.1 登录态与数据获取技术方案)。

- 持久浏览器 Profile(独立用户数据目录), 登录态由 Profile 会话 Cookie 提供
- 无头/有头可切换; 页面滚动触发懒加载; 真实鼠标事件点击(isTrusted)
- 下载监听(过滤 .crdownload/.tmp); 截图存档
- 登录检查: 亚马逊 Cookie 有效性 / 卖家精灵自动登录(表单填写)+手动兜底
- Playwright 延迟导入: 未安装 playwright 时仅在使用浏览器步骤报错, 不影响 dry-run
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .config import Config
from .errors import BrowserError
from .mock import MockSellersprite

_crdownload_suffixes = (".crdownload", ".tmp")


# ---- CDP Chrome 自动启动(2026-09): 断点/中断后再运行, 若调试 Chrome 未启动则自动拉起 ----

def _cdp_alive(cdp_url: str, timeout: float = 2.0) -> bool:
    """探测 CDP 地址是否可连接(HTTP GET {url}/json/version 返回 200)。"""
    if not cdp_url:
        return False
    import urllib.request

    probe = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def cdp_port_from_url(cdp_url: str) -> int:
    try:
        from urllib.parse import urlparse

        p = urlparse(cdp_url)
        if p.port:
            return p.port
    except Exception:
        pass
    return 9222


def find_chrome_path() -> str:
    """定位 Chrome 可执行文件(环境变量 CHROME_PATH 优先, 常见安装位次之, PATH 兜底)。"""
    env = os.environ.get("CHROME_PATH", "")
    if env and os.path.exists(env):
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "chrome"  # PATH 兜底


def effective_ss_profile(config) -> str:
    """卖家精灵实际使用的 profile 目录(**按类目隔离**, 与 `Paths.profile_ss()` 同源)。

    为什么这里也必须隔离(2026-09-15, 用户"会给不同类目配不同账号"):
      自动拉起 Chrome 时用的是这个目录(--user-data-dir)。若仍用配置里那个**共享**的
      `data/profiles/ss-profile`, 那么第二个类目(另一个账号)想启动自己的 Chrome 时,
      Chrome 会因为**同一个 user-data-dir 已被占用**而拒绝启动; 更糟的是即使连上了,
      一个 profile 也只保存一份卖家精灵登录态 —— 两个账号会互相顶掉。
      按类目隔离后: 不同端口 + 各自 profile = 两个 Chrome 各自登录一个账号, 可同时跑。
    取不到 Paths(工具脚本传的简化配置)时退回配置值。
    """
    try:
        from .paths import Paths

        return str(Paths(config).profile_ss())
    except Exception:  # noqa: BLE001
        return str(config.get("SS_PROFILE", "") or "").strip() or "data/profiles/ss-profile"


def ensure_download_permissions(profile_dir: str, origins: tuple[str, ...] = (
    "https://sellersprite.com:443,*",
    "[*.]sellersprite.com:443,*",
)) -> bool:
    """预置 Chrome 的「自动下载多个文件」权限（写 profile 的 Preferences）。

    为什么需要（2026-11-03 实测）: 卖家精灵「导出」一次会触发多个下载，
    Chrome 默认拦截并提示「需有下载权限」→ 自动化环境无法点"允许" →
    下载落成 **0 字节文件** → 步骤11/12/13 合并出空表。

    ⚠ 必须在 **Chrome 启动前** 调用：运行中的 Chrome 会在退出时用内存状态
    覆盖 Preferences；启动前写入才会生效。
    """
    import json as _json
    try:
        default = os.path.join(profile_dir, "Default")
        os.makedirs(default, exist_ok=True)
        pref = os.path.join(default, "Preferences")
        data: dict = {}
        if os.path.exists(pref):
            try:
                with open(pref, encoding="utf-8") as fh:
                    data = _json.load(fh) or {}
            except Exception:  # noqa: BLE001
                data = {}
        exceptions = (data.setdefault("profile", {})
                      .setdefault("content_settings", {})
                      .setdefault("exceptions", {}))
        auto = exceptions.setdefault("automatic_downloads", {})
        changed = False
        for origin in origins:
            if auto.get(origin) != {"setting": 1}:
                auto[origin] = {"setting": 1}
                changed = True
        if changed:
            tmp = pref + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, pref)
        return changed
    except Exception:  # noqa: BLE001
        return False


def launch_ss_chrome(config, port: int, url: str = "https://www.sellersprite.com/v2/welcome", *, managed: bool = False) -> bool:
    """启动卖家精灵后台 Chrome(持久 Profile + 远程调试端口), Popen 后台拉起。

    返回 True=已发出启动命令; 端口是否真正就绪由 ensure_ss_chrome 轮询确认
    (若同一 Profile 已被其它 Chrome 占用会启动失败, 轮询超时返回 False)。
    profile 取 `effective_ss_profile(config)`(**按类目隔离**), 因此"每个类目一个
    端口 + 一个账号"可以各自起一个 Chrome 同时登录。
    """
    try:
        profile = effective_ss_profile(config)
        # 启动前预置"自动下载多个文件"权限，避免导出被 Chrome 拦截成 0 字节
        ensure_download_permissions(profile)
        chrome = find_chrome_path()
        cmd = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={os.path.abspath(profile)}",
            "--no-first-run",
            url,
        ]
        process = subprocess.Popen(cmd)
        if managed:
            from .ss_auto_close import record_auto_launch
            record_auto_launch(config, port, profile, process.pid)
        return True
    except Exception:
        return False


def ensure_ss_chrome(config, log=None, timeout: float = 60.0) -> bool:
    """确保卖家精灵 CDP Chrome 已启动: 探测 SS_CDP_URL, 未启动则自动拉起并等端口就绪。

    未配置 SS_CDP_URL(有头兜底模式)无需外部 Chrome, 直接返回 True。
    返回 True=CDP 可用; False=自动启动失败或等待超时(调用方应明确报错, 不再静默崩溃)。
    log: 可选单参回调(打印启动/就绪进度), 无则写入 pipeline 日志(无 handler 时回退 print)。
    """
    cdp_url = str(config.get("SS_CDP_URL", "") or "").strip()
    if not cdp_url:
        return True

    user = str(os.environ.get("PIPELINE_WORKBENCH_USER") or "").strip() or "default"
    port = cdp_port_from_url(cdp_url)
    tag = f"用户={user}, 端口={port}, 地址={cdp_url}"

    def _log(msg: str) -> None:
        try:
            if log:
                log(msg)
            else:
                import logging
                lg = logging.getLogger("pipeline")
                if lg.handlers:
                    lg.info(msg)
                else:
                    print(f"[ss-chrome] {msg}", flush=True)
        except Exception:
            pass

    if _cdp_alive(cdp_url):
        _log(f"[卖家精灵CDP] 复用已启动浏览器 ({tag})")
        return True
    _log(f"[卖家精灵CDP] 自动开启浏览器 ({tag})…")
    if not launch_ss_chrome(config, port, managed=True):
        _log(f"[卖家精灵CDP] 自动开启浏览器失败 ({tag}，请手动运行 python scripts/start_ss_chrome.py 排查)")
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cdp_alive(cdp_url):
            _log(f"[卖家精灵CDP] 浏览器已就绪 ({tag})")
            return True
        time.sleep(1.0)
    _log(f"[卖家精灵CDP] 等待浏览器就绪超时({int(timeout)}s) ({tag}) —— 可能同 Profile 已被其它 Chrome 占用")
    return False


def parse_cookie_header(cookie_header: str, domain: str) -> list[dict]:
    """把 Cookie 头字符串解析为 Playwright add_cookies 格式。

    每个 k=v 段转一条 cookie; __Secure-/__Host- 前缀标记 secure。
    domain 形如 amazon.com, 自动加前导点以覆盖子域。
    """
    cookies = []
    dot_domain = domain if domain.startswith(".") else f".{domain}"
    # 持久 Profile 退出时会丢弃无 expires 的会话 Cookie；赋予 30 天有效期确保落盘复用
    expires = int(time.time()) + 30 * 86400
    for pair in str(cookie_header or "").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            continue
        secure = name.startswith("__Secure-") or name.startswith("__Host-")
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": dot_domain,
                "path": "/",
                "expires": expires,
                "secure": secure,
                "sameSite": "Lax",
            }
        )
    return cookies


def _normalize_cdp_url(url: str | None) -> str | None:
    """CDP URL 主机名 localhost -> 127.0.0.1。

    Chrome 调试端口默认仅绑定 IPv4(127.0.0.1), 而 Node/Playwright 解析
    localhost 时优先 IPv6 ::1, 失败后不回落, 报 ECONNREFUSED ::1:<port>。
    统一改写主机名, 避免配置/文档示例里的 localhost 踩坑。
    """
    if not url:
        return url
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        host = parsed.hostname
        if host and host.lower() == "localhost":
            netloc = parsed.netloc.replace(host, "127.0.0.1", 1)
            return urlunparse(
                (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
            )
    except Exception:
        pass
    return url


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        raise BrowserError(
            "未安装 playwright, 请先执行: pip install playwright && playwright install chromium"
        ) from exc


# ---- 2026-11-21 新版无头指纹对齐(1000+400 条隔离验证通过率 98.5%/有效商品 100%) ----
# Playwright 默认 headless=True 走精简版 chrome-headless-shell, UA 带 HeadlessChrome 且缺真实渲染栈,
# 连续 ~100 条即触发亚马逊 3.5KB captcha; 改用本机 Chrome `--headless=new` + 清洗 UA + 对齐站点指纹。
_MARKET_FINGERPRINTS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("amazon.co.uk", ("en-GB", "Europe/London")),
    ("amazon.de", ("de-DE", "Europe/Berlin")),
    ("amazon.fr", ("fr-FR", "Europe/Paris")),
    ("amazon.co.jp", ("ja-JP", "Asia/Tokyo")),
    ("amazon.ca", ("en-CA", "America/Toronto")),
    ("amazon.it", ("it-IT", "Europe/Rome")),
    ("amazon.es", ("es-ES", "Europe/Madrid")),
    ("amazon.com.au", ("en-AU", "Australia/Sydney")),
    ("amazon.com", ("en-US", "America/Los_Angeles")),
)
_DEFAULT_VIEWPORT = {"width": 1366, "height": 768}
_CACHED_CHROME_UA: str | None = None


def _market_fingerprint(market: str | None) -> tuple[str, str]:
    """按亚马逊站点返回 (locale, timezone_id), 未知站点默认美国站。"""
    m = str(market or "").strip().lower()
    for suffix, pair in _MARKET_FINGERPRINTS:
        if m == suffix or m.endswith("." + suffix):
            return pair
    return ("en-US", "America/Los_Angeles")


def _detect_clean_chrome_ua(pw) -> str | None:
    """探测本机 Chrome 真实 UA 并去掉 HeadlessChrome 标识(进程内缓存一次)。"""
    global _CACHED_CHROME_UA
    if _CACHED_CHROME_UA:
        return _CACHED_CHROME_UA
    browser = None
    try:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=False,
            args=[
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        page = browser.new_page()
        ua = str(page.evaluate("() => navigator.userAgent") or "").strip()
        if ua:
            _CACHED_CHROME_UA = ua.replace("HeadlessChrome/", "Chrome/")
            return _CACHED_CHROME_UA
    except Exception:  # noqa: BLE001 — 无本机 Chrome 或探测失败时由调用方回退
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return None


class BrowserAutomation:
    """Playwright 持久上下文封装。用 with 语句管理生命周期。"""

    # 进程内登录确认缓存: 同一轮运行(预检查+各步骤)只检查一次登录,
    # 避免每个步骤都重复导航登录页验证(会话短时间内不会失效)
    _login_verified: dict = {}  # profile_dir -> bool(多账号: 按 Profile 隔离登录确认)

    @classmethod
    def login_verified(cls, profile: str = "") -> bool:
        return bool(cls._login_verified.get(profile or "", False))

    @classmethod
    def mark_login_verified(cls, profile: str = "") -> None:
        cls._login_verified[profile or ""] = True
        # 登录成功 → 自动把登录态备份回该账号的种子 profile
        # （卖家精灵 profile 按类目隔离，种植/副本之间需要自动同步）
        if profile:
            try:
                from .ss_profile_sync import backup_login
                backup_login(profile)
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    def reset_login_verified(cls, profile: str | None = None) -> None:
        """清除登录缓存(换号/登出后调用); profile=None 清全部。"""
        if profile is None:
            cls._login_verified = {}
        else:
            cls._login_verified.pop(profile or "", None)

    def __init__(
        self,
        config: Config,
        profile_dir: str,
        headed: bool = False,
        dry_run: bool = False,
        downloads_path: str | None = None,
        cdp_url: str | None = None,
        amazon_share_root: str | None = None,
        proxy_url: str | None = None,
        amazon_slot_id: str = "",
        block_media: bool = False,
    ):
        self.config = config
        self.profile_dir = profile_dir
        self.headed = headed
        self.dry_run = dry_run
        self.downloads_path = downloads_path
        self.cdp_url = cdp_url
        self.amazon_share_root = amazon_share_root
        self.proxy_url = str(proxy_url or "").strip() or None
        self.amazon_slot_id = str(amazon_slot_id or "").strip()
        self.block_media = bool(block_media)
        self._media_blocking_active = False
        self._proxy_bridge = None
        self._last_activity = time.monotonic()
        self._watchdog_stop = None
        self._watchdog_thread = None
        self._watchdog_tripped = False
        self._pw = None
        self._context = None
        self._browser = None
        self._pages: list = []

    @property
    def is_cdp(self) -> bool:
        """CDP 直连模式(直连用户已打开的 Chrome, 复用持久会话, 不弹新窗口)。"""
        return bool(self.cdp_url) and not self.dry_run

    def __enter__(self) -> "BrowserAutomation":
        if self.dry_run:
            return self
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        if self.downloads_path:
            Path(self.downloads_path).mkdir(parents=True, exist_ok=True)
        sync_playwright = _import_playwright()
        self._pw = sync_playwright().start()
        try:
            return self._enter_browser()
        except BaseException:
            self._stop_watchdog()
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None
            self._stop_proxy_bridge()
            self._context = None
            self._browser = None
            raise

    def touch_watchdog(self) -> None:
        """刷新浏览器活跃时间戳，防止正常执行被看门狗误判卡死。"""
        self._last_activity = time.monotonic()

    def _resolve_hang_timeout(self) -> float:
        raw = self.config.get("BROWSER_HANG_TIMEOUT", 360) if self.config is not None else 360
        try:
            sec = float(raw)
        except (TypeError, ValueError):
            sec = 360.0
        if sec <= 0:
            return 0.0
        # 有头窗口可能用于人工点人机验证(最多 300s)，放宽到至少 600s 避免误杀
        if self.headed:
            return max(sec, 600.0)
        return max(sec, 60.0)

    def _start_watchdog(self) -> None:
        """启动后台无响应看门狗（覆盖步骤 00/01/09/10，CDP 模式不启用）。"""
        if self.dry_run or self.is_cdp or not self.profile_dir:
            return
        timeout_sec = self._resolve_hang_timeout()
        if timeout_sec <= 0:
            return
        import threading
        self._watchdog_tripped = False
        self._last_activity = time.monotonic()
        stop_ev = threading.Event()
        self._watchdog_stop = stop_ev

        def _loop() -> None:
            poll = min(10.0, max(1.0, timeout_sec / 6.0))
            while not stop_ev.wait(timeout=poll):
                idle = time.monotonic() - self._last_activity
                if idle >= timeout_sec:
                    self._watchdog_tripped = True
                    try:
                        import logging
                        logging.getLogger("pipeline").warning(
                            "[BrowserWatchdog] 浏览器已连续 %.0f 秒无活动(上限 %.0fs，疑似渲染/IPC假死)，"
                            "强制终止隔离 Chrome 以释放抓取槽位: profile=%s",
                            idle, timeout_sec, self.profile_dir,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    self._force_kill_profile_chrome()
                    return

        t = threading.Thread(target=_loop, daemon=True, name="browser-hang-watchdog")
        self._watchdog_thread = t
        t.start()

    def _stop_watchdog(self) -> None:
        ev = getattr(self, "_watchdog_stop", None)
        self._watchdog_stop = None
        if ev is not None:
            try:
                ev.set()
            except Exception:  # noqa: BLE001
                pass
        self._watchdog_thread = None

    def _driver_pid(self) -> int:
        """获取当前 BrowserAutomation 实例专属的 Playwright driver 子进程 PID。"""
        try:
            transport = self._pw._impl_obj._connection._transport  # type: ignore[union-attr]
            proc = getattr(transport, "_proc", None)
            return int(getattr(proc, "pid", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _force_kill_profile_chrome(self) -> None:
        """定向强杀当前实例的 Playwright driver + 隔离 Chrome 进程树，绝不误杀其他类目或卖家精灵。"""
        driver_pid = self._driver_pid()
        if driver_pid > 0:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(driver_pid)],
                        timeout=10,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc = self._pw._impl_obj._connection._transport._proc  # type: ignore[union-attr]
                if proc is not None:
                    proc.kill()
                    return
            except Exception:  # noqa: BLE001
                pass

        target = os.path.normcase(os.path.abspath(self.profile_dir))
        if not target:
            return
        if os.name == "nt":
            try:
                escaped = target.replace("'", "''")
                ps_cmd = (
                    "$t = '" + escaped + "'.ToLower(); "
                    "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe' OR Name='chrome-headless-shell.exe'\" "
                    "| Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($t) } "
                    "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", target],
                    timeout=10,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:  # noqa: BLE001
                pass

    def _prepare_playwright_proxy(self) -> dict | None:
        if not self.proxy_url:
            return None
        from core.proxy_utils import LocalSocks5HttpBridge, parse_proxy_url
        spec = parse_proxy_url(self.proxy_url)
        if spec is None:
            return None
        if spec.needs_bridge_for_chromium:
            bridge = LocalSocks5HttpBridge(spec)
            local_url = bridge.start()
            self._proxy_bridge = bridge
            return {"server": local_url}
        out: dict[str, str] = {"server": f"{spec.scheme}://{spec.host}:{spec.port}"}
        if spec.username:
            out["username"] = spec.username
        if spec.password:
            out["password"] = spec.password
        return out

    def _stop_proxy_bridge(self) -> None:
        bridge = getattr(self, "_proxy_bridge", None)
        self._proxy_bridge = None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:  # noqa: BLE001
                pass

    def _enter_browser(self) -> "BrowserAutomation":
        if self.is_cdp:
            # CDP 直连已打开的 Chrome(需求文档技术方案: 持久 Profile 登录态,
            # 不弹新窗口); 用其默认上下文, 新页面=新标签页。
            # 2026-09: 连接前探测, 调试 Chrome 未启动则自动拉起(断点/中断后再跑无需手动开 Chrome);
            # 自动启动仍失败 → 明确报错(提示手动 start_ss_chrome.py), 不再静默崩溃。
            if not _cdp_alive(self.cdp_url):
                ok = ensure_ss_chrome(self.config)
                if not ok:
                    raise BrowserError(
                        f"CDP 连接失败且自动启动 Chrome 未就绪: {self.cdp_url} —— "
                        "请运行 python scripts/start_ss_chrome.py 并保持 Chrome 运行, "
                        "或检查 SS_CDP_URL / 是否同 Profile 已被其它 Chrome 占用"
                    )
            else:
                try:
                    import logging
                    lg = logging.getLogger("pipeline")
                    if lg.handlers:
                        _u = str(os.environ.get("PIPELINE_WORKBENCH_USER") or "").strip() or "default"
                        _p = cdp_port_from_url(self.cdp_url)
                        lg.info(f"[卖家精灵CDP] 复用已启动浏览器 (用户={_u}, 端口={_p}, 地址={self.cdp_url})")
                except Exception:  # noqa: BLE001
                    pass
            if os.environ.get('PIPELINE_WORKBENCH_USER'):
                from .ss_lock import find_profile_cdp_port
                actual = find_profile_cdp_port(self.profile_dir)
                if actual != cdp_port_from_url(self.cdp_url):
                    raise BrowserError('CDP 端口不属于当前工作台用户的 Profile，停止连接以避免账号混用')
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            self._context = (
                self._browser.contexts[0]
                if self._browser.contexts
                else self._browser.new_context()
            )
            return self
        base_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        headless_mode = (
            str(self.config.get("BROWSER_HEADLESS_MODE", "new") or "new").strip().lower()
            if self.config is not None
            else "new"
        )
        pw_proxy = self._prepare_playwright_proxy()
        if not self.headed and headless_mode != "legacy":
            locale, timezone_id = _market_fingerprint(
                getattr(self.config, "market", "") if self.config is not None else ""
            )
            clean_ua = _detect_clean_chrome_ua(self._pw)
            launch_kw: dict = {
                "user_data_dir": self.profile_dir,
                "channel": "chrome",
                "headless": False,
                "viewport": dict(_DEFAULT_VIEWPORT),
                "locale": locale,
                "timezone_id": timezone_id,
                "accept_downloads": True,
                "downloads_path": self.downloads_path,
                "timeout": 60000,
                "args": ["--headless=new", *base_args],
            }
            if pw_proxy is not None:
                launch_kw["proxy"] = pw_proxy
            if clean_ua:
                launch_kw["user_agent"] = clean_ua
            try:
                self._browser = self._pw.chromium.launch_persistent_context(**launch_kw)
            except Exception:  # noqa: BLE001 — 本机未装 Chrome 时回退内置 Chromium 无头
                fallback_kw = {
                    "user_data_dir": self.profile_dir,
                    "headless": True,
                    "viewport": dict(_DEFAULT_VIEWPORT),
                    "locale": locale,
                    "timezone_id": timezone_id,
                    "accept_downloads": True,
                    "downloads_path": self.downloads_path,
                    "timeout": 60000,
                    "args": base_args,
                }
                if pw_proxy is not None:
                    fallback_kw["proxy"] = pw_proxy
                if clean_ua:
                    fallback_kw["user_agent"] = clean_ua
                self._browser = self._pw.chromium.launch_persistent_context(**fallback_kw)
        else:
            headed_kw: dict = {
                "user_data_dir": self.profile_dir,
                "headless": not self.headed,
                "accept_downloads": True,
                "downloads_path": self.downloads_path,
                "timeout": 60000,
                "args": base_args,
            }
            if self.headed:
                # 有头模式关闭 Playwright 默认 1280x720 固定视口，避免窗口最大化后右侧大片留白
                headed_kw["no_viewport"] = True
            if pw_proxy is not None:
                headed_kw["proxy"] = pw_proxy
            self._browser = self._pw.chromium.launch_persistent_context(**headed_kw)
        self._context = self._browser
        try:
            self._context.set_default_timeout(45000)
            self._context.set_default_navigation_timeout(60000)
        except Exception:  # noqa: BLE001
            pass
        self._install_media_blocker()
        if self.amazon_share_root:
            self.sync_shared_amazon_login(self.amazon_share_root)
        self._start_watchdog()
        return self

    _BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})
    _HEADED_BLOCKED_RESOURCE_TYPES = frozenset({"media", "font"})
    _CAPTCHA_IMAGE_HINTS = (
        "captcha",
        "opfcaptcha",
        "cvf",
        "waf",
        "puzzle",
        "arkose",
        "challenge",
    )
    _BLOCKED_TELEMETRY_HOSTS = (
        "amazon-adsystem.com",
        "fls-na.amazon.com",
        "fls-eu.amazon.com",
        "fls-fe.amazon.com",
        "unagi.amazon.com",
        "aax-us-iad.amazon.com",
        "aax-fe.amazon.com",
        "doubleclick.net",
        "googlesyndication.com",
        "google-analytics.com",
    )

    def _should_block_media(self) -> bool:
        """非 CDP 抓取且调用方显式开启 block_media 时拦截图片/媒体/字体/广告埋点（无头额外拦 CSS，有头保留样式与验证码图）。"""
        if self.dry_run or self.is_cdp or not self.block_media:
            return False
        raw = self.config.get("BROWSER_BLOCK_MEDIA", "true") if self.config is not None else "true"
        return str(raw if raw is not None else "true").strip().lower() not in ("0", "false", "no", "off")

    @classmethod
    def _is_blocked_request(cls, resource_type: str, url: str, *, headed: bool = False) -> bool:
        rtype = str(resource_type or "").lower()
        low_url = str(url or "").lower()
        if headed:
            if rtype in cls._HEADED_BLOCKED_RESOURCE_TYPES:
                return True
            if rtype == "image":
                # 有头兜底放行验证码/人机挑战图片，拦截普通商品图以大幅压降内存
                if any(tok in low_url for tok in cls._CAPTCHA_IMAGE_HINTS):
                    return False
                return True
        else:
            if rtype in cls._BLOCKED_RESOURCE_TYPES:
                return True
        if low_url:
            try:
                from urllib.parse import urlparse
                host = (urlparse(low_url).hostname or "").lower()
                if host and any(host == h or host.endswith("." + h) for h in cls._BLOCKED_TELEMETRY_HOSTS):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _install_media_blocker(self) -> bool:
        """在抓取上下文安装资源拦截器：加速页面加载并显著降低 Chromium 内存占用与假死风险。"""
        self._media_blocking_active = False
        if not self._should_block_media() or self._context is None:
            return False
        headed = bool(self.headed)
        try:
            def _route_handler(route) -> None:
                try:
                    req = route.request
                    if self._is_blocked_request(req.resource_type, req.url, headed=headed):
                        route.abort()
                    else:
                        route.continue_()
                except Exception:  # noqa: BLE001 — 页面/上下文正在关闭时静默忽略
                    pass

            self._context.route("**/*", _route_handler)
            self._media_blocking_active = True
            return True
        except Exception:  # noqa: BLE001
            return False

    def sync_shared_amazon_login(self, profile_root: str | None = None, *, slot_id: str | None = None) -> bool:
        """将管理员维护的共享亚马逊登录态 Cookie 同步到当前浏览器上下文(静默兜底，不阻断启动)。"""
        root = profile_root or self.amazon_share_root
        if self.dry_run or not root or self._context is None:
            return False
        market = str(getattr(self.config, "market", "") or "").strip().lower()
        if not market:
            return False
        sid = self.amazon_slot_id if slot_id is None else str(slot_id or "").strip()
        try:
            from core import amazon_login_share
            if sid:
                if amazon_login_share.restore(self._context, root, market, slot_id=sid):
                    return True
                return False
            return bool(amazon_login_share.restore(self._context, root, market))
        except Exception:  # noqa: BLE001
            return False

    def __exit__(self, exc_type, exc, tb):
        self._stop_watchdog()
        try:
            if self.is_cdp:
                # CDP 模式: 只关闭自己创建的标签页, **不关闭用户的 Chrome**
                for p in self._pages:
                    try:
                        p.close()
                    except Exception:
                        pass
            elif self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._stop_proxy_bridge()
        self._context = None
        self._pw = None

    def new_page(self):
        self.touch_watchdog()
        if self.dry_run:
            return MockPage(self)
        if self._context is None:
            raise BrowserError("浏览器未启动")
        page = self._context.new_page()
        if self.is_cdp:
            self._pages.append(page)
        return page

    def goto(self, page, url: str, timeout: int = 60000):
        self.touch_watchdog()
        if self.dry_run:
            # dry-run 也要记住 URL: MockPage.content() 据此判断"这是商品页还是榜单页"
            # (2026-09-15: 让步骤1 浏览器兜底/步骤10 补抓在 dry-run 下可被冒烟覆盖)。
            try:
                page.url = url
            except Exception:
                pass
            return
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    def scroll_to_bottom(self, page, times: int = 5, delay: float = 1.0,
                         max_times: int = 40, settle: float = 1.5) -> int:
        """滚动触发懒加载(FR-00/FR-09), **直到真正到底或高度不再变化**。

        为什么不能只滚固定次数(2026-09-15 修复): 榜单/搜索结果页是**懒加载 + 分页式增量渲染**,
        固定 `times=5` 次 `mouse.wheel(0,8000)` 只覆盖约 4 万像素 —— 一页 50 个商品需要约 6~8 万像素,
        于是"看起来滚过底了, 其实只渲染出 31~32 个卡片", 表现为"每榜只抓到 32 个 ASIN"却毫无告警。
        现改为循环: 每次滚到底 → 等 `delay` → 比较 `scrollHeight`; 连续 `settle` 次无增长即认为到底。
        **`times` 语义保持向后兼容**: 它是最少滚动次数(原先就是"滚几次"), 真正上限由 `max_times` 控制。

        返回实际滚动次数(便于调用方记录/排查); dry-run 返回 0。
        """
        self.touch_watchdog()
        if self.dry_run:
            return 0
        min_times = max(1, int(times))
        cap = max(min_times, int(max_times))
        done = 0
        same = 0                      # 高度与上一轮相同的累计次数
        last_h = -1
        for i in range(1, cap + 1):
            done = i
            try:
                page.mouse.wheel(0, 8000)
            except Exception:
                break
            time.sleep(delay)
            try:
                h = int(page.evaluate("() => document.body ? document.body.scrollHeight : 0") or 0)
            except Exception:
                h = last_h                # 拿不到高度就当"没增长", 由 same 计数兜底收敛
            if h == last_h:
                same += 1
            else:
                same = 0
                last_h = h
            # 已经满足最少次数, 且连续 2 次高度不再增长 → 认定到底(懒加载结束)
            if i >= min_times and same >= 2:
                break
        return done

    def wait_selector(self, page, selector: str, timeout: int = 30000):
        if self.dry_run:
            return
        page.wait_for_selector(selector, timeout=timeout)

    def click(self, page, selector: str, timeout: int = 30000):
        """真实鼠标点击(isTrusted), 失败时用 DOM click 兜底。"""
        if self.dry_run:
            return
        try:
            page.locator(selector).first.scroll_into_view_if_needed(timeout=timeout)
            page.locator(selector).first.click(timeout=timeout)
        except Exception:
            page.locator(selector).first.evaluate("el => el.click()")

    def type_text(self, page, selector: str, text: str, timeout: int = 30000):
        if self.dry_run:
            return
        page.locator(selector).first.fill(text, timeout=timeout)

    def screenshot(self, page, path: str):
        if self.dry_run:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=path)
        except Exception:
            pass

    def wait_download(self, page, download_dir: str, timeout: int = 120) -> str | None:
        """监听下载目录新文件(过滤未完成文件), 返回最新文件路径。

        2026-11-03: **过滤 0 字节文件** —— 实测步骤11 的导出文件全部是 0 字节
        （文件已创建/重命名完成，但内容为空），wait_download 一看到文件就返回，
        导致后续合并拿到空文件。现在只有**非空**文件才算下载完成。
        """
        if self.dry_run:
            return None
        before = set(_list_downloads(download_dir))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            after = set(_list_downloads(download_dir))
            new = after - before
            new = {p for p in new if not p.endswith(_crdownload_suffixes)}
            new = {p for p in new if _file_size(p) > 0}
            if new:
                return max(new, key=os.path.getmtime)
        return None

    def trigger_download(self, page, download_dir: str, click_fn, timeout: int = 120) -> str:
        """CDP 模式下载: 执行 click_fn 触发下载, expect_download 捕获并保存到 download_dir。

        普通模式用 downloads_path 自动落盘+目录轮询; CDP 直连的是用户已打开的
        Chrome, 下载目录不可控, 故用事件捕获后 save_as 到目标目录。
        """
        if self.dry_run:
            return ""
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        with page.expect_download(timeout=timeout * 1000) as dl_info:
            click_fn()
        dl = dl_info.value
        name = dl.suggested_filename or f"download-{int(time.time())}.xlsx"
        target = os.path.join(download_dir, os.path.basename(name))
        dl.save_as(target)
        return target

    def set_download_dir(self, page, download_dir: str) -> None:
        """把下载目标目录设为规定文件夹(需求文档: 轮询下载目录方案)。

        CDP 模式: 通过 Browser.setDownloadBehavior 把已连接 Chrome 的下载
        **重定向**到 download_dir(浏览器本身下载到规定目录, 不再落到系统默认
        Downloads); 普通模式: launch 时已设 downloads_path, 无需处理。
        """
        if self.dry_run or not self.is_cdp:
            return
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        try:
            session = self._browser.new_browser_cdp_session()
            session.send(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": download_dir},
            )
        except Exception as exc:
            raise BrowserError(f"CDP 设置下载目录失败: {download_dir}, {exc}")

    def has_valid_amazon_auth_cookie(self, page) -> bool:
        """检查当前浏览器上下文是否持有目标站点未过期的亚马逊认证 Cookie(at-/sess-at-)。"""
        try:
            import re
            market = self.config.market.lower().lstrip('.')
            now = time.time()
            for c in page.context.cookies():
                name = str(c.get('name') or '')
                if not (re.match(r'^(?:at-|sess-at-)', name) and c.get('value')):
                    continue
                dom = str(c.get('domain') or '').lstrip('.').lower()
                if not (dom == market or dom.endswith('.' + market)):
                    continue
                exp = c.get('expires')
                if exp is None:
                    return True
                try:
                    exp_f = float(exp)
                except (TypeError, ValueError):
                    return True
                if exp_f <= 0 or exp_f > now:
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def is_amazon_captcha_page(page) -> bool:
        """当前页是否为亚马逊风控/验证码拦截页（而非 /ap/signin 掉登录页）。"""
        try:
            url = str(getattr(page, 'url', '') or '').lower()
            if '/ap/signin' in url or '/ap/register' in url:
                return False
            html = str(page.content() or '').lower()
            if not html:
                return False
            return any(
                m in html
                for m in ('captcha', 'robot check', 'automated access', 'verify you are human')
            )
        except Exception:
            return False

    def diagnose_amazon_login(self, page) -> tuple[bool, str]:
        """不导航页面，返回 `(是否已登录, 诊断说明)` 供管理员登录轮询实时展示。"""
        self.touch_watchdog()
        if self.dry_run:
            return True, "dry-run"
        try:
            import re
            from urllib.parse import urlparse as _urlparse
            cur_url = str(getattr(page, "url", "") or "").lower()
            parsed = _urlparse(cur_url)
            cur_host = (parsed.hostname or "").lower()
            cur_path = parsed.path or ""
            # 只检查 URL path，避免误杀登录成功跳回首页时的 query 参数(如 ?ref_=nav_ya_signin)
            if any(tok in cur_path for tok in (
                "/ap/", "/ax/claim", "validatecaptcha", "/cvf/", "/mfa", "challenge", "approval", "/signin",
            )):
                return False, f"当前仍在登录/验证页（{cur_path[:36]}）"
            if not self.has_valid_amazon_auth_cookie(page):
                market = self.config.market.lower().lstrip('.')
                try:
                    all_c = page.context.cookies()
                except Exception:
                    all_c = []
                matched = [
                    c for c in all_c
                    if str(c.get('domain') or '').lstrip('.').lower() == market
                    or str(c.get('domain') or '').lstrip('.').lower().endswith('.' + market)
                ]
                auth_names = [
                    str(c.get('name') or '')
                    for c in matched
                    if re.match(r'^(?:at-|sess-at-|x-main|sst-)', str(c.get('name') or ''))
                ]
                if auth_names:
                    return False, f"发现 {market} 认证Cookie({','.join(auth_names[:4])})但已过期或无有效 at-/sess-at-"
                return False, f"未检测到 {market} 认证Cookie（当前站点Cookie数={len(matched)}）"
            link = None
            try:
                # 使用 state="attached" 避免窄窗口或大促页头折叠时因不可见而误判超时
                link = page.wait_for_selector("#nav-link-accountList", state="attached", timeout=3000)
            except TypeError:
                link = page.wait_for_selector("#nav-link-accountList", timeout=3000)
            except Exception:
                link = None
            if link is not None:
                href = str(link.get_attribute('href') or '').lower()
                if not href and hasattr(link, 'query_selector'):
                    # 亚马逊新版页头中 #nav-link-accountList 改为 <div> 容器，真实链接在内层 <a>
                    inner = link.query_selector('a[href]')
                    href = str(inner.get_attribute('href') or '').lower() if inner else ''
                if '/ap/signin' in href or '/ap/register' in href:
                    return False, "账号菜单链接仍指向登录页"
                if href:
                    return True, "已确认登录态"
            # 兜底：部分大促/窄屏页头无标准 href 时，检查问候语文本与是否处于目标站正常页
            if hasattr(page, "query_selector"):
                try:
                    greet = page.query_selector("#nav-link-accountList-nav-line-1")
                    gtxt = (greet.inner_text() or "").strip().lower() if greet else ""
                    if gtxt:
                        if any(tok in gtxt for tok in (
                            "sign in", "anmelden", "identifiez", "iniciar", "accedi", "ログイン", "登录",
                        )):
                            return False, f"页头仍显示未登录（{gtxt[:24]}）"
                        return True, f"已确认登录态（{gtxt[:24]}）"
                except Exception:
                    pass
                market_dom = str(self.config.market or "").lower().lstrip(".")
                if market_dom and (cur_host == market_dom or cur_host.endswith("." + market_dom)):
                    try:
                        if not self.is_amazon_captcha_page(page) and not page.query_selector(
                            "#ap_email, #ap_password, input[name='email']"
                        ):
                            return True, "已确认登录态（认证Cookie+目标站首页）"
                    except Exception:
                        pass
            return False, f"未识别到登录态（当前URL={cur_host}{cur_path[:24]}）"
        except Exception as exc:
            return False, f"检测异常: {type(exc).__name__}"

    def check_amazon_login(self, page, navigate: bool = True) -> bool:
        """检测亚马逊登录态：账号菜单不能指向登录页，且必须有有效认证 Cookie。

        navigate=False 时不导航页面(用于手动登录等待, 避免打断用户输入)。
        """
        self.touch_watchdog()
        if self.dry_run:
            return True
        if navigate:
            host = self.config.market if str(self.config.market).lower().startswith("www.") else f"www.{self.config.market}"
            target_url = f"https://{host}/"
            last_exc = None
            for attempt in range(3):
                try:
                    self.goto(page, target_url)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(2.0 * (attempt + 1))
            if last_exc is not None:
                raise last_exc
            time.sleep(2)
        ok, _reason = self.diagnose_amazon_login(page)
        return ok

    def inject_cookies(self, page, cookie_header: str) -> bool:
        """把配置的 Cookie 头注入当前浏览器上下文(Profile 初始化导入)。

        仅用于 Profile 无有效会话时的首次导入; 已有有效会话时不应调用
        (需求 4.1: 避免旧 Cookie 覆盖 Profile 新登录态)。
        """
        if self.dry_run:
            return True
        cookies = parse_cookie_header(cookie_header, self.config.market)
        if not cookies:
            return False
        try:
            page.context.add_cookies(cookies)
            return True
        except Exception:
            return False

    def check_sellersprite_login(self, page, navigate: bool = True) -> bool:
        """检测卖家精灵登录态(2026-09-02 实测修正, 多通道判定)。

        实测现象(2026-09): ①未登录访问 /w/user/login 显示登录表单(可见
        input[name=email]/扫码登录 tab); ②已登录访问该 URL 内容为**工作台**
        (快捷搜索框 input[name=departmentKeyword]/高级会员/退出登录/问候语);
        ③/v3/reversing 未登录时头部显示"未登录 | 游客", 但**已登录时该页
        偶发仍显示游客标识**(多实例/缓存导致), 单看该页会误判——因此:
        任一"已登录"信号(工作台元素/会员/退出)出现即视为已登录;
        未登录表单/扫码 tab 出现即视为未登录; 最后才参考 v3/reversing 游客标识。
        navigate=False 时不导航, 直接检查当前页。
        """
        if self.dry_run:
            return True
        # 进程内已确认过登录(同一轮运行预检查+步骤间) → 直接放行, 不再导航验证
        if navigate and BrowserAutomation.login_verified(self.profile_dir):
            return True
        if navigate:
            self.goto(page, "https://www.sellersprite.com/w/user/login")
            # 工作台页面较重, 渲染可能需数秒——轮询最多 8 秒等信号出现
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                # 已登录信号①: 工作台快捷搜索框(仅登录后可见)
                try:
                    if page.locator("input[name=departmentKeyword]").first.is_visible():
                        BrowserAutomation.mark_login_verified(self.profile_dir)
                        return True
                except Exception:
                    pass
                # 已登录信号②: 工作台文本(会员/退出/问候)
                try:
                    body = page.inner_text("body") or ""
                except Exception:
                    body = ""
                if any(k in body for k in ("高级会员", "退出登录", "早上好")):
                    BrowserAutomation.mark_login_verified(self.profile_dir)
                    return True
                # 未登录信号: 可见的账号输入框(登录表单)
                try:
                    if page.locator("input[name=email]").first.is_visible():
                        return False
                except Exception:
                    pass
                time.sleep(0.8)
        else:
            # 不导航(手动登录等待场景): 直接检查当前页
            try:
                body = page.inner_text("body") or ""
            except Exception:
                body = ""
            if any(k in body for k in ("高级会员", "退出登录", "早上好")):
                BrowserAutomation.mark_login_verified(self.profile_dir)
                return True
            try:
                if page.locator("input[name=email]").first.is_visible():
                    return False
            except Exception:
                pass
            try:
                if page.locator("input[name=departmentKeyword]").first.is_visible():
                    BrowserAutomation.mark_login_verified(self.profile_dir)
                    return True
            except Exception:
                pass
            return False
        # 兜底: 反查页游客标识(可能误判, 仅作最后参考)
        try:
            self.goto(page, "https://www.sellersprite.com/v3/reversing")
            time.sleep(3)
            body2 = page.inner_text("body") or ""
        except Exception:
            body2 = ""
        if "未登录" in body2 or "游客" in body2:
            return False
        BrowserAutomation.mark_login_verified(self.profile_dir)
        return True

    def login_sellersprite(self, page, account: str, password: str, timeout: int = 180,
                           *, click_wait: float = 0.0) -> bool:
        """卖家精灵自动登录: 填写表单提交; 失败抛出需手动登录。

        登录页为 /w/user/login(2026-08 实测, /v2/login 已 404):
        邮箱 input[name=email], 密码 input[type=password], 提交 button[type=submit]。
        页面含多份表单(登录/注册/扫码 tab 切换), 必须只操作**可见**表单;
        默认若为扫码登录, 先点击"密码登录"类标签切换出账号密码表单。
        等待表单渲染(最多 15 秒), 应对慢网络下的动态加载。
        """
        if self.dry_run:
            return True
        self.goto(page, "https://www.sellersprite.com/w/user/login")
        # 已登录(工作台) → 无需登录(登录页对已登录用户直接显示工作台)
        try:
            if page.locator("input[name=departmentKeyword]").first.is_visible():
                return True
        except Exception:
            pass
        try:
            # 登录页默认可能停在扫码登录 tab, 账号密码输入框在隐藏表单中——
            # 只等"已挂载"不要求可见, 由 _fill_sellersprite_form 判断可见性并切 tab
            page.wait_for_selector("input[name=email]", state="attached", timeout=15000)
        except Exception as exc:
            raise BrowserError(f"卖家精灵登录页未加载出账号输入框(请检查页面结构或网络): {exc}")
        time.sleep(1)
        if self._fill_sellersprite_form(page, account, password, click_wait=click_wait):
            pass
        else:
            self._switch_to_password_tab(page)
            time.sleep(1)
            if not self._fill_sellersprite_form(page, account, password, click_wait=click_wait):
                raise BrowserError(
                    "未找到可见的卖家精灵账号/密码输入框(可能为扫码登录页或表单结构变化)"
                    + self._login_page_diag(page)
                )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.check_sellersprite_login(page):
                BrowserAutomation.mark_login_verified(self.profile_dir)
                return True
            time.sleep(3)
        return False

    def _first_visible(self, page, selectors) -> object | None:
        """返回第一个存在且可见的 locator, 否则 None。

        2026-09-08: 不能只查 `.first`——登录页同一选择器常有**多份表单**
        (扫码隐藏表单 + 账号密码可见表单), .first 会命中隐藏的那份误判;
        改为遍历该选择器的**全部元素**, 返回第一个可见的。
        """
        for sel in selectors:
            try:
                locs = page.locator(sel)
                for i in range(locs.count()):
                    el = locs.nth(i)
                    try:
                        if el.is_visible():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _fill_sellersprite_form(self, page, account: str, password: str, *,
                                click_wait: float = 0.0) -> bool:
        """填写并提交可见的账号密码表单, 返回是否找到可见表单。

        用 is_visible() 判断(兼容性优于 :visible 伪类), 等待渲染再操作。
        2026-09-08: 输入框选择器放宽(兼容 name=email/account/placeholder 等变体);
        找不到可见"提交"按钮时回车提交。
        """
        try:
            email = self._first_visible(page, (
                "input[name=email]", "input[name=account]", "input[name=username]",
                "input[placeholder*=邮箱]", "input[placeholder*=账号]", "input[placeholder*=帐号]",
            ))
            pwd = self._first_visible(page, (
                "input[type=password]", "input[name=password]",
                "input[placeholder*=密码]", "input[placeholder*=password]",
            ))
            if email is None or pwd is None:
                return False
            email.fill(account)
            pwd.fill(password)
            # 提交按钮同样不能只取 .first(扫码隐藏表单也有 button[type=submit]):
            # 在全部候选中找**第一个可见的**再点击, 找不到才回车兜底
            btn = self._first_visible(page, (
                "button[type=submit]", "button.login_btn",
                "button:has-text('立即登录')", "button:has-text('登 录')",
                "button:has-text('登录')", "button:has-text('Sign in')",
            ))
            if btn is not None:
                btn.click(timeout=5000)
            else:
                page.keyboard.press("Enter")
            if click_wait > 0:
                time.sleep(click_wait)
            return True
        except Exception:
            return False

    def _switch_to_password_tab(self, page) -> bool:
        """点击登录页中的"密码登录/账号登录"类标签(默认可能为扫码登录)。

        2026-09-08: 候选选择器放宽(role=tab + 多种文案), 每点一次后验证
        账号密码输入框是否转为可见, 可见才算切换成功。
        """
        cands = (
            "[role=tab]:has-text('密码')", "[role=tab]:has-text('账号')",
            "[role=tab]:has-text('邮箱')", "text=账号密码登录", "text=密码登录",
            "text=账号登录", "text=邮箱登录", "text=手机号登录",
            "text=使用密码登录", "text=密码 登录", "text=密码",
            "text=账号", "text=邮箱",
        )
        for sel in cands:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=3000)
                    time.sleep(1.5)
                    # 点击后账号密码输入框是否可见(切换成功才算)
                    if self._first_visible(page, (
                        "input[name=email]", "input[name=account]",
                        "input[placeholder*=邮箱]", "input[placeholder*=账号]",
                        "input[type=password]",
                    )) is not None:
                        return True
            except Exception:
                continue
        return False

    def _login_page_diag(self, page) -> str:
        """登录失败时的页面诊断(辅助人工/远程定位表单结构)。"""
        try:
            body = (page.inner_text("body") or "")[:200].replace("\n", " ")
        except Exception:
            body = ""
        inputs = []
        try:
            for el in page.locator("input").all()[:10]:
                try:
                    inputs.append(
                        f"{el.get_attribute('name') or ''}/{el.get_attribute('type') or ''}/"
                        f"{(el.get_attribute('placeholder') or '')[:24]}"
                        f"{'[可见]' if el.is_visible() else '[隐藏]'}"
                    )
                except Exception:
                    continue
        except Exception:
            pass
        diag = f" | 页面文本:{body[:150]} | 输入框:" + ("; ".join(inputs) if inputs else "无")
        return diag[:400]

    def mock_sellersprite(self, ctx) -> MockSellersprite:
        return MockSellersprite(ctx)


class MockPage:
    """dry-run 模式的页面替身。"""

    def __init__(self, browser: BrowserAutomation):
        self.browser = browser
        self.url = ""

    def content(self) -> str:
        """按当前 URL 返回模拟页面内容。

        - `/dp/<ASIN>`(商品详情页) → 返回与 MockHttp 同形的**商品页**(含 productTitle
          与五点), 使步骤1 的"浏览器兜底"、步骤10 的"浏览器补抓"在 dry-run 下也能跑通
          真实解析路径(2026-09-15 补: 此前一律返回榜单页 HTML, 浏览器兜底在 dry-run 里
          必然 `parse_failed`, 等于这条新路径没有任何冒烟覆盖)。
        - 其它 → 含模拟 ASIN 的榜单/搜索页(便于步骤0/9 的真实解析路径工作)。
        """
        import re as _re

        from .mock import MockHttp, make_asins

        m = _re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", self.url or "")
        if m:
            return MockHttp().get(f"/dp/{m.group(1)}").text
        asins = make_asins(12)
        links = "\n".join(
            f'<div class="s-result-item"><a href="https://www.amazon.com/dp/{a}">{a}</a></div>'
            for a in asins
        )
        return f"<html><body><div id='gridItemRoot'>{links}</div></body></html>"

    def title(self) -> str:
        return "mock"

    def wait_for_timeout(self, ms: int) -> None:
        """dry-run 占位等待(2026-09-15 补): 步骤9 在结果卡渲染等待后会调它。

        此前 MockPage 缺这个方法 → dry-run 跑步骤9 必抛
        `AttributeError: 'MockPage' object has no attribute 'wait_for_timeout'`,
        导致"dry-run 全流程冒烟"覆盖不到步骤9。真实 Page 语义是不阻塞地等一会儿,
        替身里可安全地什么都不做(不引入真实 sleep, 冒烟保持秒级)。
        """
        return None


def _list_downloads(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, name)
        for name in os.listdir(directory)
    ]


def _file_size(path: str) -> int:
    """文件大小（不存在/读不到按 0）。用于把 0 字节的"假下载"过滤掉。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
