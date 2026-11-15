# -*- coding: utf-8 -*-
"""步骤20(VOC 评论分析)接线测试。

覆盖: 步骤元数据/产物声明、启用开关、上游校验、配置透传(页数/预算/源顺序)、
产物质量校验。**不调用真实 MCP/LLM** —— `run_voc` 用 monkeypatch 替换。
"""
from __future__ import annotations

import json
import os

import openpyxl
import pytest

from core.config import Config
from core.errors import StepDataError
from core.paths import Paths
from steps import step19_voc as S19
from steps.base import StepContext


def _ctx(tmp_dir, extra: dict | None = None):
    data = {"CAT": "t", "DATA_ROOT": str(tmp_dir), "DRY_RUN": True, "VOC_OPPORTUNITY_ENABLED": False}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths), cfg


def _write_traffic(cat_dir: str) -> str:
    """造一张最小流量拆分表(父ASIN/变体ASIN/月销量/品牌/商品标题)。"""
    d = os.path.join(cat_dir, "14_流量拆分")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "流量拆分数据-需人工补充.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AI已补充"
    ws.append(["父ASIN", "变体ASIN", "月销量", "品牌", "商品标题"])
    ws.append(["B000C91625", "B09611AA37", 100, "X", "标题A"])
    ws.append(["B000C91625", "B0C83FA9D7", 50, "X", "标题A"])
    ws.append(["B04A8EAA04", "B05BE1F425", 900, "Y", "标题B"])
    wb.save(path)
    wb.close()
    return path


# ---------------------------------------------------------------- 元数据与声明
def test_metadata_and_step_dir():
    step = S19.Step19Voc()
    assert step.number == 19, "步骤号必须为 19(VOC 紧跟步骤18, 备份/体检移到 20 收尾)"
    assert "VOC" in step.title
    assert S19.STEP_DIR == "19_VOC报告"


def test_artifacts_and_archive_declarations(ws_tmp):
    ctx, _ = _ctx(ws_tmp)
    _write_traffic(str(ctx.paths.cat_root))
    step = S19.Step19Voc()
    step.validate_upstream(ctx)
    assert step.artifacts == ["19_VOC报告/t-VOC报告.xlsx"]
    assert step.archive_dirs == ["19_VOC报告"], "整目录随步骤归档(每步即时同步)"
    assert any(p.endswith(S19.SUMMARY_NAME) for p in step.extra_archive_files)


def test_paths_registers_step_dir_and_realtime(tmp_path):
    """core.paths 必须登记 19 → 19_VOC报告, 且在独立实时子目录集合里。"""
    from core.paths import REALTIME_SUBDIRS, STEP_DIR_NAMES
    assert STEP_DIR_NAMES[19] == "19_VOC报告"
    assert 19 in REALTIME_SUBDIRS


# ---------------------------------------------------------------- 开关与上游
def test_skip_when_disabled(ws_tmp):
    ctx_off, _ = _ctx(os.path.join(str(ws_tmp), "off"), {"VOC_ENABLED": False})
    assert S19.Step19Voc().skip_when(ctx_off) is True
    ctx_on, _ = _ctx(os.path.join(str(ws_tmp), "on"), {"DRY_RUN": False})
    assert S19.Step19Voc().skip_when(ctx_on) is False, "默认启用(非 dry_run)"


def test_skip_when_dry_run(ws_tmp):
    """dry_run 下必须跳过: mock 拿不到真实评论, 否则整条流水线的冒烟会被判 FAILED。"""
    ctx, _ = _ctx(ws_tmp)                       # _ctx 默认 DRY_RUN=True
    assert S19.Step19Voc().skip_when(ctx) is True


def test_validate_upstream_requires_one_of_two_inputs(ws_tmp):
    ctx, _ = _ctx(ws_tmp)
    with pytest.raises(StepDataError) as ei:
        S19.Step19Voc().validate_upstream(ctx)
    msg = str(ei.value)
    assert "14_流量拆分" in msg and "parents.txt" in msg


