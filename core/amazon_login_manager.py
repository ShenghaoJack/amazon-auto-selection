"""管理员统一亚马逊站点登录与共享登录态发布管理器。

供 WebUI 管理员入口调用：
- 按站点（amazon.com / amazon.co.uk / amazon.de / amazon.fr 等）查看加密共享态是否已发布；
- 唤起有头 Chrome 使用共享种子 Profile（`data/profiles/amz-profile`）打开对应站点登录页，
  在管理员指定的等待时间（默认 300 秒，可配 30~1800 秒）内轮询检测登录是否完成；
- 登录验证通过后立刻调用 `core.amazon_login_share.save()` 将认证 Cookie 加密落盘到
  `data/profiles/amazon-login-share/<market>.bin`，供所有操作员（operator）自动复用。
"""
from __future__ import annotations

import copy
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core import amazon_login_share
from core.browser import BrowserAutomation
from core.config import Config

SUPPORTED_MARKETS: tuple[tuple[str, str], ...] = (
    ("amazon.com", "美国站 (US)"),
    ("amazon.co.uk", "英国站 (UK)"),
    ("amazon.de", "德国站 (DE)"),
    ("amazon.fr", "法国站 (FR)"),
    ("amazon.co.jp", "日本站 (JP)"),
    ("amazon.ca", "加拿大站 (CA)"),
    ("amazon.it", "意大利站 (IT)"),
    ("amazon.es", "西班牙站 (ES)"),
)

MIN_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 1800
DEFAULT_WAIT_SECONDS = 300
_POLL_INTERVAL = 2.5


def normalize_market(market: str) -> str:
    m = str(market or "").strip().lower()
    if m.startswith("https://"):
        m = m[8:]
    elif m.startswith("http://"):
        m = m[7:]
    m = m.split("/", 1)[0].strip()
    if m.startswith("www."):
        m = m[4:]
    if not re.fullmatch(r"amazon\.[a-z.]+", m):
        raise ValueError(f"无效的亚马逊站点: {market!r}（示例: amazon.com / amazon.co.uk / amazon.de）")
    return m


def normalize_wait_seconds(value: Any) -> int:
    if value is None or str(value).strip() == "":
        return DEFAULT_WAIT_SECONDS
    try:
        sec = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"等待时间必须是数字（{MIN_WAIT_SECONDS}~{MAX_WAIT_SECONDS} 秒）") from exc
    if sec < MIN_WAIT_SECONDS or sec > MAX_WAIT_SECONDS:
        raise ValueError(f"等待时间需在 {MIN_WAIT_SECONDS}~{MAX_WAIT_SECONDS} 秒之间（当前 {sec} 秒）")
    return sec


