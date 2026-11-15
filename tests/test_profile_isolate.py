# -*- coding: utf-8 -*-
"""单元测试: 每类目独立亚马逊 Profile(core/profile_isolate, 2026-09-09)。

覆盖: 文件名安全化 / 克隆跳过缓存与锁文件 / 幂等(已存在不重复克隆) /
并发锁(另一进程持有 clone.lock 时等待而非双写) / 解析规则(默认共享路径→隔离;
自定义 AMZ_PROFILE→不隔离; 无 CAT / 占位类目→不隔离)。
"""
from __future__ import annotations

import os
from pathlib import Path

from core.config import Config
from core.profile_isolate import (
    clone_seed_profile,
    profile_slug,
    resolve_amz_profile_dir,
    resolve_ss_backup_profile_dir,
    resolve_ss_profile_dir,
)


def _make_seed(root: Path) -> Path:
    """构造带缓存与锁文件的假 Chrome profile(登录态=Preferences/Cookies/Network)。"""
    seed = root / "amz-profile"
    (seed / "Default").mkdir(parents=True)
    (seed / "Default" / "Cache" / "Cache_Data").mkdir(parents=True)   # 二级缓存(应跳过)
    (seed / "Cache").mkdir(parents=True)
    (seed / "Code Cache").mkdir(parents=True)
    (seed / "Network").mkdir(parents=True)
    (seed / "Preferences").write_text("{}", encoding="utf-8")
    (seed / "Default" / "Cookies").write_bytes(b"cookie-bytes")
    (seed / "Network" / "Cookies").write_bytes(b"net-cookie-bytes")
    (seed / "Cache" / "f_000001").write_bytes(b"cache-data")      # 应被跳过
    (seed / "Code Cache" / "js").write_bytes(b"js-cache")         # 应被跳过
    (seed / "Default" / "Cache" / "Cache_Data" / "data_0").write_bytes(b"x")
    (seed / "SingletonLock").write_text("lock", encoding="utf-8") # 应被跳过
    (seed / "lockfile").write_text("lock", encoding="utf-8")      # 应被跳过
    return seed


def test_profile_slug_sanitize():
    assert profile_slug("Cat Fountains-US-202608") == "Cat Fountains-US-202608"
    assert profile_slug("A/B:C") == "A_B_C"
    assert profile_slug("") == "cat"
    assert profile_slug("示例-") == "示例-"   # '-' 合法, 保留


def test_clone_tolerates_locked_file_and_tops_up_later(tmp_path, monkeypatch):
    """**实测踩到的坑**: Chrome 正开着时它的 Cookies/Sessions 被占用, copytree 抛
    `shutil.Error` 会把整个克隆废掉(于是"按类目隔离 profile"反而让流水线拿不到路径)。

    要求: ①个别文件被占用**不整体失败**; ②记进 `.clone-incomplete` 清单; ③下次调用
    (占用已释放)自动补齐并清掉清单。
    """
    import shutil

    from core.profile_isolate import _INCOMPLETE_MARKER, clone_seed_profile

    seed = _make_ss_seed(tmp_path)
    target = tmp_path / "ss-profile-CatA"
    locked_src = seed / "Default" / "Cookies"
    locked_dst = target / "Default" / "Cookies"

    real_copy2 = shutil.copy2
    state = {"busy": True}

    def _copy2(src, dst, *a, **kw):
        if Path(src) == locked_src and state["busy"]:
            raise OSError(32, "另一个程序正在使用此文件")     # 模拟被 Chrome 占用
        return real_copy2(src, dst, *a, **kw)

    def _copytree(src, dst, **kw):
        # 整体拷贝时报错(与实测一致: copytree 一旦有文件失败就抛 Error), 列出被占用项
        try:
            return real_copytree(src, dst, **kw)
        except Exception:  # noqa: BLE001
            raise shutil.Error([(str(locked_src), str(locked_dst), "WinError 32")])

    real_copytree = shutil.copytree
    monkeypatch.setattr(shutil, "copytree", _copytree)
    monkeypatch.setattr(shutil, "copy2", _copy2)

    assert clone_seed_profile(seed, target, retries=1, wait=0.0) is True, "被占用也不能整体失败"
    assert target.exists()
    marker = target / _INCOMPLETE_MARKER
    assert marker.is_file(), "未复制成功的文件要记清单"
    assert "Cookies" in marker.read_text(encoding="utf-8")
    assert not locked_dst.exists()

    # 占用释放 → 下次调用顺手补齐(幂等, 且不再重复克隆)
    state["busy"] = False
    assert clone_seed_profile(seed, target) is False, "已存在的 profile 不重复克隆"
    assert locked_dst.read_bytes() == b"ss-cookie", "占用释放后应补齐 Cookies"
    assert not marker.exists(), "补齐后清掉清单"