def test_validate_upstream_accepts_parents_txt(ws_tmp):
    ctx, _ = _ctx(ws_tmp)
    with open(os.path.join(str(ctx.paths.cat_root), "t-parents.txt"), "w", encoding="utf-8") as fh:
        fh.write("B000C91625\nB000000002\n")
    step = S19.Step19Voc()
    step.validate_upstream(ctx)
    assert step._traffic is None, "无流量拆分表 → 走 parents.txt 回退"


def test_validate_upstream_prefers_traffic_table(ws_tmp):
    ctx, _ = _ctx(ws_tmp)
    path = _write_traffic(str(ctx.paths.cat_root))
    step = S19.Step19Voc()
    step.validate_upstream(ctx)
    assert step._traffic and os.path.normcase(step._traffic) == os.path.normcase(path)


# ---------------------------------------------------------------- 配置透传 + 产物校验
def test_run_passes_config_values_to_pipeline(ws_tmp, monkeypatch):
    """页数按配置透传; **源顺序写死**、**父体与积分都不设上限**(旧键即使残留也必须被忽略)。"""
    ctx, _ = _ctx(ws_tmp, {"VOC_PAGES_PER_PARENT": 7,
                           "VOC_MAX_PARENTS": 3, "VOC_POINT_BUDGET": 500,   # 故意留两个陈旧键
                           # ★ 2026-10-12 起这两个键已移除；故意再留一份，验证**必须被忽略**
                           "VOC_SOURCE_PRIORITY": "sellersprite", "VOC_SITE": "amz_de",
                           "VOC_VALIDATE_STRICT": False})
    _write_traffic(str(ctx.paths.cat_root))
    captured = {}

    def fake_run_voc(cat_dir, config, **kw):
        captured.update(kw)
        out = kw["out_dir"]
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "t-VOC报告.xlsx"), "wb") as fh:
            fh.write(b"x" * 64)
        return {"parents": 714, "pages": 3570, "points": 17850, "unique_reviews": 120,
                "issues": 12, "drivers": 8, "validation_status": "PASS", "stopped_reason": ""}

    monkeypatch.setattr(S19.voc_pipeline, "run_voc", fake_run_voc)
    step = S19.Step19Voc()
    step.validate_upstream(ctx)
    step.run(ctx)

    assert captured["pages_per_parent"] == 7, "页数必须来自配置"
    assert captured["max_parents"] is None, "父体全量: VOC_MAX_PARENTS 旧键必须被忽略"
    assert "point_budget" not in captured or captured.get("point_budget") is None, \
        "不设费用熔断: VOC_POINT_BUDGET 旧键必须被忽略"
    assert captured["source_priority"] == S19.voc_pipeline.DEFAULT_SOURCE_PRIORITY, \
        "源顺序**写死在代码**：配置里的 VOC_SOURCE_PRIORITY 必须被忽略"
    assert captured["validate"] is False
    assert captured["meter"] is ctx.usage()
    assert captured["out_dir"].endswith(S19.STEP_DIR)
    assert captured["log"] is not None


def test_run_raises_when_report_missing(ws_tmp, monkeypatch):
    ctx, _ = _ctx(ws_tmp)
    _write_traffic(str(ctx.paths.cat_root))
    monkeypatch.setattr(S19.voc_pipeline, "run_voc", lambda *a, **k: {"unique_reviews": 0})
    step = S19.Step19Voc()
    step.validate_upstream(ctx)
    with pytest.raises(StepDataError) as ei:
        step.run(ctx)
    assert "VOC 工作簿未生成" in str(ei.value)


def test_validate_artifacts_checks_size_and_summary(ws_tmp):
    ctx, _ = _ctx(ws_tmp)
    step = S19.Step19Voc()
    out = os.path.join(str(ctx.paths.cat_root), S19.STEP_DIR)
    os.makedirs(out, exist_ok=True)
    xlsx = os.path.join(out, "t-VOC报告.xlsx")

    with pytest.raises(StepDataError):
        step.validate_artifacts(ctx)                     # 不存在

    with open(xlsx, "wb") as fh:
        fh.write(b"x" * 100)                              # 过小
    with pytest.raises(StepDataError):
        step.validate_artifacts(ctx)

    with open(xlsx, "wb") as fh:
        fh.write(b"x" * 20_000)
    with pytest.raises(StepDataError):
        step.validate_artifacts(ctx)                     # 缺摘要

    with open(os.path.join(out, S19.SUMMARY_NAME), "w", encoding="utf-8") as fh:
        json.dump({"unique_reviews": 0}, fh)
    with pytest.raises(StepDataError) as ei:
        step.validate_artifacts(ctx)
    assert "0 条唯一评论" in str(ei.value)

    with open(os.path.join(out, S19.SUMMARY_NAME), "w", encoding="utf-8") as fh:
        json.dump({"unique_reviews": 120, "validation_status": "PASS"}, fh)
    step.validate_artifacts(ctx)                          # 通过


