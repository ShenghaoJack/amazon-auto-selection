# 单元测试: FR-05 挖词把**人工核心词清单**一并挖(2026-09-29)
# ---------------------------------------------------------------------------
# 背景(用户诉求): 面板允许人工加核心词(手填新词/不达标词), 但那些词不在 FR-04 候选里 →
# FR-05 不挖它们 → 步骤8 关键词报告里它们的指标列全空。把人工核心词并入待挖清单后,
# 重跑 步骤5 → 7 → 8 就能得到"完整指标"的报告。
# 关键约束: **断点续挖**(已有记录按 keyword 跳过) → 只对真正的新词花 MCP 配额。
import json
import os
from pathlib import Path

from core.config import Config
from core.keyword_lists import core_manual_name, write_words
from core.paths import Paths
from steps.base import StepContext
from steps.step05_keyword_data import Step05KeywordData

CAT = "Kw Mine-US-202609"


def _make_ctx(tmp_dir: str, **extra) -> StepContext:
    data = {"CAT": CAT, "DATA_ROOT": tmp_dir, "DRY_RUN": True, "TREND_MONTH": "2026-08"}
    data.update(extra)
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _write_candidates(ctx: StepContext, words: list[str]) -> None:
    p = ctx.resolve_artifact(f"{CAT}-keywords-candidates.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(words) + "\n")


def _read_out(ctx: StepContext) -> list[dict]:
    return json.load(open(ctx.resolve_artifact(f"{CAT}-amazon-keyword-data.json"), encoding="utf-8"))


def _step(ctx: StepContext) -> Step05KeywordData:
    """按编排器的顺序: 先 validate_upstream(_cat 在这里设置) 再 run。"""
    s = Step05KeywordData()
    s.validate_upstream(ctx)
    return s


def test_step05_mines_manual_core_words_too(tmp_path):
    """人工核心词(不在候选里)也要被挖 → 报告才有指标。"""
    ctx = _make_ctx(tmp_path)
    _write_candidates(ctx, ["dog dryer", "pet dryer"])
    write_words(Path(ctx.paths.cat_root) / core_manual_name(CAT), ["blow dryer", "dog dryer"])
    _step(ctx).run(ctx)
    recs = _read_out(ctx)
    words = [r["keyword"] for r in recs]
    assert words == ["dog dryer", "pet dryer", "blow dryer"], "人工词要并入且去重(顺序: 候选在前)"
    manual_rec = [r for r in recs if r["keyword"] == "blow dryer"][0]
    assert manual_rec.get("status") == "ok"
    assert manual_rec.get("monthly_search_volume") is not None, "人工词的指标要有值(mock 也应给数)"


def test_step05_without_manual_list_unchanged(tmp_path):
    """没有人工清单时行为与旧版一致(只有候选)。"""
    ctx = _make_ctx(tmp_path)
    _write_candidates(ctx, ["a", "b"])
    _step(ctx).run(ctx)
    assert [r["keyword"] for r in _read_out(ctx)] == ["a", "b"]


def test_step05_manual_only_when_candidates_missing(tmp_path):
    """候选文件缺失但有人工清单 → 仍要挖人工词(旧实现会直接写空数组返回)。"""
    ctx = _make_ctx(tmp_path)
    write_words(Path(ctx.paths.cat_root) / core_manual_name(CAT), ["only manual"])
    _step(ctx).run(ctx)
    recs = _read_out(ctx)
    assert [r["keyword"] for r in recs] == ["only manual"]


def test_step05_resumes_and_skips_existing(tmp_path):
    """断点续挖: 已有记录(含刚挖的人工词)重跑时跳过 —— 只对新词花 MCP 调用。"""
    ctx = _make_ctx(tmp_path)
    _write_candidates(ctx, ["a"])
    write_words(Path(ctx.paths.cat_root) / core_manual_name(CAT), ["m1"])
    _step(ctx).run(ctx)
    first = _read_out(ctx)
    assert [r["keyword"] for r in first] == ["a", "m1"]

    # 第二轮: 再加一个人工词 → 只应新增它(其余保留原记录)
    write_words(Path(ctx.paths.cat_root) / core_manual_name(CAT), ["m1", "m2"])
    _step(ctx).run(ctx)
    second = _read_out(ctx)
    assert [r["keyword"] for r in second] == ["a", "m1", "m2"]
    assert second[0] == first[0] and second[1] == first[1], "已有记录应原样保留(未重复挖)"