def test_clone_copies_login_state_skips_cache_and_locks(tmp_path):
    seed = _make_seed(tmp_path)
    target = tmp_path / "amz-profile-CatA"
    assert clone_seed_profile(seed, target) is True
    assert (target / "Preferences").exists()
    assert (target / "Default" / "Cookies").exists()
    assert (target / "Network" / "Cookies").exists()
    assert not (target / "Cache").exists()          # 缓存跳过
    assert not (target / "Code Cache").exists()     # 缓存跳过
    assert not (target / "Default" / "Cache").exists()   # 二级缓存跳过
    assert not (target / "SingletonLock").exists()   # 锁文件跳过
    assert not (target / "lockfile").exists()
    # 幂等: 目标已存在 -> 不重复克隆, 原内容不动
    (target / "marker.txt").write_text("keep", encoding="utf-8")
    assert clone_seed_profile(seed, target) is False
    assert (target / "marker.txt").read_text(encoding="utf-8") == "keep"


def test_clone_seed_missing_returns_false(tmp_path):
    target = tmp_path / "amz-profile-CatA"
    assert clone_seed_profile(tmp_path / "no-such-seed", target) is False
    assert not target.exists()


def test_clone_respects_concurrent_lock(tmp_path):
    seed = _make_seed(tmp_path)
    target = tmp_path / "amz-profile-CatA"
    lock = tmp_path / "amz-profile-CatA.clone.lock"
    # 锁被占用(时间戳很新, 非陈旧) -> 短重试后拿不到锁, 返回 False 且不写 target
    lock.write_text(f"{os.getpid()}\n{__import__('time').time():.3f}\n", encoding="utf-8")
    assert clone_seed_profile(seed, target, retries=2, wait=0.05) is False
    assert not target.exists()
    # 陈旧锁(>300s) -> 接管并克隆成功
    lock.write_text(f"{os.getpid()}\n{__import__('time').time() - 600:.3f}\n",
                    encoding="utf-8")
    assert clone_seed_profile(seed, target, retries=3, wait=0.05) is True
    assert (target / "Preferences").exists()


def test_resolve_isolates_default_shared_path(tmp_path):
    """AMZ_PROFILE 指向共享目录(真实类目 config 的写法) -> 解析为该类目独立目录并克隆种子。"""
    seed = _make_seed(tmp_path)
    cfg = Config({
        "CAT": "Cat Fountains-US-202608",
        "AMZ_PROFILE": str(seed),       # 真实类目 config 都显式写共享 amz-profile 路径
        "PROFILE_DIR": str(tmp_path),
        "DRY_RUN": True,
    })
    out = resolve_amz_profile_dir(cfg, tmp_path)
    assert out == (tmp_path / "amz-profile-Cat Fountains-US-202608").resolve()
    assert (tmp_path / "amz-profile-Cat Fountains-US-202608" / "Preferences").exists()
    # 种子里的共享目录本身没被改动(克隆是单向的)
    assert (seed / "Preferences").exists()


def test_resolve_respects_custom_amz_profile(tmp_path):
    """显式自定义 AMZ_PROFILE(≠共享目录) -> 不隔离, 原样返回。"""
    _make_seed(tmp_path)
    custom = tmp_path / "my-custom-profile"
    custom.mkdir()
    (custom / "Preferences").write_text("{}", encoding="utf-8")
    cfg = Config({
        "CAT": "Cat Fountains-US-202608",
        "AMZ_PROFILE": str(custom),
        "PROFILE_DIR": str(tmp_path),
    })
    out = resolve_amz_profile_dir(cfg, tmp_path)
    assert out == custom.resolve()
    assert not (tmp_path / "amz-profile-Cat Fountains-US-202608").exists()


def test_resolve_no_cat_or_placeholder_keeps_legacy(tmp_path):
    """无 CAT / 占位类目 -> 退回 AMZ_PROFILE 原路径, 不隔离不克隆。"""
    seed = _make_seed(tmp_path)
    cfg = Config({"CAT": "", "AMZ_PROFILE": str(seed), "PROFILE_DIR": str(tmp_path),
                  "DRY_RUN": True})
    assert resolve_amz_profile_dir(cfg, tmp_path) == seed.resolve()
    cfg2 = Config({"CAT": "xxx-测试", "AMZ_PROFILE": str(seed),
                   "PROFILE_DIR": str(tmp_path), "DRY_RUN": True})
    assert resolve_amz_profile_dir(cfg2, tmp_path) == seed.resolve()
    # 未显式配置 AMZ_PROFILE -> 用默认值。⚠ 默认值是**字面路径**
    #   （`data/profiles/amz-profile`，2026-10-12 由 `data/amz-profile` 升级而来），
    #   **不跟随 `PROFILE_DIR`** —— 所以这里它跟传进来的 `profile_root`(tmp_path) 对不上
    #   → 按 `core/profile_isolate.py:218` 的判据**不隔离**，原样返回。
    #   （生产环境 CWD=项目根，字面默认正好等于 profile_root/amz-profile → 才隔离。）
    cfg3 = Config({"CAT": "Cat Fountains-US-202608", "PROFILE_DIR": str(tmp_path)})
    assert resolve_amz_profile_dir(cfg3, tmp_path) == Path(cfg3.amz_profile_dir).resolve()


