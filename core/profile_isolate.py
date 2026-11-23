# -*- coding: utf-8 -*-
"""每类目独立亚马逊 Profile(2026-09-09 多类目并行)。

背景: 多类目并行跑时若所有类目共用同一个 AMZ_PROFILE(默认 data/profiles/
amz-profile), 两个进程会同时 launch_persistent_context 同一个 user-data-dir →
后启动方被 Chrome profile 锁拒之门外(浏览器补抓会话失败, 残余变体被舍弃)。

方案(2026-09-09 用户选定: 先复制代码到副本用 CLI 双终端验证并行):
  * **默认按类目隔离**: 生效的 amazon profile = <profiles>/amz-profile-<CAT>;
  * 某类目**首次**用到浏览器时, 若其目录不存在且共享种子 data/profiles/
    amz-profile 存在, 则整目录克隆一份(保留已登录态, 免重新登录);
    克隆跳过缓存子目录与 Chrome 锁文件, 并用独占锁防并发重复克隆;
  * 显式把 AMZ_PROFILE 配成**非默认**路径 = 用户自定义, 保持原行为(逃生舱);
  * 无 CAT / 占位类目(工具/脚本场景)不做隔离, 退回配置路径。
"""
from __future__ import annotations

import logging
import hashlib
import os
import re
import shutil
import time
from pathlib import Path

from .category_config import is_placeholder_cat

# 克隆时跳过的 Chrome 缓存子目录(登录态不需要; 单 profile 内通常占几十~几百MB)
_SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
    "Service Worker", "CacheStorage", "databases", "File System",
}
# 跳过的锁/临时文件(目标由新进程接管, 旧锁不复制)
_SKIP_FILES = {"SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"}
# 克隆时"有文件被占用没复制成功"的清单(下次调用会顺手补齐, 见 _top_up_incomplete)
_INCOMPLETE_MARKER = ".clone-incomplete"

_WIN_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_CLONE_LOCK_STALE_SEC = 300.0


def profile_slug(cat: str) -> str:
    """类目 -> 文件名安全 token(只替换 Windows 非法字符, 尽量保留可读性)。"""
    slug = _WIN_UNSAFE.sub("_", str(cat)).strip().rstrip(". ")
    return slug or "cat"


def _clone_ignore(directory: str, names: list[str]) -> set[str]:
    """copytree 忽略回调: 过滤锁/临时文件 + 任意深度的缓存目录。

    注意 Default/Cache、Default/Code Cache 等二级缓存也在 _SKIP_DIRS 内,
    只跳过"名字匹配且为目录"的项(避免误伤同名普通文件)。
    """
    dropped = {n for n in names if n in _SKIP_FILES}
    for n in names:
        if n in _SKIP_DIRS and (Path(directory) / n).is_dir():
            dropped.add(n)
    return dropped


def _copy_tolerant(seed: Path, target: Path, retries: int = 3, wait: float = 0.5) -> list[tuple]:
    """`copytree` 的容错版: **个别文件被占用不整体失败**(2026-09-15)。

    实测踩到: 卖家精灵 Chrome(CDP 直连/start_ss_chrome)正开着时, 它的
    `Default/Network/Cookies`、`Default/Sessions/*` 被进程占用, `shutil.copytree` 会抛
    `shutil.Error` 并把**整个克隆**废掉 —— 于是"按类目隔离 profile"这一步反而让流水线
    拿不到 profile 路径而失败。这里改为: 整体拷贝 → 失败的少数文件逐个重试 → 仍失败则
    **告警并跳过**(该 profile 只是少了这几个文件, 通常无碍; CDP 模式下浏览器用的是
    用户那个 Chrome, profile 差异不影响)。

    返回仍未复制的 [(源, 目标, 原因), ...](空列表=全部成功)。
    """
    try:
        shutil.copytree(seed, target, dirs_exist_ok=True, ignore=_clone_ignore)
        return []
    except shutil.Error as exc:
        failed = exc.args[0] if exc.args and isinstance(exc.args[0], list) else []
    still: list[tuple] = []
    for item in failed:
        try:
            src, dst, why = item
        except (TypeError, ValueError):
            continue
        copied = False
        for _ in range(max(1, int(retries))):
            try:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied = True
                break
            except OSError:
                time.sleep(max(0.0, float(wait)))
        if not copied:
            still.append((src, dst, why))
    if still:
        logging.getLogger(__name__).warning(
            "profile 克隆: %d 个文件被占用未能复制(来源 profile 可能正被 Chrome 使用), "
            "已跳过: %s", len(still), ", ".join(Path(s).name for s, _, _ in still[:6]))
    return still