class AmazonLoginManager:
    """单实例管理管理员交互式亚马逊登录会话（同一时刻仅允许 1 个登录窗口）。"""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self._lock = threading.Lock()
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._task: dict[str, Any] = {
            "state": "idle",
            "market": "",
            "actor": "",
            "wait_seconds": DEFAULT_WAIT_SECONDS,
            "started_at": "",
            "finished_at": "",
            "deadline_mono": 0.0,
            "started_mono": 0.0,
            "message": "",
        }

    def profile_root(self) -> Path:
        return (self.project_root / "data" / "profiles").resolve()

    def shared_amz_profile(self) -> Path:
        p = self.profile_root() / "amz-profile"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def market_statuses(self) -> list[dict[str, Any]]:
        root = self.profile_root()
        out: list[dict[str, Any]] = []
        for market, label in SUPPORTED_MARKETS:
            share_path = amazon_login_share._path(root, market)
            exists = share_path.is_file()
            updated_at = ""
            size = 0
            if exists:
                try:
                    st = share_path.stat()
                    size = int(st.st_size)
                    updated_at = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                except OSError:
                    exists = False
            out.append(
                {
                    "market": market,
                    "label": label,
                    "shared": exists,
                    "updated_at": updated_at,
                    "size": size,
                }
            )
        return out

    def task_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = copy.deepcopy(self._task)
        now_m = time.monotonic()
        if snap.get("state") == "running" and snap.get("deadline_mono"):
            rem = max(0, int(round(snap["deadline_mono"] - now_m)))
            el = max(0, int(round(now_m - snap.get("started_mono", now_m))))
        else:
            rem = 0
            el = 0
        snap["remaining_seconds"] = rem
        snap["elapsed_seconds"] = el
        snap.pop("deadline_mono", None)
        snap.pop("started_mono", None)
        return snap

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "profile_dir": str(self.shared_amz_profile()),
            "share_dir": str(self.profile_root() / "amazon-login-share"),
            "min_wait_seconds": MIN_WAIT_SECONDS,
            "max_wait_seconds": MAX_WAIT_SECONDS,
            "default_wait_seconds": DEFAULT_WAIT_SECONDS,
            "markets": self.market_statuses(),
            "task": self.task_snapshot(),
        }

    def start_login(
        self,
        *,
        market: str,
        wait_seconds: Any = DEFAULT_WAIT_SECONDS,
        actor: str = "admin",
        cookie: str = "",
    ) -> dict[str, Any]:
        norm_market = normalize_market(market)
        wait_sec = normalize_wait_seconds(wait_seconds)
        cookie_str = str(cookie or "").strip()

        with self._lock:
            if self._task.get("state") == "running" and self._thread is not None and self._thread.is_alive():
                raise RuntimeError(
                    f"已有管理员登录窗口正在进行中（{self._task.get('market')}，操作人 {self._task.get('actor')}），"
                    "请先完成登录或点击「取消等待」"
                )
            cancel_ev = threading.Event()
            now_m = time.monotonic()
            self._cancel_event = cancel_ev
            self._task = {
                "state": "running",
                "market": norm_market,
                "actor": str(actor or "admin"),
                "wait_seconds": wait_sec,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "started_mono": now_m,
                "deadline_mono": now_m + wait_sec,
                "message": f"正在启动浏览器并打开 {norm_market}，等待管理员完成登录（最多 {wait_sec} 秒）…",
            }
            thread = threading.Thread(
                target=self._run_worker,
                args=(norm_market, wait_sec, cookie_str, cancel_ev),
                daemon=True,
                name=f"amz-admin-login-{norm_market}",
            )
            self._thread = thread
            thread.start()
        return {"ok": True, "task": self.task_snapshot()}

    def cancel_login(self) -> dict[str, Any]:
        with self._lock:
            ev = self._cancel_event
            running = self._task.get("state") == "running"
            if ev is not None and running:
                ev.set()
                self._task["message"] = "正在取消登录等待并关闭浏览器…"
        return {"ok": True, "cancelled": running, "task": self.task_snapshot()}

    def _update_task(self, **kwargs: Any) -> None:
        with self._lock:
            self._task.update(kwargs)

    def _run_worker(self, market: str, wait_sec: int, cookie_str: str, cancel_ev: threading.Event) -> None:
        cfg = Config(
            {
                "MARKET": market,
                "DRY_RUN": False,
                "DATA_ROOT": str(self.project_root / "data"),
                "PROFILE_DIR": str(self.profile_root()),
                "AMZ_PROFILE": str(self.shared_amz_profile()),
            }
        )
        host = market if market.startswith("www.") else f"www.{market}"
        deadline = time.monotonic() + wait_sec
        self._update_task(deadline_mono=deadline)
        try:
            with BrowserAutomation(cfg, profile_dir=str(self.shared_amz_profile()), headed=True) as browser:
                page = browser.new_page()
                try:
                    # 1. 若管理员提供了 Cookie，先导入并立即核验
                    if cookie_str:
                        self._update_task(message=f"正在向 {market} 导入 Cookie 并核验登录态…")
                        if browser.inject_cookies(page, cookie_str) and browser.check_amazon_login(page, navigate=True):
                            saved = amazon_login_share.save(
                                page.context, self.profile_root(), market, replace=True
                            )
                            if saved:
                                self._update_task(
                                    state="succeeded",
                                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message=f"Cookie 导入验证通过，已发布 {market} 加密共享登录态，所有操作员可立即复用。",
                                )
                                return
                        self._update_task(message=f"Cookie 核验未通过，已打开 {market} 登录页等待手动登录…")

                    # 2. 先检测当前共享 Profile 是否已经有效（若未提供 Cookie 且原本就已登录，也直接刷新共享态）
                    if not cookie_str:
                        self._update_task(message=f"正在打开 {market} 检查当前共享 Profile 会话…")
                        if browser.check_amazon_login(page, navigate=True):
                            saved = amazon_login_share.save(
                                page.context, self.profile_root(), market, replace=True
                            )
                            if saved:
                                self._update_task(
                                    state="succeeded",
                                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message=f"检测到共享 Profile 在 {market} 已处于登录态，已刷新加密共享登录态。",
                                )
                                return

                    # 3. 打开登录页，在管理员配置的等待时间内轮询等待手动登录完成
                    browser.goto(page, f"https://{host}/ap/signin")
                    self._update_task(
                        message=f"请在弹出的 Chrome 窗口完成 {market} 登录（支持账号密码与二次验证），最多等待 {wait_sec} 秒…"
                    )
                    while time.monotonic() < deadline:
                        if cancel_ev.is_set():
                            self._update_task(
                                state="cancelled",
                                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                message=f"管理员已取消 {market} 登录等待。",
                            )
                            return
                        if browser.check_amazon_login(page, navigate=False):
                            saved = amazon_login_share.save(
                                page.context, self.profile_root(), market, replace=True
                            )
                            if saved:
                                self._update_task(
                                    state="succeeded",
                                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message=f"{market} 登录确认成功！已发布加密共享登录态，所有操作员可立即复用。",
                                )
                                return
                        cancel_ev.wait(timeout=_POLL_INTERVAL)

                    self._update_task(
                        state="failed",
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        message=f"{market} 登录在 {wait_sec} 秒内未获确认，已关闭窗口；可增大等待时间后重试。",
                    )
                finally:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            self._update_task(
                state="failed",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                message=f"{market} 登录窗口启动或核验失败: {type(exc).__name__}: {exc}",
            )
