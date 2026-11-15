# -*- coding: utf-8 -*-
"""卖家精灵 profile 登录态 **自动备份 / 恢复**。

每个卖家精灵账号对应一个"种子 profile"：
  · 主账号   : data/profiles/ss-profile
  · 备用 N   : data/profiles/ss-profile-b<N>

各类目实际使用按类目隔离的副本（ss-profile-<CAT> / ss-profile-<CAT>-b<N>）。
本模块负责：
  · 登录成功后，把副本里的登录文件**备份回种子**；
  · 副本缺登录态时，从种子**恢复**。

为什么需要：`clone_seed_profile` 只在目标不存在时克隆一次；种子后来重新登录的
状态不会自动回灌 → 实测备用账号的 per-category profile 没有 `rank-login-user`，
步骤11 切号后"未登录"。

登录态文件（含 Chrome 的 cookie 解密密钥）：
  Local State              ← cookie 的 DPAPI 密钥，必须一起复制，否则目标解不开 cookie
  Default/Network/Cookies  ← 登录 cookie
  Default/Preferences      ← 偏好（含下载权限等）
  Default/Login Data       ← 保存的账号密码（可选）
  Default/Web Data         ← 自动填充/账号（可选）
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

_LOGIN_FILES = (
    "Local State",
    "Default/Preferences",
    "Default/Network/Cookies",
    "Default/Login Data",
    "Default/Web Data",
)

_COOKIE_REL = ("Default/Network/Cookies", "Default/Cookies")
_LOGIN_COOKIE = "rank-login-user"


def _cookie_db(profile) -> Path | None:
    base = Path(profile)
    for rel in _COOKIE_REL:
        p = base / rel
        if p.exists():
            return p
    return None


def login_ok(profile) -> bool:
    """该 profile 是否有卖家精灵登录态（存在 `rank-login-user` cookie）。"""
    db = _cookie_db(profile)
    if not db:
        return False
    tmp = None
    try:
        tmp = os.path.join(tempfile.gettempdir(), "ss_cookies_%d.db" % int(time.time() * 1e6))
        shutil.copy2(db, tmp)
        con = sqlite3.connect("file:%s?mode=ro" % tmp.replace("\\", "/"), uri=True)
        try:
            row = con.execute(
                "SELECT count(*) FROM cookies WHERE name=?", (_LOGIN_COOKIE,)
            ).fetchone()
        finally:
            con.close()
        return bool(row and row[0] > 0)
    except Exception:  # noqa: BLE001
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _copy_with_retry(src: Path, dst: Path, retries: int = 6, wait: float = 0.5) -> bool:
    """复制单个文件（Chrome 可能正持有源文件 → 失败重试几次）。"""
    for i in range(max(1, retries)):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(dst) + ".sync.tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            return True
        except Exception:  # noqa: BLE001
            if i < retries - 1:
                time.sleep(wait)
    return False


def sync_login(src, dst) -> bool:
    """把登录相关文件从 src 复制到 dst（逐个文件，失败跳过不整体报错）。"""
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        return False
    changed = False
    for rel in _LOGIN_FILES:
        s = src / rel
        if not s.exists():
            continue
        if _copy_with_retry(s, dst / rel):
            changed = True
    return changed


def seed_for(profile_dir, profile_root=None) -> Path | None:
    """按 profile 目录名推导该账号的种子 profile 路径。

    `ss-profile-<CAT>`      → `<root>/ss-profile`
    `ss-profile-<CAT>-b<N>` → `<root>/ss-profile-b<N>`
    其它（自定义路径）→ None（不自动备份，保持用户自定义行为）。
    """
    p = Path(profile_dir)
    root = Path(profile_root) if profile_root else p.parent
    name = p.name
    m = re.match(r"^ss-profile-(.+)-b(\d+)$", name)
    if m:
        return root / ("ss-profile-b%s" % m.group(2))
    if name.startswith("ss-profile-") and name != "ss-profile":
        return root / "ss-profile"
    return None


def backup_login(profile_dir, profile_root=None) -> bool:
    """登录成功 → 把 profile 的登录态备份回该账号的种子。"""
    seed = seed_for(profile_dir, profile_root)
    if seed is None or not seed.is_dir():
        return False
    if Path(seed).resolve() == Path(profile_dir).resolve():
        return False
    return sync_login(profile_dir, seed)


def restore_login(profile_dir, seed_dir) -> bool:
    """副本缺登录态 → 从种子恢复（种子有登录才恢复）。"""
    if not Path(seed_dir).is_dir():
        return False
    if login_ok(profile_dir):
        return False
    if not login_ok(seed_dir):
        return False
    return sync_login(seed_dir, profile_dir)
