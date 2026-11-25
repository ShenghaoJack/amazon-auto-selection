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
        self._check_results: dict[str, dict[str, str]] = {}
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
            with self._lock:
                chk = dict(self._check_results.get(market) or {})
            out.append(
                {
                    "market": market,
                    "label": label,
                    "shared": exists,
                    "updated_at": updated_at,
                    "size": size,
                    "check_status": str(chk.get("status") or ""),
                    "check_message": str(chk.get("message") or ""),
                    "checked_at": str(chk.get("checked_at") or ""),
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

    def pool_statuses(self) -> list[dict[str, Any]]:
        from core import amazon_pool
        slots = amazon_pool.list_slots(self.profile_root())
        for s in slots:
            key = f"{s['market']}::{s['slot_id']}"
            with self._lock:
                chk = dict(self._check_results.get(key) or {})
            s["check_status"] = str(chk.get("status") or "")
            s["check_message"] = str(chk.get("message") or "")
            s["checked_at"] = str(chk.get("checked_at") or "")
        return slots

    def save_slot(
        self,
        *,
        slot_id: str = "",
        name: str = "",
        market: str = "amazon.com",
        proxy_url: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        from core import amazon_pool
        slot = amazon_pool.upsert_slot(
            self.profile_root(),
            slot_id=slot_id,
            name=name,
            market=market,
            proxy_url=proxy_url,
            enabled=enabled,
        )
        return {"ok": True, "slot": slot, "slots": self.pool_statuses()}

    def delete_slot(self, *, slot_id: str) -> dict[str, Any]:
        from core import amazon_pool
        deleted = amazon_pool.delete_slot(self.profile_root(), slot_id)
        return {"ok": True, "deleted": deleted, "slots": self.pool_statuses()}

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "profile_dir": str(self.shared_amz_profile()),
            "share_dir": str(self.profile_root() / "amazon-login-share"),
            "min_wait_seconds": MIN_WAIT_SECONDS,
            "max_wait_seconds": MAX_WAIT_SECONDS,
            "default_wait_seconds": DEFAULT_WAIT_SECONDS,
            "markets": self.market_statuses(),
            "slots": self.pool_statuses(),
            "task": self.task_snapshot(),
        }

    def _resolve_slot(self, slot_id: str, fallback_market: str) -> tuple[str, str, str, str, str]:
        """返回 (norm_market, norm_slot_id, display_label, profile_dir, proxy_url)。"""
        sid = str(slot_id or "").strip().lower()
        if sid:
            from core import amazon_pool
            slot = amazon_pool.get_slot(self.profile_root(), sid)
            if not slot:
                raise ValueError(f"未找到亚马逊槽位: {sid}")
            m = normalize_market(slot["market"])
            label = f"{m} / {slot['name']}({sid})"
            return m, sid, label, slot["profile_dir"], str(slot.get("proxy_url") or "")
        m = normalize_market(fallback_market)
        return m, "", m, str(self.shared_amz_profile()), ""

    def start_login(
        self,
        *,
        market: str,
        wait_seconds: Any = DEFAULT_WAIT_SECONDS,
        actor: str = "admin",
        cookie: str = "",
        slot_id: str = "",
    ) -> dict[str, Any]:
        norm_market, norm_sid, target_label, profile_dir, proxy_url = self._resolve_slot(slot_id, market)
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
                "market": target_label,
                "slot_id": norm_sid,
                "actor": str(actor or "admin"),
                "wait_seconds": wait_sec,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "started_mono": now_m,
                "deadline_mono": now_m + wait_sec,
                "message": f"正在启动浏览器并打开 {target_label}，等待管理员完成登录（最多 {wait_sec} 秒）…",
            }
            thread = threading.Thread(
                target=self._run_worker,
                args=(norm_market, norm_sid, target_label, profile_dir, proxy_url, wait_sec, cookie_str, cancel_ev),
                daemon=True,
                name=f"amz-admin-login-{norm_market}-{norm_sid or 'default'}",
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

    def check_login(self, *, market: str, actor: str = "admin", slot_id: str = "") -> dict[str, Any]:
        """在独立临时 Profile 中用无头浏览器在线验证指定站点/槽位已发布的共享登录态是否仍然有效。"""
        norm_market, norm_sid, target_label, _profile_dir, proxy_url = self._resolve_slot(slot_id, market)
        with self._lock:
            if self._task.get("state") == "running" and self._thread is not None and self._thread.is_alive():
                raise RuntimeError(
                    f"已有管理员任务正在进行中（{self._task.get('market')}，操作人 {self._task.get('actor')}），"
                    "请稍候或先点击「取消等待」"
                )
            cancel_ev = threading.Event()
            now_m = time.monotonic()
            self._cancel_event = cancel_ev
            self._task = {
                "state": "running",
                "market": target_label,
                "slot_id": norm_sid,
                "actor": str(actor or "admin"),
                "wait_seconds": 60,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "started_mono": now_m,
                "deadline_mono": now_m + 60,
                "message": f"正在启动无头浏览器在线检测 {target_label} 共享登录态有效性…",
            }
            thread = threading.Thread(
                target=self._run_check_worker,
                args=(norm_market, norm_sid, target_label, proxy_url, cancel_ev),
                daemon=True,
                name=f"amz-admin-check-{norm_market}-{norm_sid or 'default'}",
            )
            self._thread = thread
            thread.start()
        return {"ok": True, "task": self.task_snapshot()}

    def _set_check_result(self, market: str, status: str, message: str, *, slot_id: str = "") -> None:
        key = f"{market}::{slot_id}" if slot_id else market
        with self._lock:
            self._check_results[key] = {
                "status": status,
                "message": message,
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    def _run_check_worker(
        self,
        market: str,
        slot_id: str,
        target_label: str,
        proxy_url: str,
        cancel_ev: threading.Event,
    ) -> None:
        import tempfile

        share_path = amazon_login_share._path(self.profile_root(), market, slot_id=slot_id)
        if not share_path.is_file():
            msg = f"{target_label} 尚未发布加密共享登录态，请先点击登录。"
            self._set_check_result(market, "missing", msg, slot_id=slot_id)
            self._update_task(
                state="failed",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                message=msg,
            )
            return

        cfg = Config(
            {
                "MARKET": market,
                "DRY_RUN": False,
                "DATA_ROOT": str(self.project_root / "data"),
                "PROFILE_DIR": str(self.profile_root()),
            }
        )
        try:
            with tempfile.TemporaryDirectory(prefix=f"amz-check-{market}-") as tmp_profile:
                if cancel_ev.is_set():
                    self._update_task(
                        state="cancelled",
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        message=f"已取消 {target_label} 有效性检测。",
                    )
                    return
                with BrowserAutomation(
                    cfg,
                    profile_dir=tmp_profile,
                    headed=False,
                    amazon_share_root=str(self.profile_root()),
                    proxy_url=proxy_url or None,
                    amazon_slot_id=slot_id,
                ) as browser:
                    page = browser.new_page()
                    try:
                        logged_in = browser.check_amazon_login(page, navigate=True)
                        if logged_in:
                            nav_name = ""
                            try:
                                el = page.query_selector("#nav-link-accountList-nav-line-1")
                                nav_name = (el.inner_text() or "").strip() if el else ""
                            except Exception:  # noqa: BLE001
                                nav_name = ""
                            who = f"（{nav_name}）" if nav_name else ""
                            msg = f"{target_label} 共享登录态有效{who}，可正常复用。"
                            self._set_check_result(market, "valid", msg, slot_id=slot_id)
                            self._update_task(
                                state="succeeded",
                                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                message=msg,
                            )
                            return

                        is_cap = getattr(browser, "is_amazon_captcha_page", None)
                        has_auth = getattr(browser, "has_valid_amazon_auth_cookie", None)
                        if callable(is_cap) and callable(has_auth) and is_cap(page) and has_auth(page):
                            msg = (
                                f"{target_label} 认证 Cookie 仍在有效期内，但当前 IP 无头访问命中亚马逊人机验证，"
                                "暂无法直接确认是否掉登录（建议点「重新登录/刷新」在有头窗口核验）。"
                            )
                            self._set_check_result(market, "captcha", msg, slot_id=slot_id)
                            self._update_task(
                                state="failed",
                                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                message=msg,
                            )
                            return

                        msg = f"{target_label} 共享登录态已失效（页面显示未登录或 Cookie 已过期），请点击重新登录。"
                        self._set_check_result(market, "invalid", msg, slot_id=slot_id)
                        self._update_task(
                            state="failed",
                            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            message=msg,
                        )
                    finally:
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as exc:  # noqa: BLE001
            msg = f"{target_label} 有效性检测失败: {type(exc).__name__}: {exc}"
            self._set_check_result(market, "error", msg, slot_id=slot_id)
            self._update_task(
                state="failed",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                message=msg,
            )

    def _update_task(self, **kwargs: Any) -> None:
        with self._lock:
            self._task.update(kwargs)

    def _run_worker(
        self,
        market: str,
        slot_id: str,
        target_label: str,
        profile_dir: str,
        proxy_url: str,
        wait_sec: int,
        cookie_str: str,
        cancel_ev: threading.Event,
    ) -> None:
        cfg = Config(
            {
                "MARKET": market,
                "DRY_RUN": False,
                "DATA_ROOT": str(self.project_root / "data"),
                "PROFILE_DIR": str(self.profile_root()),
                "AMZ_PROFILE": profile_dir,
            }
        )
        host = market if market.startswith("www.") else f"www.{market}"
        deadline = time.monotonic() + wait_sec
        self._update_task(deadline_mono=deadline)
        try:
            browser_kw: dict[str, Any] = {"profile_dir": profile_dir, "headed": True}
            if proxy_url:
                browser_kw["proxy_url"] = proxy_url
            if slot_id:
                browser_kw["amazon_slot_id"] = slot_id
            save_kw: dict[str, Any] = {"replace": True}
            if slot_id:
                save_kw["slot_id"] = slot_id
            with BrowserAutomation(cfg, **browser_kw) as browser:
                page = browser.new_page()
                try:
                    # 1. 若管理员提供了 Cookie，先导入并立即核验
                    if cookie_str:
                        self._update_task(message=f"正在向 {target_label} 导入 Cookie 并核验登录态…")
                        if browser.inject_cookies(page, cookie_str) and browser.check_amazon_login(page, navigate=True):
                            saved = amazon_login_share.save(
                                page.context, self.profile_root(), market, **save_kw
                            )
                            if saved:
                                self._set_check_result(market, "valid", f"{target_label} 登录态有效", slot_id=slot_id)
                                self._update_task(
                                    state="succeeded",
                                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message=f"Cookie 导入验证通过，已发布 {target_label} 加密共享登录态。",
                                )
                                return
                        self._update_task(message=f"Cookie 核验未通过，已打开 {target_label} 登录页等待手动登录…")

                    # 2. 先检测当前 Profile 是否已经有效（若未提供 Cookie 且原本就已登录，也直接刷新共享态）
                    if not cookie_str:
                        self._update_task(message=f"正在打开 {target_label} 检查当前 Profile 会话…")
                        if browser.check_amazon_login(page, navigate=True):
                            saved = amazon_login_share.save(
                                page.context, self.profile_root(), market, **save_kw
                            )
                            if saved:
                                self._set_check_result(market, "valid", f"{target_label} 登录态有效", slot_id=slot_id)
                                self._update_task(
                                    state="succeeded",
                                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message=f"检测到 {target_label} 已处于登录态，已刷新加密共享登录态。",
                                )
                                return

                    # 3. 打开登录页，在管理员配置的等待时间内轮询等待手动登录完成
                    browser.goto(page, f"https://{host}/ap/signin")
                    self._update_task(
                        message=f"请在弹出的 Chrome 窗口完成 {target_label} 登录（含邮箱/手机二次验证），最多等待 {wait_sec} 秒…"
                    )
                    confirm_streak = 0
                    touch_wd = getattr(browser, "touch_watchdog", None)
                    while time.monotonic() < deadline:
                        if callable(touch_wd):
                            touch_wd()
                        if cancel_ev.is_set():
                            self._update_task(
                                state="cancelled",
                                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                message=f"管理员已取消 {target_label} 登录等待。",
                            )
                            return
                        # 兼容用户在同窗口新开标签页完成登录：依次检查主页面及上下文内其他已打开的亚马逊标签页
                        cand_pages = [page]
                        ctx_pages = getattr(getattr(page, "context", None), "pages", None) or []
                        for p_other in ctx_pages:
                            if p_other is page:
                                continue
                            try:
                                if callable(getattr(p_other, "is_closed", None)) and p_other.is_closed():
                                    continue
                                u_other = str(getattr(p_other, "url", "") or "").lower()
                                if market in u_other:
                                    cand_pages.append(p_other)
                            except Exception:  # noqa: BLE001
                                continue
                        logged_now = False
                        for cand in cand_pages:
                            if browser.check_amazon_login(cand, navigate=False):
                                logged_now = True
                                break
                        if logged_now:
                            confirm_streak += 1
                            if confirm_streak == 1:
                                self._update_task(
                                    message=f"已检测到 {target_label} 登录态，正在二次确认并保存共享态…"
                                )
                            if confirm_streak >= 2:
                                saved = amazon_login_share.save(
                                    page.context, self.profile_root(), market, **save_kw
                                )
                                if saved:
                                    self._set_check_result(market, "valid", f"{target_label} 登录态有效", slot_id=slot_id)
                                    self._update_task(
                                        state="succeeded",
                                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        message=f"{target_label} 登录确认成功！已发布加密共享登录态。",
                                    )
                                    return
                        else:
                            confirm_streak = 0
                        cancel_ev.wait(timeout=_POLL_INTERVAL)

                    self._update_task(
                        state="failed",
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        message=f"{target_label} 登录在 {wait_sec} 秒内未获确认，已关闭窗口；可增大等待时间后重试。",
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
                message=f"{target_label} 登录窗口启动或核验失败: {type(exc).__name__}: {exc}",
            )