def test_opportunity_chain_required_when_enabled(tmp_path):
    ctx, _ = _ctx(tmp_path, {"VOC_OPPORTUNITY_ENABLED": True})
    out = ctx.paths.cat_root / S19.STEP_DIR
    out.mkdir(parents=True)
    (out / "t-VOC报告.xlsx").write_bytes(b"x" * 20000)
    (out / S19.SUMMARY_NAME).write_text(json.dumps({"unique_reviews": 1}), encoding="utf-8")
    with pytest.raises(StepDataError, match="需求机会链"):
        S19.Step19Voc().validate_artifacts(ctx)
    chain_dir = out / "2_需求机会链"
    chain_dir.mkdir()
    (chain_dir / "chain.json").write_text(json.dumps({"reviews": [{"review_key": "US::R1"}]}), encoding="utf-8")
    summary_path = chain_dir / "需求机会链摘要.json"
    summary_path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    with pytest.raises(StepDataError, match="未完成"):
        S19.Step19Voc().validate_artifacts(ctx)
    summary_path.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    with pytest.raises(StepDataError, match="统一分类"):
        S19.Step19Voc().validate_artifacts(ctx)
    from tools.voc.classification import VERSION
    classification = {"status": "complete", "version": VERSION, "classified_aspects": 0}
    from tools.voc.opportunity import EXTRACTION_VERSION
    (chain_dir / "chain.json").write_text(json.dumps({"reviews": [{"review_key": "US::R1"}], "aspects": [],
                                                     "extraction": {"version": EXTRACTION_VERSION},
                                                     "relation_classification": {"version": "voc-relation-tags-v1", "status": "complete", "classified_needs": 0, "classified_opens": 0},
                                                     "classification": classification}), encoding="utf-8")
    summary_path.write_text(json.dumps({"status": "complete", "classification": classification}), encoding="utf-8")
    with pytest.raises(StepDataError, match="步骤18关联核查"):
        S19.Step19Voc().validate_artifacts(ctx)
    from tests.test_voc_solutions import make_catalog
    from tools.voc.solutions import VERSION as SOLUTION_VERSION
    catalog = make_catalog(ctx.paths.cat_root, "t")
    chain_path = chain_dir / "chain.json"
    updated = json.loads(chain_path.read_text(encoding="utf-8"))
    updated["solution_check"] = {"version": SOLUTION_VERSION, "status": "complete", "mapped_needs": 0,
                                 "checked_solutions": 0, "source": catalog["source"]}
    from tools.voc.identity import admit_reviews, bind_identities
    from tests.test_voc_opportunity import row
    _, _, _, admission = admit_reviews([row()], catalog, "US")
    updated["reviews"][0]["variant_asin"] = "V"
    for name in ("needs", "solutions", "opportunities", "coverage"):
        updated.setdefault(name, [])
    bind_identities(updated, catalog, admission)
    chain_path.write_text(json.dumps(updated), encoding="utf-8")
    summary_path.write_text(json.dumps({"status": "complete", "classification": classification,
                                      "solution_check": updated["solution_check"],
                                      "identity_association": updated["identity_association"],
                                      "admission_summary": admission}), encoding="utf-8")
    S19.Step19Voc().validate_artifacts(ctx)
    chain_path = chain_dir / "chain.json"
    old = json.loads(chain_path.read_text(encoding="utf-8"))
    old.pop("extraction")
    chain_path.write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(StepDataError, match="变体ASIN"):
        S19.Step19Voc().validate_artifacts(ctx)
