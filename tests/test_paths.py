# 单元测试: 归档目录复用/新建策略 (3.4 数据保存要求)
import os

from core.config import Config
from core.paths import Paths


def _paths(ws_tmp, cat="test-cat") -> Paths:
    cfg = Config({"CAT": cat, "DATA_ROOT": ws_tmp})
    return Paths(cfg)


def test_snapshot_dir_reuses_latest(ws_tmp):
    p = _paths(ws_tmp)
    first = p.snapshot_dir()
    second = p.snapshot_dir()
    assert first == second  # 断点续跑/重跑复用最新归档, 不重复新建
    assert os.path.isdir(first)
    assert os.path.basename(first).startswith("test-cat-")


def test_new_snapshot_dir_always_new(ws_tmp):
    p = _paths(ws_tmp)
    os.makedirs(os.path.join(ws_tmp, "archive", "test-cat-20260901-000000"))
    a = p.new_snapshot_dir()
    b = p.new_snapshot_dir()
    assert a != b  # 同一秒内连续调用也不重名
    assert os.path.isdir(a) and os.path.isdir(b)
    assert os.path.basename(a) != "test-cat-20260901-000000"
    assert os.path.basename(b) != "test-cat-20260901-000000"


def test_latest_snapshot_by_name(ws_tmp):
    p = _paths(ws_tmp)
    arc = os.path.join(ws_tmp, "archive")
    os.makedirs(os.path.join(arc, "test-cat-20260901-090000"))
    os.makedirs(os.path.join(arc, "test-cat-20260901-110000"))
    os.makedirs(os.path.join(arc, "other-cat-20260901-120000"))
    assert p.latest_snapshot() == os.path.join(arc, "test-cat-20260901-110000")


def test_latest_snapshot_does_not_match_prefix_category(ws_tmp):
    """**串档回归钉子(2026-09-15)**: 类目名互为前缀时不能把别类目的归档当自己的。

    实测: `CAT=Cutting-Boards` 用 `startswith(cat + "-")` 会命中
    `Cutting-Boards-US-202608-…`(而 `data/<基类目>-<站点>-<月份>` 正是本项目常见命名),
    后果是本轮产物被归档/同步进**另一个类目**的归档目录。
    """
    p = _paths(ws_tmp, "Cutting-Boards")
    arc = os.path.join(ws_tmp, "archive")
    os.makedirs(os.path.join(arc, "Cutting-Boards-20260914-090000"))          # 自己的
    os.makedirs(os.path.join(arc, "Cutting-Boards-US-202608-20260915-120000"))  # 别类目的(前缀命中)
    os.makedirs(os.path.join(arc, "Cutting-Boards-draft-20260915-130000"))    # 名字像但非时间戳
    got = p.latest_snapshot()
    assert got == os.path.join(arc, "Cutting-Boards-20260914-090000"), \
        f"只能认自己的归档, 实际 {os.path.basename(got or '')}"
    # snapshot_dir()(断点续跑复用最新归档)同样不能复用到别类目头上
    assert p.snapshot_dir() == got
    # 带 -N 后缀(同一秒重复新建)的自己的目录仍要认
    os.makedirs(os.path.join(arc, "Cutting-Boards-20260916-100000"))
    os.makedirs(os.path.join(arc, "Cutting-Boards-20260916-100000-1"))
    assert os.path.basename(p.latest_snapshot()) == "Cutting-Boards-20260916-100000-1"


def test_snapshot_dir_forced_timestamp(ws_tmp):
    p = _paths(ws_tmp)
    snap = p.snapshot_dir("20260901-080000")
    assert snap.endswith(os.path.join("archive", "test-cat-20260901-080000"))
    assert os.path.isdir(snap)
    # 之后无参调用复用这个(按名字最新)目录
    assert p.snapshot_dir() == snap


def test_new_snapshot_dir_ignores_latest(ws_tmp):
    """显式新建绝不复用已有目录: 即使最新目录存在也新建。"""
    p = _paths(ws_tmp)
    latest = p.snapshot_dir()
    fresh = p.new_snapshot_dir()
    assert fresh != latest
    assert os.path.isdir(latest) and os.path.isdir(fresh)