def _write_incomplete_marker(target: Path, seed: Path, still: list[tuple]) -> None:
    """记录"克隆时被占用没复制成功"的相对路径, 供下次补齐(全部成功则清掉标记)。"""
    marker = target / _INCOMPLETE_MARKER
    if not still:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return
    rels = []
    for src, _dst, _why in still:
        try:
            rels.append(str(Path(src).relative_to(seed)))
        except ValueError:
            rels.append(Path(src).name)
    try:
        marker.write_text("\n".join(rels) + "\n", encoding="utf-8")
    except OSError:
        pass


def _top_up_incomplete(target: Path, seed: Path) -> None:
    """把上次因占用未复制的文件补齐(现在可能已释放 → 换机/关掉 Chrome 后自愈)。"""
    marker = target / _INCOMPLETE_MARKER
    if not marker.is_file() or not seed.is_dir():
        return
    try:
        rels = [ln.strip() for ln in marker.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    except OSError:
        return
    missing = []
    for rel in rels:
        src, dst = seed / rel, target / rel
        if dst.exists() or not src.is_file():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            missing.append(rel)
    try:
        if missing:
            marker.write_text("\n".join(missing) + "\n", encoding="utf-8")
        else:
            marker.unlink(missing_ok=True)
            logging.getLogger(__name__).info("profile 克隆已补齐: %s", target.name)
    except OSError:
        pass


def clone_seed_profile(seed: Path, target: Path,
                       retries: int = 120, wait: float = 0.5) -> bool:
    """种子 profile -> 每类目独立目录(仅当 target 不存在且 seed 存在时克隆)。

    返回是否执行了克隆:
    - seed 不存在 -> False(target 由调用方 mkdir, 浏览器按既有 COOKIE 导入登录);
    - target 已存在 -> False(不重复克隆; 但若上次留了"未复制清单"标记, 会**顺手补齐**
      那几个当时被 Chrome 占用的文件);
    - 跨进程并发 -> 独占锁 <target>.clone.lock(pid+时间戳), 冲突等待复查,
      最多 retries*wait 秒(默认 60s); 陈旧锁(>300s)直接接管。
    - 个别文件被占用 -> **不整体失败**(见 `_copy_tolerant`), 记进清单下次补。
    """
    if target.exists():
        _top_up_incomplete(target, seed)
        return False
    if not seed.is_dir():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / (target.name + ".clone.lock")
    acquired = False
    for _ in range(max(1, int(retries))):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode("utf-8"))
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            age = 0.0
            try:
                age = time.time() - float(lock.read_text(encoding="utf-8-sig").splitlines()[1])
            except Exception:  # noqa: BLE001
                pass
            if age > _CLONE_LOCK_STALE_SEC:
                try:
                    lock.unlink(missing_ok=True)
                    continue
                except OSError:
                    pass
            time.sleep(max(0.0, float(wait)))
    if not acquired:
        return False
    try:
        if target.exists() or not seed.is_dir():
            return False
        target.mkdir(parents=True, exist_ok=True)
        still = _copy_tolerant(seed, target)
        _write_incomplete_marker(target, seed, still)
        return True
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_amz_profile_dir(config, profile_root) -> Path:
    """返回该类目实际使用的 amazon profile 目录(默认按类目隔离 + 首用克隆)。

    config: 任意含 cat / amz_profile_dir 属性的配置对象(core.config.Config);
    profile_root: data/profiles 目录(core.paths.Paths.profile_root)。
    """
    profile_root = Path(profile_root)
    shared = (profile_root / "amz-profile").resolve()
    configured = Path(config.amz_profile_dir).resolve()
    cat = str(getattr(config, "cat", "") or "").strip()
    raw_cfg = str(config.get("AMZ_PROFILE", "") or "").strip() if hasattr(config, "get") else ""
    is_default = (configured == shared) or (raw_cfg in ("", "data/profiles/amz-profile", r"data\profiles\amz-profile"))
    if (not cat) or is_placeholder_cat(cat) or (not is_default):
        return configured          # 自定义路径 / 工具脚本场景: 保持原行为
    target = profile_root / f"amz-profile-{profile_slug(cat)}"
    seed = shared if shared.is_dir() else configured
    clone_seed_profile(seed, target)
    return target.resolve()


