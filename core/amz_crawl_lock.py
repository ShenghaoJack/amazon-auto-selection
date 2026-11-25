# -*- coding: utf-8 -*-
"""跨类目亚马逊浏览器抓取并发限流信号量（OS 独占文件锁 + 公平队列 + 时间片让渡）。

背景(2026-11-22 多用户多类目并发实测):
  1) 同一本机出口 IP 下，3 个类目同时跑步骤 10 无头 Chrome 抓取，前 ~55 分钟
     （~1200 次请求、~22 req/min）100% 稳定，随后同步触发 IP 级 captcha；启用
     `AMZ_CRAWL_MAX_CONCURRENT=1` 后连续 4 小时 0 验证码。
  2) 实测同时发现纯无序抢锁会让大批量类目（如 2746 条子变体）独占数小时，饿死
     只剩 28 条即可收尾的邻居类目。因此增加两项公平调度机制：
     - **优先级 + FIFO 排队票据**：排队者写入 `queue/q-<prio>-<ts_ns>-<pid>-<uid>.ticket`，
       仅当排在自己之前的存活票据数 `< max_concurrent` 时才尝试抢槽；小批收尾任务
       （`remaining <= AMZ_CRAWL_SHORT_BATCH`，默认 50）享有高优先级（`priority=0`），
       常规任务（`priority=10`）严格按入队时间 FIFO；进程退出或心跳超时（60s）自动失效，
       绝不死锁。
     - **时间片主动让渡**：持有者在处理满 `AMZ_CRAWL_SLICE_SIZE`（默认 120 条）后，
       仅在 `has_waiters()` 检测到确有其他类目排队时主动落盘并释放槽位让下一位接手，
       随后重新入队续跑；若无其他排队者则保持单浏览器会话连续跑完，零额外开销。
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .run_lock import _LOCK_OFFSET, _read_lock_info, _try_lock, _unlock

_LOCK_DIR = Path(__file__).resolve().parent.parent / "data" / ".locks"
_POLL_SECONDS = 1.0
_LOG_EVERY_SECONDS = 30.0
_HEARTBEAT_SECONDS = 5.0
_STALE_TICKET_SECONDS = 60.0

DEFAULT_MAX_CONCURRENT = 1
DEFAULT_WAIT_SECONDS = 21600.0
DEFAULT_SLICE_SIZE = 120
DEFAULT_SHORT_BATCH = 50
DEFAULT_YIELD_PAUSE = 2.0
DEFAULT_IP_COOLDOWN_SECONDS = 300.0

PRIORITY_SHORT = 0
PRIORITY_NORMAL = 10

_TICKET_SEQ = itertools.count(1)
_ACTIVE_SLOT_TLS = threading.local()


def current_slot_index() -> int | None:
    """返回当前线程正在持有的亚马逊物理并发槽位号(1-based)，未持有时返回 None。"""
    stack = getattr(_ACTIVE_SLOT_TLS, "stack", None)
    if stack:
        return int(stack[-1])
    return None


def _push_active_slot(idx: int) -> None:
    stack = getattr(_ACTIVE_SLOT_TLS, "stack", None)
    if stack is None:
        stack = []
        _ACTIVE_SLOT_TLS.stack = stack
    stack.append(int(idx))


def _pop_active_slot(idx: int) -> None:
    stack = getattr(_ACTIVE_SLOT_TLS, "stack", None)
    if not stack:
        return
    for i in range(len(stack) - 1, -1, -1):
        if stack[i] == int(idx):
            stack.pop(i)
            return


def lock_dir() -> Path:
    env = str(os.environ.get("PIPELINE_AMZ_LOCK_DIR") or "").strip()
    return Path(env) if env else _LOCK_DIR


def _queue_dir() -> Path:
    return lock_dir() / "queue"


def _ip_cooldown_path() -> Path:
    return lock_dir() / "amazon-ip-cooldown.json"


def resolve_ip_cooldown_seconds(config) -> float:
    """解析触发连续 captcha 熔断后的跨类目本机 IP 共享冷却秒数（默认 300s=5分钟；0=关闭）。"""
    env = str(os.environ.get("PIPELINE_AMZ_IP_COOLDOWN_SECONDS") or "").strip()
    if env:
        try:
            return max(0.0, float(env))
        except ValueError:
            pass
    raw = config.get("AMZ_IP_COOLDOWN_SECONDS", DEFAULT_IP_COOLDOWN_SECONDS) if config is not None else DEFAULT_IP_COOLDOWN_SECONDS
    if raw is None or str(raw).strip() == "":
        return DEFAULT_IP_COOLDOWN_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_IP_COOLDOWN_SECONDS


def set_ip_cooldown(seconds: float, *, reason: str = "", label: str = "") -> float:
    """广播本机 IP 共享冷却截止时间（取与已有冷却的最大值），阻止其他排队类目立刻续打热 IP。"""
    sec = max(0.0, float(seconds))
    if sec <= 0:
        return 0.0
    now = time.time()
    new_until = now + sec
    path = _ip_cooldown_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            try:
                old = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                old_until = float(old.get("until") or 0.0)
                if old_until > new_until:
                    new_until = old_until
            except (OSError, ValueError, TypeError):
                pass
        payload = {
            "until": new_until,
            "pid": os.getpid(),
            "label": str(label or ""),
            "reason": str(reason or "captcha"),
            "set_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return max(0.0, new_until - now)


def remaining_ip_cooldown() -> tuple[float, dict]:
    """查询跨类目本机 IP 共享冷却剩余秒数；已过期自动清理。"""
    path = _ip_cooldown_path()
    if not path.is_file():
        return 0.0, {}
    try:
        info = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        until = float(info.get("until") or 0.0)
    except (OSError, ValueError, TypeError):
        return 0.0, {}
    rem = until - time.time()
    if rem <= 0:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return 0.0, {}
    return rem, info if isinstance(info, dict) else {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def resolve_max_concurrent(config) -> int:
    """解析跨类目亚马逊抓取并发上限（0 表示不限流；默认 1，配置了多代理槽位池时自动按启用槽位数并发）。"""
    env = str(os.environ.get("PIPELINE_AMZ_CRAWL_MAX_CONCURRENT") or "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    raw = config.get("AMZ_CRAWL_MAX_CONCURRENT") if config is not None else None
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_CONCURRENT
    # 未显式指定并发数时，若当前站点配置了多个启用的静态住宅代理槽位，自动按槽位数并发
    if config is not None and not getattr(config, "dry_run", False):
        try:
            from core import amazon_pool
            pdir = getattr(config, "profile_dir", "") or config.get("PROFILE_DIR", "") or "data/profiles"
            root = Path(config.resolve_path(pdir)) if hasattr(config, "resolve_path") else Path(pdir)
            market = str(getattr(config, "market", "") or config.get("MARKET", "") or "").strip()
            if market:
                n_slots = len(amazon_pool.enabled_slots_for_market(root, market))
                if n_slots >= 2:
                    return n_slots
        except Exception:
            pass
    return DEFAULT_MAX_CONCURRENT


def resolve_slice_size(config) -> int:
    """解析大批量时间片轮转条数（0 表示不切分让渡；默认 120）。"""
    env = str(os.environ.get("PIPELINE_AMZ_CRAWL_SLICE_SIZE") or "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    raw = config.get("AMZ_CRAWL_SLICE_SIZE", DEFAULT_SLICE_SIZE) if config is not None else DEFAULT_SLICE_SIZE
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SLICE_SIZE
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_SLICE_SIZE


def resolve_short_batch(config) -> int:
    """解析小批收尾优先门槛（remaining <= 该值优先拿槽；默认 50；0 表示关闭优先）。"""
    raw = config.get("AMZ_CRAWL_SHORT_BATCH", DEFAULT_SHORT_BATCH) if config is not None else DEFAULT_SHORT_BATCH
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SHORT_BATCH
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_SHORT_BATCH


class AmzCrawlSlot:
    """跨进程 N 槽位亚马逊抓取并发锁（支持优先级 + FIFO 队列与时间片让渡检测）。"""

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
        label: str = "",
        log: Callable[[str], None] | None = None,
        warn: Callable[[str], None] | None = None,
        poll: float = _POLL_SECONDS,
        enabled: bool = True,
        priority: int = PRIORITY_NORMAL,
        remaining: int = 0,
    ):
        self.max_concurrent = max(0, int(max_concurrent))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.label = str(label or "amazon-crawl")
        self.log = log
        self.warn = warn
        self.poll = max(0.05, float(poll))
        self.enabled = bool(enabled) and self.max_concurrent > 0
        self.priority = max(0, min(99, int(priority)))
        self.remaining = max(0, int(remaining))
        self.slot_index: int | None = None
        self.waited: float = 0.0
        self._fh = None
        self._path: Path | None = None
        self._ticket_path: Path | None = None
        self._ticket_id: str = ""

    def _slot_path(self, index: int) -> Path:
        return lock_dir() / f"amazon-crawl-slot-{index}.lock"

    def _slot_meta_path(self, index: int) -> Path:
        return lock_dir() / f"amazon-crawl-slot-{index}.meta.json"

    def _create_ticket(self) -> Path | None:
        try:
            qdir = _queue_dir()
            qdir.mkdir(parents=True, exist_ok=True)
            seq = next(_TICKET_SEQ)
            uid = uuid.uuid4().hex[:8]
            name = f"q-{self.priority:02d}-{time.time_ns():020d}-{os.getpid():08d}-{seq:06d}-{uid}.ticket"
            path = qdir / name
            payload = {
                "pid": os.getpid(),
                "label": self.label,
                "priority": self.priority,
                "remaining": self.remaining,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self._ticket_path = path
            self._ticket_id = name
            return path
        except OSError:
            self._ticket_path = None
            self._ticket_id = ""
            return None

    def _touch_ticket(self) -> None:
        tp = self._ticket_path
        if tp is None:
            return
        try:
            os.utime(tp, None)
        except OSError:
            pass

    def _remove_ticket(self) -> None:
        tp = self._ticket_path
        self._ticket_path = None
        self._ticket_id = ""
        if tp is not None:
            try:
                tp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _live_tickets(*, exclude_name: str = "") -> list[tuple[str, dict]]:
        """返回按 (priority, timestamp, pid, seq) 排好序的存活排队票据，顺带清理僵尸票据。"""
        qdir = _queue_dir()
        if not qdir.is_dir():
            return []
        now = time.time()
        live: list[tuple[str, dict]] = []
        try:
            entries = sorted(qdir.glob("q-*.ticket"), key=lambda p: p.name)
        except OSError:
            return []
        for p in entries:
            if exclude_name and p.name == exclude_name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            info: dict = {}
            try:
                raw = p.read_text(encoding="utf-8", errors="replace").strip()
                if raw:
                    info = json.loads(raw)
            except (OSError, ValueError):
                info = {}
            pid = int(info.get("pid") or 0) if isinstance(info, dict) else 0
            stale = (age > _STALE_TICKET_SECONDS) or (pid > 0 and not _pid_alive(pid))
            if stale:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            live.append((p.name, info if isinstance(info, dict) else {}))
        return live

    def _queue_position(self) -> tuple[int, int]:
        """返回 (排在自己前面的存活票据数, 总排队数)。"""
        if not self._ticket_id:
            return 0, 0
        live = self._live_tickets()
        ahead = 0
        for name, _ in live:
            if name < self._ticket_id:
                ahead += 1
        return ahead, len(live)

    def has_waiters(self) -> bool:
        """当前是否有其他任务正在排队等待亚马逊抓取槽位。"""
        if not self.enabled:
            return False
        return bool(self._live_tickets(exclude_name=self._ticket_id))

    def waiters_summary(self) -> str:
        """返回当前排队任务摘要，便于日志透明展示。"""
        live = self._live_tickets(exclude_name=self._ticket_id)
        if not live:
            return ""
        parts = []
        for _, info in live[:4]:
            lbl = info.get("label") or "?"
            rem = int(info.get("remaining") or 0)
            prio = int(info.get("priority") if info.get("priority") is not None else PRIORITY_NORMAL)
            tag = "优先收尾" if prio <= PRIORITY_SHORT else "排队"
            parts.append(f"{lbl}({tag}{f', 剩{rem}条' if rem else ''})")
        if len(live) > 4:
            parts.append(f"等共{len(live)}个任务")
        return "、".join(parts)

    def _try_acquire_any(self) -> bool:
        base = lock_dir()
        base.mkdir(parents=True, exist_ok=True)
        for idx in range(1, self.max_concurrent + 1):
            path = self._slot_path(idx)
            fh = None
            try:
                fh = open(path, "a+b")
                fh.seek(0, os.SEEK_END)
                if fh.tell() <= _LOCK_OFFSET:
                    fh.seek(_LOCK_OFFSET)
                    fh.write(b"\x00")
                    fh.flush()
                if _try_lock(fh):
                    meta_obj = {
                        "pid": os.getpid(),
                        "label": self.label,
                        "slot": idx,
                        "remaining": self.remaining,
                        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    payload = json.dumps(meta_obj, ensure_ascii=False).encode("utf-8")
                    fh.seek(0)
                    fh.write(payload.ljust(_LOCK_OFFSET, b" "))
                    fh.flush()
                    try:
                        self._slot_meta_path(idx).write_text(
                            json.dumps(meta_obj, ensure_ascii=False), encoding="utf-8"
                        )
                    except OSError:
                        pass
                    self._fh = fh
                    self._path = path
                    self.slot_index = idx
                    return True
                fh.close()
            except OSError:
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass
                continue
        return False

    def _holders_summary(self) -> str:
        parts = []
        for idx in range(1, self.max_concurrent + 1):
            meta_path = self._slot_meta_path(idx)
            path = self._slot_path(idx)
            info = {}
            try:
                if meta_path.is_file():
                    raw_meta = meta_path.read_text(encoding="utf-8", errors="replace").strip()
                    if raw_meta.startswith("{"):
                        info = json.loads(raw_meta)
            except (OSError, ValueError):
                info = {}
            if not info:
                try:
                    with open(path, "rb") as fh:
                        raw = fh.read(_LOCK_OFFSET).decode("utf-8", errors="replace").strip()
                    if raw.startswith("{"):
                        info = json.loads(raw)
                    else:
                        info = _read_lock_info(path)
                except (OSError, ValueError):
                    info = {}
            if info:
                parts.append(f"槽{idx}:{info.get('label') or '?'}(pid={info.get('pid') or '?'})")
        return "、".join(parts) if parts else "其他类目任务"

    def acquire(self) -> "AmzCrawlSlot":
        if not self.enabled:
            return self
        start = time.monotonic()
        last_log = 0.0
        last_touch = start
        announced = False
        self._create_ticket()
        try:
            while True:
                now = time.monotonic()
                if (now - last_touch) >= _HEARTBEAT_SECONDS:
                    self._touch_ticket()
                    last_touch = now
                cd_rem, cd_info = remaining_ip_cooldown()
                ahead, total_q = self._queue_position()
                if cd_rem <= 0 and ahead < self.max_concurrent and self._try_acquire_any():
                    self.waited = time.monotonic() - start
                    self._remove_ticket()
                    if self.slot_index is not None:
                        _push_active_slot(self.slot_index)
                    if announced and self.log:
                        self.log(
                            f"已获得亚马逊抓取并发槽位({self.slot_index}/{self.max_concurrent}, "
                            f"排队等待 {self.waited:.0f} 秒), 开始抓取"
                        )
                    return self
                elapsed = time.monotonic() - start
                if self.wait_seconds > 0 and elapsed >= self.wait_seconds:
                    msg = (
                        f"等待亚马逊抓取并发槽位超时({self.wait_seconds:.0f}秒, 上限 {self.max_concurrent}); "
                        f"当前占用: {self._holders_summary()}"
                    )
                    if self.warn:
                        self.warn(msg)
                    raise TimeoutError(msg)
                if not announced or (now - last_log) >= _LOG_EVERY_SECONDS:
                    announced = True
                    last_log = now
                    if self.log:
                        pos_txt = f" | 队列位次 {ahead + 1}/{max(total_q, ahead + 1)}"
                        if self.priority <= PRIORITY_SHORT:
                            pos_txt += "(小批优先)"
                        if cd_rem > 0:
                            src = cd_info.get("label") or "其他类目"
                            rsn = cd_info.get("reason") or "captcha"
                            self.log(
                                f"本机 IP 共享冷却中(剩余 {cd_rem:.0f}s，触发源: {src}/{rsn})，"
                                f"暂停抓取等待 IP 恢复(已等 {elapsed:.0f}s){pos_txt}"
                            )
                        else:
                            self.log(
                                f"亚马逊抓取并发限流(上限 {self.max_concurrent}): 槽位已满，排队等待中"
                                f"(已等 {elapsed:.0f}s){pos_txt} | 当前占用: {self._holders_summary()}"
                            )
                time.sleep(self.poll)
        except BaseException:
            self._remove_ticket()
            raise

    def release(self) -> None:
        self._remove_ticket()
        idx = self.slot_index
        if idx is not None:
            _pop_active_slot(idx)
        if idx is not None and self._fh is not None:
            try:
                self._slot_meta_path(idx).unlink(missing_ok=True)
            except OSError:
                pass
        fh = self._fh
        self._fh = None
        if fh is not None:
            try:
                _unlock(fh)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass
        self.slot_index = None

    def __enter__(self) -> "AmzCrawlSlot":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
