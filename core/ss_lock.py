# -*- coding: utf-8 -*-
"""卖家精灵浏览器**互斥锁**(多类目并行: 同一个浏览器同一时刻只允许一个流水线用它)。

背景(2026-09-15, 用户要"看板里同时跑多个类目"):
  步骤 0(登录检查)/11(查流量来源)/12(查竞品提交)/13(竞品导出下载)都要驱动
  **卖家精灵浏览器**。CDP 直连模式(`SS_CDP_URL`)下, **一个 CDP 端口 = 一个 Chrome =
  一份登录态**: 两个类目同时用它, 会互相抢标签页、把对方页面导航走、把导出文件落错目录,
  导出的还是**对方账号**的数据。账号配额(每账号每天 50 次导出)也是跟着账号走的。

锁的粒度 = **浏览器资源**, 不是全局一把(2026-09-15 用户告知"会给不同类目配不同账号"后调整):
  * `resource_key()`: CDP 模式取**归一化后的 CDP 地址**(同端口=同一个 Chrome=同一份登录态);
    非 CDP 模式取 **profile 目录**(各起各的浏览器);
  * **不同资源 = 不同的锁** → 真并行, 互不等待(例: A 用 9222 + 账号A, B 用 9223 + 账号B);
  * **同一资源 = 同一把锁** → 后到的**等待**(默认上限 `SS_BROWSER_LOCK_WAIT` 秒, 默认 1800),
    等待期间每 30 秒往流水线日志写一行"在等谁", 不会静默卡住; 超时明确报错并指明占用者;
  * 锁用 OS 独占文件锁实现(Windows `msvcrt.locking` / POSIX `fcntl.flock`),
    **进程一死由操作系统自动释放**, 不需要"陈旧锁"清理逻辑。

⚠️ 同端口 + **不同账号**是硬冲突(见 `browser_conflict()`): 一个 Chrome 只能保持一份登录态,
   后跑的会读到/用到前一个账号的配额与数据(静默出错数据, 比排队更危险) —— 由启动守门
   (`webui/server.py::run_start_guard`)与 `scripts/parallel_cats.py` 拦下, 提示改用不同端口。

与 `.run.lock` 的分工(两个锁互不干扰):
  * `core/run_lock.py::.run.lock` —— **每个类目一把**: 防同一个类目双跑(不同类目可并行);
  * 本模块 —— **每个卖家精灵浏览器资源一把**: 防两个类目同时用同一个 Chrome。

锁文件位置: **项目根 `data/.locks/sellersprite-<资源>.lock`**(与 CAT / DATA_ROOT 无关 ——
各类目的 DATA_ROOT 可能不同, 锁必须落在同一个目录下才有互斥意义);
可用环境变量 `PIPELINE_SS_LOCK` 覆盖成固定文件(测试/单资源场景)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

# 复用 .run.lock 的 OS 锁原语(同一套跨平台实现, 避免两处语义漂移)
from .run_lock import _LOCK_OFFSET, _read_lock_info, _try_lock, _unlock

_LOCK_DIR = Path(__file__).resolve().parent.parent / "data" / ".locks"
_POLL_SECONDS = 1.0
_LOG_EVERY_SECONDS = 30.0
DEFAULT_WAIT_SECONDS = 1800.0
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_cdp_url(url: str) -> str:
    """归一化 CDP 地址: 只留 `host:port`(忽略路径/大小写; `localhost` 视同 `127.0.0.1`)。

    Chrome 的调试端口只绑 IPv4, `localhost` 解析 `::1` 会连不上, 系统已有兜底改写;
    这里把两者视为**同一个资源**, 避免"同一端口因为写法不同而被当成两个资源"。
    """
    u = str(url or "").strip()
    if not u:
        return ""
    m = re.match(r"^(?:https?://)?([^/]+)", u, re.I)
    hostport = (m.group(1) if m else u).strip().lower()
    if ":" not in hostport:
        hostport = f"{hostport}:9222"
    host, _, port = hostport.rpartition(":")
    if host in ("localhost", "::1", "[::1]"):
        host = "127.0.0.1"
    return f"{host}:{port}"


def resource_key(cdp_url: str = "", profile_dir: str = "") -> str:
    """本次要用的卖家精灵浏览器资源标识(不同资源可并行, 同资源互斥)。

    CDP 模式 → `cdp:<host:port>`; 非 CDP 模式 → `profile:<profile目录名>`。
    """
    cdp = normalize_cdp_url(cdp_url)
    if cdp:
        return f"cdp:{cdp}"
    prof = Path(str(profile_dir or "")).name if profile_dir else "default"
    return f"profile:{prof or 'default'}"


def _slug(text: str) -> str:
    return _UNSAFE.sub("_", str(text or "")).strip().rstrip(". ") or "default"


def lock_dir() -> Path:
    """锁目录(默认项目根 `data/.locks`; 环境变量 `PIPELINE_SS_LOCK_DIR` 可覆盖, 便于测试隔离)。"""
    env = str(os.environ.get("PIPELINE_SS_LOCK_DIR") or "").strip()
    return Path(env) if env else _LOCK_DIR


def lock_path(resource: str = "") -> str | Path:
    """资源对应的锁文件。

    优先级: `PIPELINE_SS_LOCK`(固定单文件, 测试/单资源场景) → `PIPELINE_SS_LOCK_DIR`
    下的 `sellersprite-<资源>.lock` → 默认项目根 `data/.locks/`。
    """
    env = str(os.environ.get("PIPELINE_SS_LOCK") or "").strip()
    if env:
        return Path(env)
    slug = _slug(resource or "default").replace(":", "-")
    if len(slug) > 60:                      # 超长资源名(profile 路径)压成短哈希, 保证可读+唯一
        slug = f"{slug[:40]}-{hashlib.sha1(slug.encode('utf-8')).hexdigest()[:8]}"
    return lock_dir() / f"sellersprite-{slug}.lock"


def accounts_of(cfg) -> set[str]:
    """配置里出现的卖家精灵账号集合(主账号 + SS_ACCOUNTS 列表), 用于冲突判定。"""
    out: set[str] = set()
    try:
        main = str(cfg.get("SS_ACCOUNT", "") or "").strip().lower()
        if main:
            out.add(main)
        for item in (cfg.get("SS_ACCOUNTS") or []):
            if isinstance(item, dict):
                acc = str(item.get("account") or "").strip().lower()
                if acc:
                    out.add(acc)
    except Exception:  # noqa: BLE001
        pass
    return out


def primary_account(cfg) -> str:
    """本轮**先用的**卖家精灵账号(SS_ACCOUNT; 缺省取 SS_ACCOUNTS 第一项)。

    冲突判定要看的是"这个 Chrome 里登录的是谁" —— 备用账号只在配额耗尽时才轮到,
    所以两个类目**主账号不同**就已经是硬冲突(哪怕它们共用同一份备用账号列表:
    实测踩到过, 只看集合交集会把这种情况误判成"账号相同"而放过)。
    """
    try:
        main = str(cfg.get("SS_ACCOUNT", "") or "").strip().lower()
        if main:
            return main
        for item in (cfg.get("SS_ACCOUNTS") or []):
            if isinstance(item, dict):
                acc = str(item.get("account") or "").strip().lower()
                if acc:
                    return acc
    except Exception:  # noqa: BLE001
        pass
    return ""


def browser_conflict(a_cdp: str, a_primary: str, a_accounts: set[str],
                     b_cdp: str, b_primary: str, b_accounts: set[str]) -> str:
    """两个并行类目是否会在卖家精灵浏览器上打架 → 返回说明(空串=不冲突)。

    规则(2026-09-15 用户"给不同类目配不同账号"后定):
      * 资源不同(端口/profile 不同) → 不冲突, 可真并行;
      * 资源相同 + **主账号相同**(或两边都没写账号) → 只是排队(同一份登录态, 由锁串行);
      * 资源相同 + **主账号不同**(或账号集合完全不相交) → **硬冲突**: 一个 Chrome 只能
        保持一份卖家精灵登录态, 后跑的那个会读到/用到前一个账号的配额与数据(静默出错数据)
        → 必须拦下, 让用户给该类目配不同的 `SS_CDP_URL` 端口。
    """
    ra, rb = resource_key(a_cdp), resource_key(b_cdp)
    if ra != rb:
        return ""
    same_primary = (not a_primary) or (not b_primary) or (a_primary == b_primary)
    disjoint = bool(a_accounts and b_accounts and not (a_accounts & b_accounts))
    if same_primary and not disjoint:
        return ""
    who_a = a_primary or ", ".join(sorted(a_accounts)) or "未配置账号"
    who_b = b_primary or ", ".join(sorted(b_accounts)) or "未配置账号"
    return (f"同一个卖家精灵浏览器({normalize_cdp_url(a_cdp) or ra}) 上配了**不同账号** "
            f"({who_a} vs {who_b}): 一个 Chrome 只能保持一份登录态, 后跑的那个会读到/用到"
            f"前一个账号的配额与数据。请给其中一个类目换一个 CDP 端口"
            f"(如 SS_CDP_URL: http://127.0.0.1:9223), 让两个类目各用各的 Chrome、各用各的账号")


class SsBrowserLockTimeout(RuntimeError):
    """等待卖家精灵浏览器锁超时(说明另一个类目的流水线正在使用它)。"""


def _holder_text(info: dict) -> str:
    """把锁内记录翻译成"是谁在占用"(读不到时给出可操作提示)。"""
    if not info:
        return "另一个流水线进程"
    pid = info.get("pid")
    label = str(info.get("label") or "").strip()
    started = str(info.get("started") or "").strip()
    who = label or (f"pid={pid}" if pid and pid > 0 else "另一个流水线进程")
    extra = f", 开始于 {started}" if started else ""
    return f"{who}{extra}" + (f" (pid={pid})" if pid and pid > 0 and label else "")


class SsBrowserLock:
    """已持有的锁句柄(建议 with 语句使用; 退出即释放)。"""

    def __init__(self, fh, path: Path, label: str, degraded: bool = False):
        self._fh = fh
        self.path = path
        self.label = label
        self.degraded = degraded          # True = 文件系统不支持锁, 已降级为"只记录不互斥"

    def holder(self) -> dict:
        return _read_lock_info(self.path)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "SsBrowserLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _write_body(fh, pid: int, label: str) -> None:
    """把 pid/入口/时间写进锁文件(锁在远偏移 `_LOCK_OFFSET`, 正文从 0 写不受影响)。

    末尾补空格覆盖上一次更长的正文, 避免旧行残留被误读(读取方只看前 3 行非空行)。
    """
    body = f"{pid}\n{label}\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    try:
        fh.seek(0)
        fh.write(body.encode("utf-8") + b" " * 80)
        fh.flush()
    except OSError:
        pass


def acquire(*, wait_seconds: float = DEFAULT_WAIT_SECONDS, label: str = "",
            log=None, poll: float = _POLL_SECONDS, resource: str = "") -> SsBrowserLock:
    """获取**该卖家精灵浏览器资源**的锁; 拿不到就等待, 超时抛 `SsBrowserLockTimeout`。

    resource: 资源标识(见 `resource_key()`; 缺省按"单资源"处理, 等价于旧的全局锁)。
    wait_seconds<=0 表示"不等待"(拿不到立即抛错)。
    log: 可调用对象(如 `logging.Logger.info`), 用于打印等待/降级说明; 可为 None。
    """
    path = lock_path(resource)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    last_log = 0.0
    while True:
        try:
            # 必须是 **r+b**(而不是 a+b): 追加模式下 seek(0) 无效, 正文会被追加到文件尾,
            # 于是读取方永远读到**上一个**持有者的信息(踩过)。
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            fh = os.fdopen(fd, "r+b")
        except OSError as exc:
            # 锁文件都建不出来(权限/只读盘) → 不阻断业务, 降级为无锁并说明
            if log:
                log(f"⚠ 卖家精灵浏览器锁不可用({exc}); 多类目并行时可能互相干扰")
            return SsBrowserLock(None, path, label, degraded=True)
        got = _try_lock(fh)
        if got is True:
            _write_body(fh, os.getpid(), f"{label}{' · ' + resource if resource else ''}")
            return SsBrowserLock(fh, path, label)
        info = _read_lock_info(path)
        fh.close()
        if got is None:
            if log:
                log(f"⚠ 该文件系统不支持独占锁({path}); 卖家精灵浏览器将不互斥, "
                    f"多类目并行时可能互相干扰")
            return SsBrowserLock(None, path, label, degraded=True)
        now = time.monotonic()
        if now >= deadline:
            where = f"该浏览器资源({resource})" if resource else "卖家精灵浏览器"
            raise SsBrowserLockTimeout(
                f"等待{where}超时({int(wait_seconds)}s): 正被 {_holder_text(info)} 占用 —— "
                f"同一个卖家精灵浏览器同一时刻只能被一个类目使用(一份登录态/一份导出配额)。"
                f"若想让两个类目**同时**用卖家精灵, 请给它们配**不同的 CDP 端口**"
                f"(各类目 SS_CDP_URL 用不同端口, 各起各的 Chrome、各用各的账号); "
                f"否则可等它跑完, 或调大配置 SS_BROWSER_LOCK_WAIT"
            )
        if log and (now - last_log) >= _LOG_EVERY_SECONDS:
            left = int(deadline - now)
            log(f"等待卖家精灵浏览器(正被 {_holder_text(info)} 占用, 最多再等 {left}s)…")
            last_log = now
        time.sleep(max(0.05, float(poll)))


def owner_path(resource: str) -> Path:
    """该浏览器资源的"上次是谁在用"记录文件(锁文件同目录, 便于人工排查)。"""
    return Path(f"{lock_path(resource)}.owner.json")


def read_owner(resource: str) -> dict:
    """读该资源上次的使用者 {cat, account, ts}(读不到返回 {})。"""
    try:
        return json.loads(owner_path(resource).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def write_owner(resource: str, cat: str, account: str) -> None:
    """记录本次使用该资源的类目/账号(供下一次"账号对不对"自检)。"""
    try:
        p = owner_path(resource)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"cat": str(cat or ""), "account": str(account or ""),
                                 "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class SsBrowserLease:
    """把"取锁 + 用浏览器 + 放锁"打包成上下文管理器(`ctx.browser("sellersprite")` 用)。

    进入顺序: 取全局锁 → 启动/连接浏览器; 退出顺序: 关浏览器 → 放锁。
    这样即使步骤里抛异常, 锁也一定会释放(`__exit__` 保证)。
    """

    def __init__(self, browser, *, wait_seconds: float = DEFAULT_WAIT_SECONDS,
                 label: str = "", log=None, warn=None, resource: str = "", account: str = ""):
        self._browser = browser
        self._wait = float(wait_seconds)
        self._label = label
        self._log = log
        self._warn = warn or log
        self._resource = resource
        self._account = str(account or "").strip().lower()
        self._lock: SsBrowserLock | None = None

    def _note_account(self) -> None:
        """**账号自检**(2026-09-15, 用户"给不同类目配不同账号"): 同一浏览器资源上次是谁在用?

        卖家精灵的登录检查只判"登没登录", **不判"登录的是哪个账号"** —— 一个 Chrome 只保持
        一份登录态, 所以若这个端口上次是别类目(另一个账号)在用、而里面的登录态没换过来,
        本类目会**静默地**读到/用掉上一个账号的导出配额与数据。这里记下使用者并在账号变化时
        明确告警(不阻断: 用户可能刚刚手动换过登录态)。
        """
        if not self._resource or not self._account:
            return
        prev = read_owner(self._resource)
        prev_acc = str(prev.get("account") or "").strip().lower()
        if prev_acc and prev_acc != self._account and self._warn:
            self._warn(
                f"该卖家精灵浏览器({self._resource}) 上次由「{prev.get('cat') or '?'}」用账号 "
                f"{prev_acc} 使用(于 {prev.get('ts') or '?'}); 本类目配的账号是 {self._account} —— "
                f"一个 Chrome 只保持一份登录态, 若里面的登录态没换成 {self._account}, "
                f"本类目会读到/用掉**上一个账号**的配额与数据。请确认该 Chrome 已登录 "
                f"{self._account}, 或给本类目换一个不同的 SS_CDP_URL 端口"
            )
        write_owner(self._resource, self._label, self._account)

    def __enter__(self):
        self._lock = acquire(wait_seconds=self._wait, label=self._label, log=self._log,
                             resource=self._resource)
        try:
            self._note_account()
            return self._browser.__enter__()
        except BaseException:
            self._lock.release()
            self._lock = None
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._browser.__exit__(exc_type, exc, tb)
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    def __getattr__(self, name: str):
        """把"误把租约当浏览器用"换成看得懂的报错(2026-09-16)。

        `ctx.browser(...)` 返回的是**租约**(取锁 + 浏览器), 真正的浏览器对象由
        `with ctx.browser(...) as b:` 绑定出来。若写成 `browser = ctx.browser(...)`
        再调 `browser.new_page()`, 原先只报
        `'SsBrowserLease' object has no attribute 'new_page'` —— 看不出该怎么改。
        这里在属性名确实是浏览器方法时给出可直接照做的提示。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        inner = self.__dict__.get("_browser")
        if inner is not None and hasattr(inner, name):
            raise AttributeError(
                f"ctx.browser(...) 返回的是租约 SsBrowserLease(取锁+浏览器), 不是浏览器对象; "
                f"请用 `with ctx.browser(...) as b:` 绑定后再调 b.{name}(...), "
                f"不能写 `b = ctx.browser(...)` 后直接 b.{name}(...)"
            )
        raise AttributeError(name)