def resolve_ss_profile_dir(config, profile_root) -> Path:
    """返回该类目实际使用的**卖家精灵** profile 目录(默认按类目隔离 + 首用克隆)。

    为什么卖家精灵 profile 也要按类目隔离(2026-09-15, 多类目并行):
      * **非 CDP 模式**(未配 `SS_CDP_URL`, 自己启动有头浏览器)下, 两个类目同时
        `launch_persistent_context` 同一个 `ss-profile` → 后到者被 Chrome profile 锁
        拒之门外(与亚马逊 profile 当年一模一样的问题);
      * CDP 模式共用一个 Chrome, 由 `core/ss_lock.py` 的全局锁串行化 —— 但 profile
        按类目隔离后, "一个类目用 CDP、另一个没用 CDP"的混跑场景也不会互相踩;
      * 显式把 `SS_PROFILE` 配成**非默认**路径 = 用户自定义, 保持原行为(逃生舱)。
    """
    profile_root = Path(profile_root)
    user = str(os.environ.get('PIPELINE_WORKBENCH_USER') or '').strip().casefold()
    if user:
        # Never clone a shared or category profile into a different user's
        # namespace: its cookies may belong to another SellerSprite account.
        return user_ss_profile_path(user)
    shared = (profile_root / "ss-profile").resolve()
    configured = Path(config.ss_profile_dir).resolve()
    cat = str(getattr(config, "cat", "") or "").strip()
    if (not cat) or is_placeholder_cat(cat) or configured != shared:
        return configured
    target = profile_root / f"ss-profile-{profile_slug(cat)}"
    clone_seed_profile(shared, target)
    try:
        # 副本已存在但缺登录态（克隆发生在种子登录之前）→ 从种子恢复
        from .ss_profile_sync import restore_login
        restore_login(target, shared)
    except Exception:  # noqa: BLE001
        pass
    return target.resolve()


def resolve_ss_backup_profile_dir(config, profile_root, index: int) -> Path:
    """卖家精灵**备用账号** profile: 在主 profile 隔离结果后加 `-b<index>`。

    种子取用户既有的 `<configured>-b<index>`(备用账号登录态), 存在则克隆到
    隔离后的 `<isolated>-b<index>`; 不存在则由浏览器按既有账号密码自动登录。
    """
    main = resolve_ss_profile_dir(config, profile_root)
    if str(os.environ.get('PIPELINE_WORKBENCH_USER') or '').strip():
        return main
    index = int(index)
    configured_backup = Path(f"{Path(config.ss_profile_dir).resolve()}-b{index}")
    target = Path(f"{main}-b{index}")
    if target.resolve() == configured_backup.resolve():
        return target
    clone_seed_profile(configured_backup, target)
    try:
        # 备用副本缺登录态（克隆时种子未登录 / 登录态过期）→ 从备用种子恢复
        from .ss_profile_sync import restore_login
        restore_login(target, configured_backup)
    except Exception:  # noqa: BLE001
        pass
    return target.resolve()


USER_SS_PROFILE_ROOT = Path(__file__).resolve().parent.parent / 'data' / 'profiles' / 'workbench-users'


def user_ss_profile_path(username: str) -> Path:
    """A stable private profile for a workbench user, independent of category."""
    name = str(username or '').strip().casefold()
    if not name:
        raise ValueError('工作台用户为空，不能分配用户 Profile')
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:24]
    return (USER_SS_PROFILE_ROOT / ('ss-user-' + digest)).resolve()
