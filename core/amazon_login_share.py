"""Per-market Amazon cookies, encrypted for the current Windows user."""
from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
from ctypes import wintypes
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise OSError('共享登录态仅支持 Windows 用户加密')
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise OSError('Windows 用户加密操作失败')
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _normalize_slot_id(slot_id: str | None) -> str:
    s = str(slot_id or "").strip().lower()
    if not s:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", s):
        raise ValueError("无效的亚马逊槽位 ID")
    return s


def _path(profile_root, market, slot_id: str = ""):
    market = str(market or '').strip().lower()
    if not re.fullmatch(r'amazon\.[a-z.]+', market):
        raise ValueError('无效的亚马逊站点')
    sid = _normalize_slot_id(slot_id)
    filename = f"{market}__{sid}.bin" if sid else (market + '.bin')
    return Path(profile_root) / 'amazon-login-share' / filename


def save(context, profile_root, market, *, replace=True, slot_id: str = ""):
    """Called only after a positive login check; never stores other sites."""
    market = str(market or '').strip().lower()
    path = _path(profile_root, market, slot_id=slot_id)
    if not replace and path.exists():
        return False
    cookies = [c for c in context.cookies() if str(c.get('domain', '')).lstrip('.').lower() == market
               or str(c.get('domain', '')).lstrip('.').lower().endswith('.' + market)]
    if not any(re.match(r'^(?:at-|sess-at-)', str(c.get('name', ''))) and c.get('value') for c in cookies):
        return False
    encrypted = _crypt(json.dumps(cookies, ensure_ascii=False).encode('utf-8'))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.amazon-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(encrypted)
        if not replace:
            # Avoid replacing a newer successful login saved by another process.
            try:
                os.link(temp, path)
            except FileExistsError:
                return False
        else:
            os.replace(temp, path)
        return True
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def restore(context, profile_root, market, *, slot_id: str = ""):
    market = str(market or '').strip().lower()
    path = _path(profile_root, market, slot_id=slot_id)
    if not path.is_file():
        return False
    cookies = json.loads(_crypt(path.read_bytes(), decrypt=True).decode('utf-8'))
    if not isinstance(cookies, list) or not cookies:
        return False
    # Ignore any unrelated domains even if a state file was tampered with.
    cookies = [c for c in cookies if isinstance(c, dict) and
               (str(c.get('domain', '')).lstrip('.').lower() == market or
                str(c.get('domain', '')).lstrip('.').lower().endswith('.' + market))]
    if not cookies:
        return False
    context.add_cookies(cookies)
    return True


def cookie_header(profile_root, market, *, slot_id: str = "") -> str:
    """从管理员发布的加密共享登录态拼出 HTTP 直连用的 `Cookie` 请求头（失败或缺失返回空串）。"""
    import time as _time
    market = str(market or '').strip().lower()
    if not market:
        return ''
    try:
        path = _path(profile_root, market, slot_id=slot_id)
        if not path.is_file():
            return ''
        cookies = json.loads(_crypt(path.read_bytes(), decrypt=True).decode('utf-8'))
    except (OSError, ValueError, TypeError):
        return ''
    if not isinstance(cookies, list):
        return ''
    now = _time.time()
    pairs: list[str] = []
    seen: set[str] = set()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        dom = str(c.get('domain', '')).lstrip('.').lower()
        if dom != market and not dom.endswith('.' + market):
            continue
        exp = c.get('expires', -1)
        try:
            if exp is not None and float(exp) > 0 and float(exp) <= now:
                continue
        except (TypeError, ValueError):
            pass
        name = str(c.get('name') or '').strip()
        val = str(c.get('value') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={val}")
    return "; ".join(pairs)


def resolve_cookie_header(config, *, slot_id: str = "") -> str:
    """解析当前运行生效的亚马逊 HTTP Cookie：显式配置 `COOKIE` 优先，留空时自动复用管理员加密共享态。"""
    if config is None:
        return ''
    explicit = str(config.get("COOKIE", "") or "").strip()
    if explicit:
        return explicit
    if getattr(config, "dry_run", False):
        return ''
    market = str(getattr(config, "market", "") or config.get("MARKET", "") or "").strip()
    if not market:
        return ''
    try:
        pdir = getattr(config, "profile_dir", "") or config.get("PROFILE_DIR", "") or "data/profiles"
        profile_root = Path(config.resolve_path(pdir)) if hasattr(config, "resolve_path") else Path(pdir)
    except Exception:
        profile_root = Path("data/profiles")
    sid = str(slot_id or config.get("AMZ_SLOT_ID", "") or "").strip()
    if sid:
        hdr = cookie_header(profile_root, market, slot_id=sid)
        if hdr:
            return hdr
    return cookie_header(profile_root, market)
