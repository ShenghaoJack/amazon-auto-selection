"""亚马逊多账号 + 静态住宅代理槽位池管理与跨进程租用。

每个槽位 = 1 个独立名称 + 1 个固定代理地址(加密存储) + 1 个独立浏览器 Profile + 1 份独立共享登录态。
- 未配置任何启用槽位时，系统 100% 退回原有单共享态 + 本机直连模式，零侵入；
- 配置了 1~N 个启用槽位时，不同类目自动租用不同空闲槽位，浏览器与 HTTP 直连统一走该槽位代理，
  且同一个槽位固定绑定 1 个亚马逊买家号 + 1 个静态住宅 IP，避免多号同 IP 关联与单 IP 过载。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core import amazon_login_share
from core.proxy_utils import mask_proxy_url, parse_proxy_url

_POOL_FILENAME = "amazon-proxy-pool.json"
_SLOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def _pool_path(profile_root: str | Path) -> Path:
    return Path(profile_root) / _POOL_FILENAME


def _locks_dir(profile_root: str | Path) -> Path:
    d = Path(profile_root) / ".amz-pool-locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slot_profile_dir(profile_root: str | Path, slot_id: str) -> Path:
    sid = normalize_slot_id(slot_id)
    p = Path(profile_root) / "amz-pool" / sid / "profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cat_slot_profile_dir(profile_root: str | Path, slot_id: str, cat: str) -> Path:
    """按 槽位 + 类目 隔离浏览器工作 Profile，既继承对应槽位共享登录态，又避免多类目轮用同槽位时目录锁冲突。"""
    sid = normalize_slot_id(slot_id)
    safe_cat = re.sub(r"[^A-Za-z0-9._-]+", "_", str(cat or "default").strip()).strip("._-") or "default"
    p = Path(profile_root) / "amz-pool" / sid / "cats" / safe_cat[:64]
    p.mkdir(parents=True, exist_ok=True)
    return p


def slot_for_crawl_index(
    profile_root: str | Path,
    market: str,
    crawl_slot_index: int,
) -> dict[str, Any] | None:
    """将 AmzCrawlSlot 的物理并发槽位号(1-based) 1:1 映射到该站点对应的启用代理槽位。"""
    if crawl_slot_index <= 0:
        return None
    candidates = enabled_slots_for_market(profile_root, market)
    if not candidates:
        return None
    chosen = dict(candidates[(int(crawl_slot_index) - 1) % len(candidates)])
    chosen["crawl_slot_index"] = int(crawl_slot_index)
    return chosen


def normalize_slot_id(slot_id: str) -> str:
    sid = str(slot_id or "").strip().lower()
    if not _SLOT_ID_RE.fullmatch(sid):
        raise ValueError("槽位 ID 仅支持小写字母、数字、短横线或下划线（1–48 字符）")
    return sid


def _normalize_market(market: str) -> str:
    m = str(market or "amazon.com").strip().lower()
    if m.startswith("www."):
        m = m[4:]
    if not re.fullmatch(r"amazon\.[a-z.]+", m):
        raise ValueError(f"无效的亚马逊站点: {market!r}")
    return m


def _encrypt_secret(plain: str) -> tuple[str, str]:
    """返回 (enc_b64, plain_fallback)。Windows 使用 DPAPI 加密，绝不落明文。"""
    if not plain:
        return "", ""
    if os.name == "nt":
        raw = amazon_login_share._crypt(plain.encode("utf-8"), decrypt=False)
        return base64.b64encode(raw).decode("ascii"), ""
    return "", plain


def _decrypt_secret(enc_b64: str, plain_fallback: str = "") -> str:
    if enc_b64:
        try:
            raw = base64.b64decode(enc_b64.encode("ascii"))
            return amazon_login_share._crypt(raw, decrypt=True).decode("utf-8")
        except Exception:
            return ""
    return str(plain_fallback or "")


def _load_raw(profile_root: str | Path) -> list[dict[str, Any]]:
    path = _pool_path(profile_root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        slots = data.get("slots", []) if isinstance(data, dict) else []
        return [s for s in slots if isinstance(s, dict) and s.get("slot_id")]
    except Exception:
        return []


def _save_raw(profile_root: str | Path, slots: list[dict[str, Any]]) -> None:
    path = _pool_path(profile_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "slots": slots}, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".amz-pool-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def list_slots(profile_root: str | Path, *, market: str | None = None) -> list[dict[str, Any]]:
    """返回面向前端/展示的槽位清单（不含明文代理密码，附带共享登录态发布状态）。"""
    root = Path(profile_root)
    norm_m = _normalize_market(market) if market else ""
    out: list[dict[str, Any]] = []
    for s in _load_raw(root):
        m = str(s.get("market") or "amazon.com").strip().lower()
        if norm_m and m != norm_m:
            continue
        sid = str(s.get("slot_id") or "").strip().lower()
        share_path = amazon_login_share._path(root, m, slot_id=sid)
        shared = share_path.is_file()
        login_updated = ""
        if shared:
            try:
                login_updated = datetime.fromtimestamp(share_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                shared = False
        proxy_plain = _decrypt_secret(str(s.get("proxy_enc") or ""), str(s.get("proxy_plain") or ""))
        out.append({
            "slot_id": sid,
            "name": str(s.get("name") or sid),
            "market": m,
            "enabled": bool(s.get("enabled", True)),
            "mode": "proxy" if proxy_plain else "local",
            "mode_label": "静态住宅代理" if proxy_plain else "当前服务器本地",
            "has_proxy": bool(proxy_plain),
            "proxy_masked": mask_proxy_url(proxy_plain) if proxy_plain else "当前服务器本机出口（无需IP）",
            "shared": shared,
            "login_updated_at": login_updated,
            "updated_at": str(s.get("updated_at") or ""),
        })
    return out


def get_slot(profile_root: str | Path, slot_id: str) -> dict[str, Any] | None:
    """读取单个槽位完整内部信息（含解密后的 proxy_url，仅供后端内部使用，禁止直接返回前端）。"""
    root = Path(profile_root)
    sid = normalize_slot_id(slot_id)
    for s in _load_raw(root):
        if str(s.get("slot_id") or "").strip().lower() == sid:
            m = str(s.get("market") or "amazon.com").strip().lower()
            proxy_url = _decrypt_secret(str(s.get("proxy_enc") or ""), str(s.get("proxy_plain") or ""))
            return {
                "slot_id": sid,
                "name": str(s.get("name") or sid),
                "market": m,
                "enabled": bool(s.get("enabled", True)),
                "proxy_url": proxy_url,
                "proxy_masked": mask_proxy_url(proxy_url) if proxy_url else "",
                "profile_dir": str(slot_profile_dir(root, sid)),
            }
    return None


def upsert_slot(
    profile_root: str | Path,
    *,
    slot_id: str = "",
    name: str = "",
    market: str = "amazon.com",
    proxy_url: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """新增或更新一个亚马逊代理/账号槽位。编辑时若 proxy_url 为 None 则保留原代理。"""
    root = Path(profile_root)
    m = _normalize_market(market)
    slots = _load_raw(root)

    if slot_id:
        sid = normalize_slot_id(slot_id)
    else:
        existing_ids = {str(s.get("slot_id") or "").strip().lower() for s in slots}
        idx = 1
        while f"slot-{idx}" in existing_ids:
            idx += 1
        sid = f"slot-{idx}"

    clean_name = str(name or "").strip() or sid
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = next((s for s in slots if str(s.get("slot_id") or "").strip().lower() == sid), None)
    if proxy_url is not None:
        raw_p = str(proxy_url).strip()
        if raw_p:
            # 校验代理格式合法性
            parse_proxy_url(raw_p)
        enc, plain = _encrypt_secret(raw_p)
    elif existing is not None:
        enc = str(existing.get("proxy_enc") or "")
        plain = str(existing.get("proxy_plain") or "")
    else:
        enc, plain = "", ""

    rec = {
        "slot_id": sid,
        "name": clean_name,
        "market": m,
        "proxy_enc": enc,
        "proxy_plain": plain,
        "enabled": bool(enabled),
        "created_at": str(existing.get("created_at") or now_str) if existing else now_str,
        "updated_at": now_str,
    }
    if existing is not None:
        slots = [rec if str(s.get("slot_id") or "").strip().lower() == sid else s for s in slots]
    else:
        slots.append(rec)
    _save_raw(root, slots)
    slot_profile_dir(root, sid)
    return next(x for x in list_slots(root) if x["slot_id"] == sid)


def delete_slot(profile_root: str | Path, slot_id: str) -> bool:
    root = Path(profile_root)
    sid = normalize_slot_id(slot_id)
    slots = _load_raw(root)
    new_slots = [s for s in slots if str(s.get("slot_id") or "").strip().lower() != sid]
    if len(new_slots) == len(slots):
        return False
    _save_raw(root, new_slots)
    # 清理对应的共享登录态文件
    for s in slots:
        if str(s.get("slot_id") or "").strip().lower() == sid:
            try:
                p = amazon_login_share._path(root, str(s.get("market") or "amazon.com"), slot_id=sid)
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
    return True


def enabled_slots_for_market(profile_root: str | Path, market: str) -> list[dict[str, Any]]:
    """返回指定站点下所有启用中的槽位（含解密后的 proxy_url）。"""
    root = Path(profile_root)
    try:
        m = _normalize_market(market)
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for s in _load_raw(root):
        if not bool(s.get("enabled", True)):
            continue
        if str(s.get("market") or "").strip().lower() != m:
            continue
        sid = str(s.get("slot_id") or "").strip().lower()
        proxy_url = _decrypt_secret(str(s.get("proxy_enc") or ""), str(s.get("proxy_plain") or ""))
        out.append({
            "slot_id": sid,
            "name": str(s.get("name") or sid),
            "market": m,
            "proxy_url": proxy_url,
            "proxy_masked": mask_proxy_url(proxy_url) if proxy_url else "",
            "profile_dir": str(slot_profile_dir(root, sid)),
        })
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _try_acquire_lock(lock_path: Path, owner_cat: str) -> bool:
    """尝试以原子方式获取槽位锁；允许同一 PID 重入，自动回收死进程残锁。"""
    my_pid = os.getpid()
    if lock_path.exists():
        try:
            info = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(info.get("pid") or 0)
            if pid == my_pid:
                return True
            if _pid_alive(pid):
                return False
            lock_path.unlink(missing_ok=True)
        except Exception:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                return False
    payload = json.dumps({"pid": my_pid, "cat": owner_cat, "ts": time.time()}, ensure_ascii=False)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return True
    except FileExistsError:
        return False


def acquire_slot_for_category(
    profile_root: str | Path,
    market: str,
    cat: str,
    *,
    preferred_slot_id: str = "",
) -> dict[str, Any] | None:
    """为指定类目/站点挑选并锁定一个可用槽位；若该站点未配置启用槽位返回 None。"""
    root = Path(profile_root)
    candidates = enabled_slots_for_market(root, market)
    if not candidates:
        return None
    locks = _locks_dir(root)

    # 排序：显式偏好槽位优先 → 按类目名稳定哈希起点轮询，保证同类目尽量固定同一 IP+账号
    if preferred_slot_id:
        pref = [c for c in candidates if c["slot_id"] == preferred_slot_id.strip().lower()]
        rest = [c for c in candidates if c["slot_id"] != preferred_slot_id.strip().lower()]
        ordered = pref + rest
    else:
        digest = int(hashlib.sha256(str(cat or "default").encode("utf-8")).hexdigest()[:8], 16)
        start = digest % len(candidates)
        ordered = candidates[start:] + candidates[:start]

    for slot in ordered:
        lp = locks / f"{slot['slot_id']}.lock"
        if _try_acquire_lock(lp, cat):
            out = dict(slot)
            out["lock_path"] = str(lp)
            return out

    # 所有槽位暂时都被其它存活进程占满时，仍返回首选槽位（并发仍受 AmzCrawlSlot 控制排队）
    out = dict(ordered[0])
    out["lock_path"] = ""
    return out


def release_slot_lock(lock_path: str | Path | None) -> None:
    if not lock_path:
        return
    p = Path(lock_path)
    try:
        if p.exists():
            info = json.loads(p.read_text(encoding="utf-8"))
            if int(info.get("pid") or 0) == os.getpid():
                p.unlink(missing_ok=True)
    except Exception:
        pass