# ------------------------------------------------ 卖家精灵 Profile 也按类目隔离(2026-09-15)
def _make_ss_seed(root: Path, name: str = "ss-profile") -> Path:
    """假卖家精灵 profile(登录态 = Preferences/Cookies)。"""
    seed = root / name
    (seed / "Default").mkdir(parents=True)
    (seed / "Default" / "Cache").mkdir(parents=True)
    (seed / "Preferences").write_text("{}", encoding="utf-8")
    (seed / "Default" / "Cookies").write_bytes(b"ss-cookie")
    (seed / "Default" / "Cache" / "c").write_bytes(b"cache")
    (seed / "SingletonLock").write_text("lock", encoding="utf-8")
    return seed


def test_resolve_ss_isolates_default_shared_path(tmp_path):
    """SS_PROFILE 指向共享目录(真实类目 config 的写法) -> 该类目独立目录 + 克隆登录态。

    多类目并行的关键一步: 非 CDP 模式下两个类目同时 launch_persistent_context 同一个
    ss-profile 会被 Chrome profile 锁挡住(与亚马逊 profile 当年的问题一模一样)。
    """
    seed = _make_ss_seed(tmp_path)
    cfg = Config({
        "CAT": "Chopping Boards-UK-202608",
        "SS_PROFILE": str(seed),
        "PROFILE_DIR": str(tmp_path),
        "DRY_RUN": True,
    })
    out = resolve_ss_profile_dir(cfg, tmp_path)
    target = tmp_path / "ss-profile-Chopping Boards-UK-202608"
    assert out == target.resolve()
    assert (target / "Preferences").exists(), "登录态(Preferences)要克隆过来"
    assert (target / "Default" / "Cookies").read_bytes() == b"ss-cookie"
    assert not (target / "Default" / "Cache").exists(), "缓存目录应跳过"
    assert not (target / "SingletonLock").exists(), "旧锁文件应跳过"
    assert seed.exists(), "种子目录不受影响"


def test_resolve_ss_respects_custom_profile(tmp_path):
    """显式自定义 SS_PROFILE(≠共享目录) -> 不隔离(逃生舱)。"""
    _make_ss_seed(tmp_path)
    custom = tmp_path / "my-ss"
    custom.mkdir()
    cfg = Config({"CAT": "CatA", "SS_PROFILE": str(custom), "PROFILE_DIR": str(tmp_path)})
    assert resolve_ss_profile_dir(cfg, tmp_path) == custom.resolve()
    assert not (tmp_path / "ss-profile-CatA").exists()


def test_resolve_ss_no_cat_keeps_legacy(tmp_path):
    """无 CAT / 占位类目 -> 原路径(工具脚本场景)。"""
    seed = _make_ss_seed(tmp_path)
    cfg = Config({"CAT": "", "SS_PROFILE": str(seed), "PROFILE_DIR": str(tmp_path),
                  "DRY_RUN": True})
    assert resolve_ss_profile_dir(cfg, tmp_path) == seed.resolve()


def test_resolve_ss_backup_is_per_cat_and_cloned(tmp_path):
    """备用账号 profile: `<隔离主目录>-b<index>`, 并从既有 `<配置目录>-b<index>` 克隆。"""
    main_seed = _make_ss_seed(tmp_path)
    backup_seed = _make_ss_seed(tmp_path, "ss-profile-b1")
    cfg = Config({"CAT": "CatA", "SS_PROFILE": str(main_seed), "PROFILE_DIR": str(tmp_path),
                  "DRY_RUN": True})
    out = resolve_ss_backup_profile_dir(cfg, tmp_path, 1)
    assert out == (tmp_path / "ss-profile-CatA-b1").resolve()
    assert (out / "Default" / "Cookies").read_bytes() == b"ss-cookie", "备用账号登录态要克隆"
    # 没有对应的备用种子时: 目录照样给出(浏览器按账号密码自动登录)
    out2 = resolve_ss_backup_profile_dir(cfg, tmp_path, 2)
    assert out2 == (tmp_path / "ss-profile-CatA-b2").resolve()
    assert not out2.exists(), "不建空目录(交由浏览器创建)"
    assert backup_seed.exists()
    # 无 CAT 时不会克隆出类目目录
    assert not (tmp_path / "amz-profile-cat").exists()
