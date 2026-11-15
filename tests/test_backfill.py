# 单元测试: 断点续跑补同步(归档缺早期步骤修复)
import os

from core.config import Config
from core.orchestrator import PipelineRunner
from steps.registry import get_steps


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x\n")


def test_backfill_synced_steps(ws_tmp):
    """已 DONE 的早期步骤产物应被补同步进本轮归档。"""
    cfg = Config({"CAT": "test-cat", "DATA_ROOT": ws_tmp, "DRY_RUN": True})
    runner = PipelineRunner(cfg)
    steps = {s.number: s for s in get_steps()}

    # 步骤 0/1 已 DONE 且有产物; 步骤 2 尚未完成
    for num in (0, 1):
        step = steps[num]
        step.bind_cat(runner.ctx)
        for a in step.artifacts:
            _touch(runner.ctx.resolve_artifact(a))
        runner.state.start_step(num)
        runner.state.mark_done(num)
    runner.state.start_step(2)

    # 本轮归档目录
    snap = runner.paths.snapshot_dir()
    runner._snapshot = snap
    assert os.path.isdir(snap)

    # 补同步前: 归档里没有 00/01 产物
    assert not os.path.exists(os.path.join(snap, "00_榜单收集"))
    assert not os.path.exists(os.path.join(snap, "01_榜单详情与第一遍过滤"))

    runner._backfill_synced_steps(2)

    # 补同步后: 00/01 产物进入归档; 未完成步骤 2 不补
    step0, step1 = steps[0], steps[1]
    for a in step0.artifacts:
        name = os.path.basename(a)
        assert os.path.exists(os.path.join(snap, "00_榜单收集", name)), name
    for a in step1.artifacts:
        name = os.path.basename(a)
        assert os.path.exists(os.path.join(snap, "01_榜单详情与第一遍过滤", name)), name
    assert not os.path.exists(os.path.join(snap, "02_过滤提示词"))


def test_backfill_skips_not_finished(ws_tmp):
    """未完成/失败步骤不补同步(无产物, 幂等不报错)。"""
    cfg = Config({"CAT": "test-cat", "DATA_ROOT": ws_tmp, "DRY_RUN": True})
    runner = PipelineRunner(cfg)
    snap = runner.paths.snapshot_dir()
    runner._backfill_synced_steps(3)  # 无任何 DONE 步骤
    assert os.path.isdir(snap)
    assert not os.path.exists(os.path.join(snap, "00_榜单收集"))


def test_snapshot_all_includes_extra_archive_files(ws_tmp):
    """手动完整快照 snapshot_all: 步骤10 的 extra_archive_files(实际注入词清单
    <CAT>-injected-keywords.txt)存在时随步骤10 归档进 10_父项变体/, 缺失(无注入词)时跳过不报错。"""
    cfg = Config({"CAT": "test-cat", "DATA_ROOT": ws_tmp, "DRY_RUN": True})
    runner = PipelineRunner(cfg)
    step10 = {s.number: s for s in get_steps()}[10]
    step10.bind_cat(runner.ctx)
    # 写入 injected-keywords.txt(extra 产物存在场景)
    extra = step10.extra_archive_files[0]
    with open(runner.ctx.resolve_artifact(extra), "w", encoding="utf-8") as fh:
        fh.write("board\ncutting board\n")
    # 手动快照
    snap = runner.snapshot_all()
    tgt = os.path.join(snap, "10_父项变体", os.path.basename(extra))
    assert os.path.exists(tgt)
    with open(tgt, encoding="utf-8") as fh:
        assert "cutting board" in fh.read()


def test_snapshot_all_skips_missing_extra(ws_tmp):
    """extra 产物缺失(如无注入词时未写文件)不影响 snapshot_all。"""
    cfg = Config({"CAT": "test-cat", "DATA_ROOT": ws_tmp, "DRY_RUN": True})
    runner = PipelineRunner(cfg)
    snap = runner.snapshot_all()
    assert os.path.isdir(snap)  # 正常完成, 不抛错