# ---------------- CDP 端口自动分配（2026-11-12） ----------------
# 目的：多个用户各跑不同类目时，不用手改配置里的 SS_CDP_URL —— 启动前自动挑一个空闲端口。
# 规则：优先复用该用户上次用的端口（登录态/profile 与端口无关，只是浏览器调试端口）；
#       被占用就顺延；池固定 9222-9299。配置里显式写了 SS_CDP_URL 时仍然尊重配置。
CDP_PORT_MIN = 9222
CDP_PORT_MAX = 9299


def _ports_file():
    from pathlib import Path as _P
    return _P(__file__).resolve().parent.parent / ".webui" / "cdp_ports.json"


def _ports_load():
    import json
    try:
        d = json.loads(_ports_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _ports_save(d):
    import json
    try:
        f = _ports_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def port_in_use(port):
    """本机该端口是否已被监听（Chrome 在跑就是 True）。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return True


def pick_port(username="", exclude_ports=()):
    """分配一个空闲 CDP 端口；优先该用户上次用的。池满抛 RuntimeError。"""
    d = _ports_load()
    users = d.setdefault("users", {})
    name = str(username or "")
    reserved = {int(p) for p in exclude_ports}
    if name and users.get(name):
        try:
            cand = int(users[name])
        except (TypeError, ValueError):
            cand = 0
        if CDP_PORT_MIN <= cand <= CDP_PORT_MAX and cand not in reserved and not port_in_use(cand):
            return cand
    # 已被**其他用户**占用的端口不再分配（否则两个人都空闲时会拿到同一个端口）
    taken = set(reserved)
    for k, v in users.items():
        if name and str(k) == name:
            continue
        try:
            taken.add(int(v))
        except (TypeError, ValueError):
            pass
    for p in range(CDP_PORT_MIN, CDP_PORT_MAX + 1):
        if p in taken or port_in_use(p):
            continue
        if name:
            users[name] = p
            _ports_save(d)
        return p
    raise RuntimeError("CDP 端口池已满(%d-%d)，请先关闭一些 Chrome 再启动"
                       % (CDP_PORT_MIN, CDP_PORT_MAX))

