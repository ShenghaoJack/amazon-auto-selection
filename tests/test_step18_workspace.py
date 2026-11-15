"""Real storage/HTTP/LLM-boundary tests; no production data or paid calls."""
import copy
import hashlib
import importlib.util
import io
import json
import time
from pathlib import Path

import openpyxl
import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step18_standardization import Step18Standardization
from tools.attr_stability.spec_io import make_annotation_contract
from tools.attr_stability.step18_workspace import Workspace, WorkspaceConflict, read_json


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    cat = "Workspace-US-202610"
    root = tmp_path / "data" / cat
    attrs = ["材质", "功率", "是否带加热", "温度调节"]
    spec = {"cat": cat, "status": "confirmed", "definition_revision": "test-v1",
            "attributes": [{"name": a, "hint": "按原文判断；功率统一为W"} for a in attrs]}
    definitions = {"vocab_status": "pending_confirm", "attributes": [
        {"name": "材质", "rule": "enum", "params": {"values": ["金属", "不锈钢", "ABS", "ABS塑料"]}},
        {"name": "功率", "rule": "numeric", "params": {"unit": "W", "quantity": "power"}},
        {"name": "是否带加热", "rule": "enum", "params": {"values": ["是", "否"]}},
        {"name": "温度调节", "rule": "enum", "params": {"values": ["3档", "低/中/高", "不适用"]}},
    ], "cross_checks": {"蕴含": [{"左": {"属性": "是否带加热", "含": ["否"]},
                 "右": {"属性": "温度调节", "含": ["不适用"]}, "说明": "无加热不应有加热档位"}]}}
    inputs = [{"xl_row": 2, "标题": "Stainless steel, 1800 W, 3 heat levels low medium high",
               "五点描述": "", "详细参数": "", "变体ASIN": "B000C91625", "图片链接": "https://example.com/a.jpg"},
              {"xl_row": 3, "标题": "ABS housing, 1800 W. No heater.", "五点描述": "", "详细参数": "",
               "变体ASIN": "B04A8EAA04", "图片链接": "https://example.com/b.jpg"}]
    values = [["金属", "18000 W", "否", "3档"], ["ABS", "1800 W", "否", "不适用"]]
    delivered = []
    for r, vals in zip(inputs, values):
        cells = dict(zip(attrs, vals))
        for name in attrs:
            cells[name + "_来源"] = "标题"
            cells[name + "_依据"] = r["标题"]
        delivered.append({"xl_row": r["xl_row"], "attributes": cells})
    contract = make_annotation_contract(spec, inputs, source_path="unused", input_mode="snapshot")
    contract["completed"] = True
    write(root / (cat + "-attribute-spec.json"), spec)
    rules = root / "workspace.rules.json"
    write(rules, definitions)
    write(root / "17_属性标注/input.json", {"rows": inputs})
    write(root / "17_属性标注/稳定性结果.json", {"deliverable": delivered, "cells": {}, "annotation_contract": contract})
    write(root / "18_标准化/词表聚类报告.json", {"protected": True})
    write(root / "18_标准化/词表聚类历史.json", {"protected": True})
    return Workspace(root, rules_path=rules)


def request(ws, attrs, requirement="按要求复核", **kwargs):
    return {"expected_version": ws.open()["version"], "requirement": requirement,
            "selection": {"attributes": attrs, **kwargs.pop("selection", {})}, "mode": "text", **kwargs}


def test_summary_reuses_verified_chain_without_changing_history(workspace, monkeypatch):
    workspace.open()
    merge(workspace, "材质", "金属", "钢")
    state = workspace.open()
    expected = [workspace._history_entry(v) for v in workspace.chain(state, limit=30)]
    original = workspace._read_version
    calls = []
    def counted(ident):
        calls.append(ident)
        return original(ident)
    monkeypatch.setattr(workspace, "_read_version", counted)
    summary = workspace.summary()
    assert summary["history"] == expected
    assert len(calls) == len(set(calls))


def test_job_list_cache_refreshes_when_file_changes(workspace, monkeypatch):
    from webui import step18_api
    from tools.attr_stability.step18_workspace import token
    ident = token("J")
    path = workspace._path("jobs", ident, "J")
    job = {"id": ident, "status": "done", "message": "one", "result": {"patches": []}}
    write(path, job)
    original = step18_api.job_status
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(step18_api, "job_status", counted)
    assert step18_api.list_jobs(workspace)[0]["message"] == "one"
    assert step18_api.list_jobs(workspace)[0]["message"] == "one"
    assert len(calls) == 1
    job.update(status="applied", message="new message")
    write(path, job)
    result = step18_api.list_jobs(workspace)[0]
    assert result["status"] == "applied" and result["message"] == "new message"
    assert len(calls) == 2


def test_summary_cache_invalidates_on_source_and_version_changes(workspace, monkeypatch):
    from webui import server
    state = workspace.open()
    original = workspace.summary
    calls = []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(workspace, "summary", counted)
    assert not server._std_workspace_summary(workspace)["stale"]
    assert not server._std_workspace_summary(workspace)["stale"]
    assert len(calls) == 1
    spec = json.loads(workspace.spec_path.read_text(encoding="utf-8"))
    spec["updated_for_test"] = True
    write(workspace.spec_path, spec)
    assert server._std_workspace_summary(workspace)["stale"]
    assert len(calls) == 2
    version_path = workspace._path("versions", state["version"], "V")
    version = json.loads(version_path.read_text(encoding="utf-8"))
    version["operation"]["requirement"] = "tampered"
    write(version_path, version)
    with pytest.raises(ValueError, match="校验失败"):
        server._std_workspace_summary(workspace)


@pytest.mark.parametrize("raw, expected", [
    ("300mL/day", "0.63 pints/day"),
    ("12L/day", "25.36 pints/day"),
    ("349mL/24h", "0.74 pints/day"),
    ("17 US gallons/day", "136.00 pints/day"),
    ("16 US fl oz/day", "1.00 pints/day"),
    ("50 US pints/day", "50.00 pints/day"),
    ("40oz/day", None), ("16 fl oz/day", None),
    ("17 gallons/day", None), ("50 pints/day", None),
    ("500ml/pack", None), ("500ml", None), ("150g/day", None),
    ("2-4L/day", None), ("未明确", None),
])
def test_daily_liquid_pints_requires_unambiguous_volume_and_period(raw, expected):
    from tools.attr_stability.step18_workspace import daily_liquid_pints
    assert daily_liquid_pints(raw) == expected


def test_daily_gallon_and_fl_oz_ask_model_and_verify():
    from tools.attr_stability.step18_workspace import model_daily_gallon_conversions
    class Model:
        def chat_json(self, prompt, **kwargs):
            assert "17 gallons/day" in prompt and "16 fl oz/day" in prompt
            assert "40oz/day" not in prompt
            return {"mappings": {"17 gallons/day": "136.00 pints/day",
                                  "16 fl oz/day": "999.00 pints/day"}}
    assert model_daily_gallon_conversions(Model(), ["17 gallons/day", "16 fl oz/day", "40oz/day"]) == {
        "17 gallons/day": "136.00 pints/day"}


def test_daily_oz_requires_matching_liquid_evidence():
    from tools.attr_stability.step18_workspace import evidenced_daily_oz_pints
    assert evidenced_daily_oz_pints("34oz/day", "Removes up to 34oz (1000ml) of moisture per day") == "2.13 pints/day"
    assert evidenced_daily_oz_pints("60oz/day", "extract up to 60 oz (1,800 ml) of moisture per day") == "3.75 pints/day"
    assert evidenced_daily_oz_pints("40oz/day", "up to 1.2L (40 oz) of excess water daily") == "2.50 pints/day"
    assert evidenced_daily_oz_pints("34oz/day", "34oz of moisture per day") is None
    assert evidenced_daily_oz_pints("35oz/day", "34oz (1000ml) of moisture per day") is None
    assert evidenced_daily_oz_pints("34oz/day", "34oz (1000ml) container") is None


def test_selected_numeric_range_maximum_and_assignment_keep_unit():
    from tools.attr_stability.step18_workspace import selected_numeric_range_mappings
    assert selected_numeric_range_mappings("取范围中的最大值", {"30-350 sq.ft"}) == {"30-350 sq.ft": "350 sq.ft"}
    assert selected_numeric_range_mappings("取最大值", {"209 to 350 Watts"}) == {"209 to 350 Watts": "350 Watts"}
    assert selected_numeric_range_mappings("取最大值", {"209 to 350 Watts"}, {"unit": "W", "quantity": "power"}) == {"209 to 350 Watts": "350W"}
    assert selected_numeric_range_mappings("改成350", {"30-350 sq.ft"}) == {"30-350 sq.ft": "350 sq.ft"}
    with pytest.raises(ValueError):
        selected_numeric_range_mappings("取范围中的最大值", {"不明确"})
    with pytest.raises(ValueError):
        selected_numeric_range_mappings("改成350", {"30-350 sq.ft", "2-3 cm"})


def test_rejudge_then_round_uses_selected_rows_and_preserves_unit(workspace, monkeypatch):
    from tools.attr_stability import step18_model
    def fake_rejudge(llm, requirement, mode, attrs, rows, state, progress, scope=None):
        return {(rows[0]["id"], "功率"): {"value": "12.36W", "source": "标题", "basis": "12.36W"}}, []
    monkeypatch.setattr(step18_model, "reannotate", fake_rejudge)
    req = request(workspace, ["功率"], "重判并保留1位小数", selection={"values": {"功率": ["18000 W"]}})
    preview = workspace.propose(req, llm=object())
    cells = [p for p in preview["patches"] if p["type"] == "cell"]
    assert len(cells) == 1 and cells[0]["after"]["value"] == "12.4W"


def test_rejudge_kg_then_round_does_not_assume_missing_units(workspace, monkeypatch):
    from tools.attr_stability import step18_model
    from tools.attr_stability.step18_workspace import rejudge_kg_decimal_requirement
    assert rejudge_kg_decimal_requirement("重判并统一单位为kg，保留2位小数", ["重量"])
    assert not rejudge_kg_decimal_requirement("重判并统一单位为kg，保留2位小数", ["功率"])
    def fake_rejudge(llm, requirement, mode, attrs, rows, state, progress, scope=None):
        return {(rows[0]["id"], attrs[0]): {"value": "1 lb", "source": "标题", "basis": "1 lb"},
                (rows[1]["id"], attrs[0]): {"value": "5", "source": "标题", "basis": "5"}}, []
    monkeypatch.setattr(step18_model, "reannotate", fake_rejudge)
    monkeypatch.setattr("tools.attr_stability.step18_workspace.rejudge_kg_decimal_requirement", lambda req, attrs: True)
    preview = workspace.propose(request(workspace, ["功率"], "重判并统一单位为kg，保留2位小数"), llm=object())
    cells = [p for p in preview["patches"] if p["type"] == "cell"]
    assert len(cells) == 1 and cells[0]["after"]["value"] == "0.45kg"
    assert any("单位" in warning["reason"] for warning in preview["warnings"])


def test_product_type_cannot_use_fallback_contradicting_visual_evidence():
    from tools.attr_stability.step18_model import _parse
    row = {"id": "2", "input": {}, "attributes": {"产品类型": {"value": "除湿机"}}}
    cell = {"value": "除湿机", "source": "图片", "basis": "图片显示为硅胶干燥剂包/盒，非电动除湿设备，按兜底写除湿机",
            "observed_product_type": "硅胶干燥剂包"}
    raw = {"rows": [{"row_id": "2", "attributes": {"产品类型": cell}}]}
    out, warnings = _parse(raw, [row], ["产品类型"], visual=True, visual_images=["image"])
    assert out["2", "产品类型"]["value"] == "硅胶干燥剂包"
    assert any("矛盾" in item["reason"] for item in warnings)
    cell.pop("observed_product_type")
    out, warnings = _parse(raw, [row], ["产品类型"], visual=True, visual_images=["image"])
    assert ("2", "产品类型") not in out
    assert any("保留原值待复核" in item["reason"] for item in warnings)


def test_visual_product_type_unknown_with_explicit_observation():
    from tools.attr_stability.step18_model import _parse
    row = {"id": "17", "input": {}, "attributes": {"产品类型": {"value": "除湿机"}}}
    cell = {"value": "未明确", "source": "图片", "basis":
        "图片显示为硅胶干燥剂盒（Wisesorb 40 Gram Silica Gel Desiccant Canister），属吸附类干燥剂，非除湿机；无任何除湿机细分类型证据"}
    raw = {"rows": [{"row_id": "17", "attributes": {"产品类型": cell}}]}
    out, warnings = _parse(raw, [row], ["产品类型"], visual=True, visual_images=["image"])
    assert out["17", "产品类型"]["value"] == "硅胶干燥剂盒"
    assert out["17", "产品类型"]["source"] == "图片"
    assert warnings
    cell["basis"] = "图片中商品可能是干燥剂盒，无法确定"
    out, _ = _parse(raw, [row], ["产品类型"], visual=True, visual_images=["image"])
    assert out["17", "产品类型"]["value"] == "未明确"
    cell["basis"] = "图片显示为硅胶干燥剂盒，非除湿机"
    out, _ = _parse(raw, [row], ["产品类型"], visual=True, visual_images=[])
    assert out["17", "产品类型"]["value"] == "未明确"


def test_single_primary_color_uses_explicit_visual_subject(monkeypatch):
    from tools.attr_stability import step18_model as model
    row = {"id": "84", "input": {"图片链接": "https://example.com/image.jpg"}, "attributes": {"颜色": {"value": "银灰配炭黑"}}}
    state = {"attributes": [{"name": "颜色"}], "definitions": {"attributes": []}}
    monkeypatch.setattr(model, "vision_with_fallback", lambda *a, **k: ({}, ["image"], []))
    cell = {"value": "银灰配炭黑", "source": "图片", "images": ["image"], "basis": "机身主体为银灰色面板，两侧及顶部为炭黑色框架"}
    monkeypatch.setattr(model, "_parse", lambda *a, **k: ({("84", "颜色"): dict(cell)}, []))
    result, warnings = model.reannotate(None, "只保留一个主色", "vision", ["颜色"], [row], state)
    assert result["84", "颜色"]["value"] == "银灰色"
    cell["basis"] = "整体呈银灰配炭黑双色，主体颜色不明"
    result, warnings = model.reannotate(None, "只保留一个主色", "vision", ["颜色"], [row], state)
    assert ("84", "颜色") not in result and warnings
    assert model._single_primary_color_requested("只要一种颜色")
    assert model._single_primary_color_requested("只保留一个主色")
    assert model._single_primary_color_requested("重判颜色并保留一个主体颜色")
    result, _ = model.reannotate(None, "重判颜色并保留一个主体颜色", "vision", ["颜色"], [row], state)
    assert ("84", "颜色") not in result
    cell.update(value="白色配黑色", basis="机身主体为白色，顶部出风口及控制面板区域为黑色，整体呈白色配黑色")
    result, _ = model.reannotate(None, "重判颜色并保留一个主体颜色", "vision", ["颜色"], [row], state)
    assert result["84", "颜色"]["value"] == "白色"


def test_product_type_vision_prompt_requires_actual_type_before_fallback(workspace):
    from tools.attr_stability.step18_model import _prompt
    s = workspace.open()
    prompt = _prompt("按资料审核", ["材质"], s["rows"][:1], s, visual=True)
    assert "先识别商品真实形态" in prompt
    assert "不得因目标类目限制把其他产品强行标成目标产品" in prompt


def test_image_only_prompt_excludes_listing_text_and_other_attribute_evidence(workspace):
    from tools.attr_stability.step18_model import _prompt
    state = workspace.open()
    row = copy.deepcopy(state["rows"][0])
    row["input"].update({"标题": "private-title-marker", "五点描述": "private-bullet-marker",
                         "详细参数": "private-details-marker"})
    row["attributes"]["材质"]["basis"] = "private-evidence-marker"
    row["attributes"]["功率"]["value"] = "private-other-attribute-marker"
    prompt = _prompt("查看图片", ["材质"], [row], state, visual=True)
    for marker in ("private-title-marker", "private-bullet-marker", "private-details-marker",
                   "private-evidence-marker", "private-other-attribute-marker"):
        assert marker not in prompt
    assert "只能根据本次提供的图片判断" in prompt


@pytest.mark.parametrize(("raw", "expected"), [
    ("2.00787401575inch", "2.01inch"), ("2inch", "2.00inch"),
    ("2.5in", "2.50in"), ("8.82 inches", "8.82 inches"),
    ("1.005inch", "1.01inch"), ("-1.005inch", "-1.01inch"),
    ("0.499445443335sq.ft", "0.50sq.ft"), ("645.834625003sq.ft", "645.83sq.ft"),
    ("0.0347222222222sq.ft", "0.03sq.ft"), ("2.3.4inch", None),
    ("2-3inch", None), ("1/2inch", None), ("未明确", None), ("约2inch", None),
])
def test_rounding_scalar_preserves_unit_and_rejects_ambiguous_values(raw, expected):
    from tools.attr_stability.step18_workspace import round_scalar_value
    assert round_scalar_value(raw, 2) == expected


def test_rounding_preview_respects_selection_and_preserves_evidence(workspace):
    from tools.attr_stability.step18_workspace import decimal_places_requirement
    assert decimal_places_requirement("保留2位小数") == 2
    assert decimal_places_requirement("保留两位小数并换算成厘米") is None
    before = workspace.open()
    old_cell = before["rows"][0]["attributes"]["功率"]
    proposal = workspace.propose(request(workspace, ["功率"], "保留2位小数",
        selection={"values": {"功率": [old_cell["value"]]}}))
    assert proposal["plan"][0]["type"] == "保留小数"
    assert proposal["changed_cells"] == 1
    assert workspace.open()["version"] == before["version"]
    after = workspace.apply(proposal["id"], before["version"])
    new_cell = after["rows"][0]["attributes"]["功率"]
    assert new_cell == {**old_cell, "value": "18000.00 W"}
    assert after["rows"][1] == before["rows"][1]
    assert workspace.open()["rows"] == after["rows"]


def test_rounding_preview_handles_interval_without_touching_composite_labels(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["材质"] = "5.5-9.5in"
    data["deliverable"][1]["attributes"]["材质"] = "64oz:8.00inch、40oz:7.50inch"
    write(path, data)
    proposal = workspace.propose(request(workspace, ["材质"], "保留2位小数"))
    patches = [p for p in proposal["patches"] if p["type"] == "cell"]
    assert len(patches) == 1 and patches[0]["after"]["value"] == "5.50-9.50in"
    assert proposal["warnings"][0]["row_id"] == "3"


def test_incremental_refresh_persists_new_baseline_and_preserves_history(workspace):
    ws = workspace
    initial = ws.open()
    p = ws.propose(request(ws, ["材质"], mapping={"材质": {"金属": "不锈钢"}}))
    changed = ws.apply(p["id"], initial["version"])
    imported = ws.refresh(changed["version"])
    reopened = ws.open()
    assert reopened["rows"] == imported["rows"] == initial["rows"]
    assert reopened["parent"] == changed["version"]
    assert [v["version"] for v in ws.chain(reopened)][:3] == [imported["version"], changed["version"], initial["version"]]
    assert "rows" in read_json(ws._path("versions", imported["version"], "V"))


def test_parallel_rejudge_empty_alias_does_not_block_independent_mapping(workspace):
    ws = workspace
    before = ws.open()
    after = copy.deepcopy(before)
    after["rows"][0]["attributes"]["温度调节"]["value"] = "低/中/高"
    ws._defs(after)["温度调节"]["params"]["aliases"] = {}
    preview = ws._save_proposal(before, after, {"kind": "重判", "requirement": "复核", "warnings": []})
    later = ws.propose(request(ws, ["温度调节"], mapping={"温度调节": {"不适用": "3档"}}))
    changed = ws.apply(later["id"], before["version"])
    result = ws.apply(preview["id"], changed["version"])
    assert result["rows"][0]["attributes"]["温度调节"]["value"] == "低/中/高"
    assert result["rows"][1]["attributes"]["温度调节"]["value"] == "3档"
    assert ws._defs(result)["温度调节"]["params"]["aliases"]["不适用"] == "3档"


def test_export_numeric_defaults_do_not_create_derived_columns(workspace):
    state = workspace.open()
    result = workspace.export(state["version"])
    wb = openpyxl.load_workbook(workspace.root / "18_标准化" / result["filename"], read_only=True, data_only=True)
    try:
        for name in ("属性标注", "来源", "依据"):
            sheet = wb[name]
            headers = [cell.value for cell in next(sheet.iter_rows(max_row=1))]
            assert "功率_数值" not in headers and "功率_档位" not in headers
            assert "功率" in headers
        rows = list(wb["属性标注"].iter_rows(values_only=True))
        index = rows[0].index("功率")
        assert rows[1][index] == state["rows"][0]["attributes"]["功率"]["value"]
    finally:
        wb.close()


def test_export_omits_persisted_legacy_numeric_pair(workspace):
    rules = read_json(workspace.rules_path)
    power = next(item for item in rules["attributes"] if item["name"] == "功率")
    power["subcols"] = power["params"]["subcols"] = ["功率_数值", "功率_档位"]
    write(workspace.rules_path, rules)
    state = workspace.open()
    result = workspace.export(state["version"])
    wb = openpyxl.load_workbook(workspace.root / "18_标准化" / result["filename"], read_only=True, data_only=True)
    try:
        for name in ("属性标注", "来源", "依据"):
            rows = wb[name].iter_rows(values_only=True)
            headers = next(rows)
            assert "功率_数值" not in headers and "功率_档位" not in headers
            assert "功率" in headers
            if name == "属性标注":
                assert next(rows)[headers.index("功率")] == state["rows"][0]["attributes"]["功率"]["value"]
        assert workspace.open()["definitions"] == state["definitions"]
    finally:
        wb.close()


def test_export_omits_legacy_input_traffic_process_fields(workspace):
    input_path = workspace.root / "17_属性标注/input.json"
    result_path = workspace.root / "17_属性标注/稳定性结果.json"
    inputs = read_json(input_path)
    for row in inputs["rows"]:
        row.update({"标签": "legacy", "流量词数": 112, "父体总流量": 236, "流量占比": 0.5})
    result = read_json(result_path)
    contract = make_annotation_contract(read_json(workspace.spec_path), inputs["rows"],
                                        source_path="unused", input_mode="snapshot")
    contract["completed"] = True
    result["annotation_contract"] = contract
    write(input_path, inputs)
    write(result_path, result)
    state = workspace.open()
    exported = workspace.export(state["version"])
    wb = openpyxl.load_workbook(workspace.root / "18_标准化" / exported["filename"], read_only=True)
    try:
        for name in ("属性标注", "来源", "依据"):
            headers = [c.value for c in next(wb[name].iter_rows(max_row=1))]
            assert not {"标签", "流量词数", "父体总流量"} & set(headers)
            assert "流量占比" in headers
    finally:
        wb.close()


def test_incremental_checkpoint_and_legacy_history(workspace, monkeypatch):
    import tools.attr_stability.step18_workspace as module
    monkeypatch.setattr(module, "SNAPSHOT_INTERVAL", 2)
    ws = workspace
    initial = ws.open()
    # A legacy full snapshot without a checksum remains readable.
    path = ws._path("versions", initial["version"], "V")
    legacy = read_json(path)
    legacy.pop("_checksum")
    write(path, legacy)
    first = ws.delete_row("3", initial["version"])
    assert "rows" not in read_json(ws._path("versions", first["version"], "V"))
    second = ws.delete_attribute("温度调节", first["version"])
    assert "rows" in read_json(ws._path("versions", second["version"], "V"))
    assert ws.open()["rows"] == second["rows"]
    assert ws._load_state(first["version"])["rows"] == first["rows"]


def test_incremental_rejects_corruption_and_unsafe_import(workspace):
    ws = workspace
    initial = ws.open()
    changed = ws.delete_row("3", initial["version"])
    path = ws._path("versions", changed["version"], "V")
    record = read_json(path)
    record["operation"]["requirement"] = "altered"
    write(path, record)
    with pytest.raises(ValueError, match="版本文件校验失败"):
        ws.open()
    # Legacy records have no checksum; replay must still check patch contents.
    record.pop("_checksum")
    record["operation"]["patches"][0]["type"] = "unrecognized"
    write(path, record)
    with pytest.raises(ValueError, match="未知的补丁类型"):
        ws.open()
    record["operation"] = {"kind": "import", "patches": []}
    write(path, record)
    with pytest.raises(ValueError, match="重新导入版本缺少完整快照"):
        ws.open()


def test_incremental_rejects_cell_patch_with_wrong_before(workspace):
    ws = workspace
    initial = ws.open()
    p = ws.propose(request(ws, ["材质"], mapping={"材质": {"金属": "不锈钢"}}))
    changed = ws.apply(p["id"], initial["version"])
    path = ws._path("versions", changed["version"], "V")
    record = read_json(path)
    record.pop("_checksum")
    next(p for p in record["operation"]["patches"] if p["type"] == "cell")["before"]["value"] = "错误原值"
    write(path, record)
    with pytest.raises(ValueError, match="单元格补丁原值不匹配"):
        ws.open()


@pytest.mark.parametrize("requirement", [
    "逐行联合审核所选整列的跨属性冲突与一致性，证据不足保留原值并说明。",
    "审核属性值与属性值之间的冲突，每行都要校验",
    "检查多属性之间的一致性",
])
def test_cross_review_plans_all_selected_columns_without_value_limits(workspace, requirement):
    from tools.attr_stability.step18_model import plan_action
    from tools.attr_stability.step18_workspace import is_value_only_classification
    state = workspace.open()
    attrs = ["是否带加热", "温度调节"]
    plan = plan_action(None, requirement, attrs, state["rows"], state)
    assert plan["operations"] == [{"type": "重判", "attributes": attrs, "method": "逐行联合审核"}]
    assert not is_value_only_classification(requirement + "，不要直接归类")


def test_full_column_cross_review_covers_every_row_in_one_joint_batch(workspace):
    state = workspace.open()
    attrs = ["是否带加热", "温度调节"]
    response = {"rows": [{"row_id": row["id"], "attributes": {
        name: row["attributes"][name] for name in attrs}} for row in state["rows"]]}
    model = Model(response)
    proposal = workspace.propose(request(workspace, attrs,
        "逐行联合审核所选整列的跨属性冲突与一致性，证据不足保留原值并说明。"), llm=model)
    assert proposal["reviewed_rows"] == 2
    assert proposal["scope_counts"] == {name: 2 for name in attrs}
    assert all(proposal["plan"][0]["counts"][name]["matched"] == 2 for name in attrs)
    assert len(model.prompts) == 1
    assert all('"row_id": "' + row["id"] + '"' in model.prompts[0] for row in state["rows"])
    assert all(name in model.prompts[0] for name in attrs)
    assert "不等于一定矛盾" in model.prompts[0]
    assert workspace.open()["version"] == state["version"]


def test_cross_review_preserves_explicit_row_and_value_selection(workspace):
    state = workspace.open()
    attrs = ["是否带加热", "温度调节"]
    row = state["rows"][1]
    model = Model({"rows": [{"row_id": row["id"], "attributes": {
        name: row["attributes"][name] for name in attrs}}]})
    proposal = workspace.propose(request(workspace, attrs, "联合审核跨属性冲突",
        selection={"row_ids": [row["id"]], "values": {"温度调节": ["不适用"]}}), llm=model)
    assert proposal["reviewed_rows"] == 1
    assert proposal["scope_counts"] == {name: 1 for name in attrs}
    assert '"row_id": "2"' not in model.prompts[0]


class Model:
    def __init__(self, *answers, vision=None):
        self.answers = list(answers)
        self.prompts = []
        self.vision_answer = vision
        self.images = []

    def chat_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)

    def vision(self, prompt, images, **kwargs):
        self.images.append(images)
        return copy.deepcopy(self.vision_answer)


def test_selected_unknown_rejudge_accepts_only_evidenced_ml(workspace, monkeypatch):
    import tools.attr_stability.step18_model as model_module
    raw_path = workspace.root / "17_属性标注/稳定性结果.json"
    raw = read_json(raw_path)
    raw["deliverable"][0]["attributes"]["功率"] = "未明确"
    raw["deliverable"][1]["attributes"]["功率"] = ""
    write(raw_path, raw)
    calls = []
    def reannotate(llm, requirement, mode, attrs, rows, state, progress, scope=None):
        calls.append((requirement, [r["id"] for r in rows], scope))
        return {("2", "功率"): {"value": "500 ml", "source": "标题", "basis": "500 ml"},
                ("3", "功率"): {"value": "1 L", "source": "标题", "basis": "1 L"}}, []
    monkeypatch.setattr(model_module, "reannotate", reannotate)
    proposal = workspace.propose(request(workspace, ["功率"], "重判这些值，并只保留带mL单位的值",
        selection={"values": {"功率": ["未明确", ""]}}), llm=object())
    assert proposal["plan"][0]["type"] == "重判"
    assert proposal["plan"][0]["counts"]["功率"]["matched"] == 2
    patches = [p for p in proposal["patches"] if p["type"] == "cell"]
    assert len(patches) == 1 and patches[0]["after"]["value"] == "500mL"
    assert any(w["row_id"] == "3" and "保留原值" in w["reason"] for w in proposal["warnings"])
    assert set(calls[0][1]) == {"2", "3"} and calls[0][2] == {"功率": {"", "未明确"}}
    assert workspace.open()["version"] == proposal["base_version"]


def test_ml_rejudge_fast_path_does_not_consume_compound_requests():
    from tools.attr_stability.step18_workspace import is_selected_ml_rejudge
    assert is_selected_ml_rejudge("重判这些值，并只保留带mL单位的值")
    assert not is_selected_ml_rejudge("重判这些值，并只保留带mL单位的值，其他值删除")


def test_value_only_preserved_colors_are_not_treated_as_unresolved():
    from tools.attr_stability.step18_model import plan_action
    names = ["哑光黑", "多色", "橙色"]
    rows = [{"id": str(i), "attributes": {"颜色": {"value": v}}} for i, v in enumerate(names)]
    note = "“多色”无法确定具体颜色大类，保留原值；“橙色”不在用户列举大类中，按原值保留。"
    answer = {"operations": [{"type": "归类", "attributes": ["颜色"],
                               "mappings": {"颜色": {"哑光黑": "黑色", "橙色": "金色"}}}],
              "unresolved": [note]}
    req = "根据已有颜色值归类，不读取商品原文或图片，无法确定的保留原值。"
    plan = plan_action(Model(answer, answer), req, ["颜色"], rows, {}, value_only=True)
    assert plan["operations"][0]["mappings"] == {"颜色": {"哑光黑": "黑色"}}
    assert plan["preserved_notes"] == [note]


def test_preserved_value_note_does_not_mask_unfinished_operation():
    from tools.attr_stability.step18_model import plan_action
    rows = [{"id": "2", "attributes": {"颜色": {"value": "多色"}}},
            {"id": "3", "attributes": {"颜色": {"value": "哑光黑"}}}]
    answer = {"operations": [{"type": "归类", "attributes": ["颜色"],
                               "mappings": {"颜色": {"哑光黑": "黑色"}}}],
              "unresolved": ["“多色”保留原值，但无法完成删除商品操作"]}
    with pytest.raises(ValueError, match="无法完成"):
        plan_action(Model(answer, answer), "按已有取值归类，不确定保留原值", ["颜色"], rows, {}, value_only=True)


def cross_model():
    return Model({"action": "reannotate"}, {"rows": [
        {"row_id": "2", "attributes": {
            "是否带加热": {"value": "是", "source": "标题", "basis": "3 heat levels"},
            "温度调节": {"value": "低/中/高", "source": "标题", "basis": "low medium high"}}}]})


def cross(ws):
    proposal = ws.propose(request(ws, ["是否带加热", "温度调节"],
                         selection={"row_ids": ["2"]}), llm=cross_model())
    return ws.apply(proposal["id"], proposal["base_version"])


def merge(ws, attr, before, after, **selection):
    p = ws.propose(request(ws, [attr], "人工指定归并", mapping={attr: {before: after}}, selection=selection))
    return ws.apply(p["id"], p["base_version"])


def test_parallel_independent_vocab_merges_keep_both_changes(workspace):
    base = workspace.open()
    p1 = workspace.propose(request(workspace, ["材质"], "人工指定归并", mapping={"材质": {"金属": "钢"}}))
    p2 = workspace.propose(request(workspace, ["材质"], "人工指定归并", mapping={"材质": {"ABS": "塑料"}}))
    first = workspace.apply(p1["id"], base["version"])
    result = workspace.apply(p2["id"], first["version"])
    assert [r["attributes"]["材质"]["value"] for r in result["rows"]] == ["钢", "塑料"]
    definition = next(d for d in result["definitions"]["attributes"] if d["name"] == "材质")
    assert definition["params"]["aliases"]["金属"] == "钢"
    assert definition["params"]["aliases"]["ABS"] == "塑料"
    assert result["parent"] == first["version"]
    undo = workspace.restore_proposal(result["version"], selective=True, expected=result["version"])
    assert not undo.get("blocked")


def test_batch_per_attribute_combines_one_preview_and_apply(workspace):
    initial = workspace.open()
    req = request(workspace, ["材质", "温度调节"], "按各属性要求批量处理",
        batch={"mode": "per_attribute", "rules": {"材质": "金属归类为钢", "温度调节": "3档归类为三档"}})
    model = Model({"operations": [{"type": "归类", "attributes": ["材质"],
                      "mappings": {"材质": {"金属": "钢"}}}], "unresolved": []},
                  {"operations": [{"type": "归类", "attributes": ["温度调节"],
                      "mappings": {"温度调节": {"3档": "三档"}}}], "unresolved": []})
    preview = workspace.propose_batch(req, llm=model)
    assert preview["changed_cells"] == 2
    assert [p["requirement"] for p in preview["plan"]] == ["金属归类为钢", "3档归类为三档"]
    assert workspace.open()["version"] == initial["version"]
    applied = workspace.apply(preview["id"], initial["version"])
    assert applied["rows"][0]["attributes"]["材质"]["value"] == "钢"
    assert applied["rows"][0]["attributes"]["温度调节"]["value"] == "三档"
    assert applied["rows"][1]["attributes"]["材质"]["value"] == "ABS"


def test_batch_rejects_removed_auto_mode_and_incomplete_rules(workspace):
    base = workspace.open()
    req = request(workspace, ["材质"], "自动扫描", batch={"mode": "auto"})
    with pytest.raises(ValueError, match="批量处理方式无效"):
        workspace.propose_batch(req, llm=Model())
    req = request(workspace, ["材质", "功率"], "批量", batch={"mode": "per_attribute", "rules": {"材质": "归类"}})
    with pytest.raises(ValueError, match="每个已选属性"):
        workspace.propose_batch(req, llm=Model())
    assert workspace.open()["version"] == base["version"]


def test_background_batch_job_produces_one_combined_proposal(workspace):
    from webui.step18_api import start_preview, job_status
    req = request(workspace, ["材质", "温度调节"], "批量处理", batch={"mode": "per_attribute", "rules": {
        "材质": "金属归类为钢", "温度调节": "3档归类为三档"}})
    model = Model({"operations": [{"type": "归类", "attributes": ["材质"],
        "mappings": {"材质": {"金属": "钢"}}}], "unresolved": []},
        {"operations": [{"type": "归类", "attributes": ["温度调节"],
        "mappings": {"温度调节": {"3档": "三档"}}}], "unresolved": []})
    started = start_preview(workspace, req, {}, client_factory=lambda _: model)
    for _ in range(200):
        job = job_status(workspace, started["job_id"])
        if job["status"] not in ("pending", "running"):
            break
        time.sleep(.01)
    assert job["status"] == "done", job.get("message")
    assert job["result"]["kind"] == "批量处理"
    assert job["result"]["changed_cells"] == 2
    assert workspace.open()["version"] == req["expected_version"]


def test_parallel_merges_of_same_cell_use_last_confirmed_preview(workspace):
    base = workspace.open()
    p1 = workspace.propose(request(workspace, ["材质"], "人工指定归并", mapping={"材质": {"ABS": "塑料"}}))
    p2 = workspace.propose(request(workspace, ["材质"], "人工指定归并", mapping={"材质": {"ABS": "树脂"}}))
    first = workspace.apply(p1["id"], base["version"])
    result = workspace.apply(p2["id"], first["version"])
    assert result["rows"][1]["attributes"]["材质"]["value"] == "树脂"
    actual = next(p for p in result["operation"]["patches"] if p["type"] == "cell")
    assert actual["before"]["value"] == "塑料" and actual["after"]["value"] == "树脂"


def test_vocab_rebase_rejects_changed_mapping_target_or_rule():
    original = {"name": "材质", "rule": "enum", "params": {"values": ["A", "B"], "aliases": {}}}
    changed_target = copy.deepcopy(original)
    changed_target["params"]["aliases"]["B"] = "C"
    assert not Workspace._independent_vocabulary_change(original, changed_target, {"A": "B"})
    changed_rule = copy.deepcopy(original)
    changed_rule["rule"] = "numeric"
    assert not Workspace._independent_vocabulary_change(original, changed_rule, {"A": "B"})


def fingerprint(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and "版本数据" not in p.parts
            and p.suffix != ".xlsx"}


def test_input_is_read_only_and_multiselect_is_union(workspace):
    ws = workspace
    before = fingerprint(ws.root)
    current = ws.open()
    attrs, rows = ws.select(current, {"attributes": ["材质"], "values": {"材质": ["ABS", "金属"]}})
    assert {r["id"] for r in rows} == {"2", "3"}
    proposal = ws.propose(request(ws, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    assert proposal["changed_cells"] == 1
    assert ws.open()["version"] == current["version"]
    updated = ws.apply(proposal["id"], current["version"])
    assert updated["rows"][1]["attributes"]["材质"]["value"] == "ABS塑料"
    assert fingerprint(ws.root) == before
    assert ws._defs(updated)["材质"]["params"]["aliases"]["ABS"] == "ABS塑料"


def test_apply_only_selected_cells_recalculates_vocabulary_and_can_undo(workspace):
    ws = workspace
    before = ws.open()
    p = ws.propose(request(ws, ["材质"], "人工指定归并",
                           mapping={"材质": {"金属": "塑料", "ABS": "塑料"}}))
    result = ws.apply(p["id"], before["version"], selected_cells=[{"row_id": "2", "attribute": "材质"}])
    assert [r["attributes"]["材质"]["value"] for r in result["rows"]] == ["塑料", "ABS"]
    assert result["operation"]["selected_cells"] == 1
    assert result["operation"]["omitted_cells"] == 1
    aliases = ws._defs(result)["材质"]["params"]["aliases"]
    assert aliases.get("金属") == "塑料" and "ABS" not in aliases
    history = ws.summary()["history"][0]
    assert history["selected_attributes"] == ["材质"]
    assert history["selected_values"] == {}
    assert history["value_changes"]["材质"] == [{"before": "金属", "after": "塑料", "count": 1}]
    assert history["omitted_cells"] == 1
    undo = ws.restore_proposal(result["version"], selective=True, expected=result["version"])
    restored = ws.apply(undo["id"], result["version"])
    assert [r["attributes"]["材质"]["value"] for r in restored["rows"]] == ["金属", "ABS"]


def test_apply_rejects_empty_duplicate_or_unrelated_selection(workspace):
    before = workspace.open()
    p = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    item = {"row_id": "3", "attribute": "材质"}
    for selection in ([], [item, item], [{"row_id": "2", "attribute": "材质"}]):
        with pytest.raises(ValueError):
            workspace.apply(p["id"], before["version"], selected_cells=selection)
    assert workspace.open()["version"] == before["version"]


def test_delete_product_row_removes_all_attributes_from_export_and_can_be_undone(workspace):
    ws = workspace
    before = ws.open()
    upstream = fingerprint(ws.root)
    deleted = ws.delete_row("3", before["version"])
    assert [r["id"] for r in deleted["rows"]] == ["2"]
    assert ws.summary()["row_count"] == 1
    out = ws.export(deleted["version"])
    wb = openpyxl.load_workbook(ws.root / "18_标准化" / out["filename"], read_only=True)
    try:
        for name in ("属性标注", "来源", "依据"):
            assert [r[0] for r in list(wb[name].values)[1:]] == [2]
    finally:
        wb.close()
    changed = merge(ws, "材质", "金属", "塑料")
    undo = ws.restore_proposal(deleted["version"], selective=True, expected=changed["version"])
    assert undo["changed_rows"] == 1
    restored = ws.apply(undo["id"], changed["version"])
    assert [r["id"] for r in restored["rows"]] == ["2", "3"]
    assert restored["rows"][0]["attributes"]["材质"]["value"] == "塑料"
    assert restored["rows"][1] == before["rows"][1]
    assert fingerprint(ws.root) == upstream


def test_deleted_row_blocks_old_previews_and_duplicate_or_last_deletion(workspace):
    ws = workspace
    before = ws.open()
    p = ws.propose(request(ws, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    deleted = ws.delete_row("3", before["version"])
    with pytest.raises(WorkspaceConflict, match="相同商品属性或词表"):
        ws.apply(p["id"], deleted["version"])
    with pytest.raises(ValueError, match="不存在"):
        ws.delete_row("3", deleted["version"])
    with pytest.raises(ValueError, match="最后一行"):
        ws.delete_row("2", deleted["version"])


def test_old_job_marks_deleted_rows_and_can_apply_remaining_preview_cells(workspace):
    from webui.step18_api import job_status, list_jobs
    ws = workspace
    before = ws.open()
    proposal = ws.propose(request(ws, ["材质"], mapping={"材质": {"ABS": "塑料", "金属": "塑料"}}))
    ident = "J" + "e" * 32
    write(ws._path("jobs", ident, "J"), {"id": ident, "status": "done", "result": proposal})
    deleted = ws.delete_row("3", before["version"])
    visible = job_status(ws, ident)
    assert visible["result"]["unavailable_rows"] == ["3"]
    assert "unavailable_rows" not in read_json(ws._path("jobs", ident, "J"))["result"]
    assert list_jobs(ws)[0]["changed_cells"] == 2
    current = ws.apply(proposal["id"], deleted["version"],
                       selected_cells=[{"row_id": "2", "attribute": "材质"}])
    assert [r["id"] for r in current["rows"]] == ["2"]
    assert current["rows"][0]["attributes"]["材质"]["value"] == "塑料"
    assert current["operation"]["omitted_cells"] == 1


def test_delete_rows_by_attribute_value_is_exact_and_undoable(workspace):
    ws = workspace
    input_path = ws.root / "17_属性标注/input.json"
    result_path = ws.root / "17_属性标注/稳定性结果.json"
    inputs, result = read_json(input_path), read_json(result_path)
    inputs["rows"].append({**copy.deepcopy(inputs["rows"][1]), "xl_row": 4})
    result["deliverable"].append({**copy.deepcopy(result["deliverable"][1]), "xl_row": 4})
    result["deliverable"][0]["attributes"]["是否带加热"] = "是"
    contract = make_annotation_contract(read_json(ws.spec_path), inputs["rows"], source_path="unused", input_mode="snapshot")
    contract["completed"] = True
    result["annotation_contract"] = contract
    write(input_path, inputs)
    write(result_path, result)
    before = ws.open()
    deleted = ws.delete_rows_by_value("是否带加热", "否", before["version"])
    assert [r["id"] for r in deleted["rows"]] == ["2"]
    history = ws.summary()["history"][0]
    assert history["selected_attributes"] == ["是否带加热"]
    assert history["selected_values"] == {"是否带加热": ["否"]}
    assert len([p for p in deleted["operation"]["patches"] if p["type"] == "row"]) == 2
    undo = ws.restore_proposal(deleted["version"], selective=True, expected=deleted["version"])
    restored = ws.apply(undo["id"], deleted["version"])
    assert restored["rows"] == before["rows"]


def test_delete_multiple_attribute_values_is_scoped_and_undoable(workspace, monkeypatch):
    ws = workspace
    input_path = ws.root / "17_属性标注/input.json"
    result_path = ws.root / "17_属性标注/稳定性结果.json"
    inputs, result = read_json(input_path), read_json(result_path)
    inputs["rows"].append({**copy.deepcopy(inputs["rows"][1]), "xl_row": 4})
    third = copy.deepcopy(result["deliverable"][1])
    third["xl_row"] = 4
    third["attributes"]["材质"] = ""
    result["deliverable"].append(third)
    contract = make_annotation_contract(read_json(ws.spec_path), inputs["rows"], source_path="unused", input_mode="snapshot")
    contract["completed"] = True
    result["annotation_contract"] = contract
    write(input_path, inputs)
    write(result_path, result)
    module_path = Path(__file__).parents[1] / "webui/server.py"
    spec = importlib.util.spec_from_file_location("step18_multi_delete_test_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = module.Handler.__new__(module.Handler)
    monkeypatch.setattr(handler, "_std_workspace", lambda _: ws)
    monkeypatch.setattr(module, "_lock_live_started", lambda *_: (False, None, False))
    before = ws.open()
    req = {"expected_version": before["version"], "attribute": "材质", "values": ["ABS", ""]}
    info = handler._api_std_workspace_action("value-rows-info", req)
    assert info["count"] == 2 and info["total"] == 3
    deleted = handler._api_std_workspace_action("delete-value-rows", req)
    assert deleted["row_count"] == 1
    assert [r["id"] for r in ws.open()["rows"]] == ["2"]
    assert ws.summary()["history"][0]["selected_values"] == {"材质": ["ABS", ""]}
    undo = ws.restore_proposal(deleted["version"], selective=True, expected=deleted["version"])
    assert ws.apply(undo["id"], deleted["version"])["rows"] == before["rows"]


def test_history_exposes_selected_tags_and_full_requirement(workspace):
    ws = workspace
    base = ws.open()
    requirement = "请统一材质\n保留所选商品的明确范围"
    p = ws.propose(request(ws, ["材质"], requirement,
                           selection={"values": {"材质": ["ABS", "金属"]}},
                           mapping={"材质": {"ABS": "ABS塑料"}}))
    updated = ws.apply(p["id"], base["version"])
    record = ws.summary(state=updated)["history"][0]
    assert record["requirement"] == requirement
    assert record["selected_attributes"] == ["材质"]
    assert record["selected_values"] == {"材质": ["ABS", "金属"]}
    assert record["value_changes"] == {"材质": [{"before": "ABS", "after": "ABS塑料", "count": 1}]}
    assert ws.summary(state=updated, attribute_names={"材质"})["history"][0] == record


def test_delete_rows_by_value_rejects_unknown_or_all_rows(workspace):
    ws = workspace
    before = ws.open()
    with pytest.raises(ValueError, match="全部商品"):
        ws.delete_rows_by_value("是否带加热", "否", before["version"])
    with pytest.raises(ValueError, match="没有对应商品"):
        ws.delete_rows_by_value("材质", "不存在", before["version"])
    assert ws.open()["version"] == before["version"]


def test_delete_row_http_action_and_undo_return_refreshed_workspace(workspace, monkeypatch):
    module_path = Path(__file__).parents[1] / "webui/server.py"
    spec = importlib.util.spec_from_file_location("step18_delete_test_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = module.Handler.__new__(module.Handler)
    monkeypatch.setattr(handler, "_std_workspace", lambda _: workspace)
    monkeypatch.setattr(module, "_lock_live_started", lambda *_: (False, None, False))
    version = workspace.open()["version"]
    info = handler._api_std_workspace_action("row-info", {"expected_version": version, "row_id": "3"})
    assert info["title"].startswith("ABS housing") and info["attribute_count"] == 4
    result = handler._api_std_workspace_action("delete-row", {"expected_version": version, "row_id": "3"})
    assert result["row_count"] == 1
    preview = handler._api_std_workspace_action("restore-preview", {"expected_version": result["version"],
        "version": result["version"], "selective": True})
    restored = handler._api_std_workspace_action("apply", {"expected_version": result["version"],
        "proposal_id": preview["id"], "partial": True})
    assert restored["rows_changed"] is True and restored["row_count"] == 2


def test_delete_value_rows_http_reports_scope_and_removes_only_matches(workspace, monkeypatch):
    module_path = Path(__file__).parents[1] / "webui/server.py"
    spec = importlib.util.spec_from_file_location("step18_delete_value_test_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = module.Handler.__new__(module.Handler)
    monkeypatch.setattr(handler, "_std_workspace", lambda _: workspace)
    monkeypatch.setattr(module, "_lock_live_started", lambda *_: (False, None, False))
    version = workspace.open()["version"]
    req = {"expected_version": version, "attribute": "材质", "value": "ABS"}
    info = handler._api_std_workspace_action("value-rows-info", req)
    assert info["count"] == 1 and info["examples"][0]["row_id"] == "3"
    result = handler._api_std_workspace_action("delete-value-rows", req)
    assert result["row_count"] == 1 and [r["id"] for r in workspace.open()["rows"]] == ["2"]


def test_delete_attribute_removes_column_from_all_rows_export_and_can_undo(workspace):
    ws = workspace
    before = ws.open()
    upstream = fingerprint(ws.root)
    deleted = ws.delete_attribute("温度调节", before["version"])
    assert "温度调节" not in [a["name"] for a in deleted["attributes"]]
    assert "温度调节" not in ws._defs(deleted)
    assert all("温度调节" not in row["attributes"] for row in deleted["rows"])
    assert ws.summary(state=deleted)["issues"]["cross"] == []
    out = ws.export(deleted["version"])
    wb = openpyxl.load_workbook(ws.root / "18_标准化" / out["filename"], read_only=True)
    try:
        for name in ("属性标注", "来源", "依据"):
            assert "温度调节" not in [c.value for c in wb[name][1]]
    finally:
        wb.close()
    changed = merge(ws, "材质", "ABS", "ABS塑料")
    p = ws.restore_proposal(deleted["version"], selective=True, expected=changed["version"])
    assert p["changed_columns"] == 1
    restored = ws.apply(p["id"], changed["version"])
    assert [a["name"] for a in restored["attributes"]] == [a["name"] for a in before["attributes"]]
    assert restored["rows"][1]["attributes"]["材质"]["value"] == "ABS塑料"
    assert restored["rows"][0]["attributes"]["温度调节"] == before["rows"][0]["attributes"]["温度调节"]
    assert fingerprint(ws.root) == upstream


def test_deleted_attribute_skipped_in_old_job_while_other_attribute_can_apply(workspace):
    from webui.step18_api import job_status
    ws = workspace
    before = ws.open()
    p = ws.propose(request(ws, ["是否带加热", "温度调节"], selection={"row_ids": ["2"]}), llm=cross_model())
    ident = "J" + "d" * 32
    write(ws._path("jobs", ident, "J"), {"id": ident, "status": "done", "result": p})
    deleted = ws.delete_attribute("温度调节", before["version"])
    job = job_status(ws, ident)
    assert job["result"]["unavailable_attributes"] == ["温度调节"]
    with pytest.raises(WorkspaceConflict):
        ws.apply(p["id"], deleted["version"])
    applied = ws.apply(p["id"], deleted["version"],
                       selected_cells=[{"row_id": "2", "attribute": "是否带加热"}])
    assert applied["rows"][0]["attributes"]["是否带加热"]["value"] == "是"
    assert "温度调节" not in applied["rows"][0]["attributes"]
    assert applied["operation"]["omitted_cells"] == 1


def test_attribute_deleted_while_preview_is_running(workspace):
    import threading
    from webui.step18_api import start_preview, job_status

    entered, release = threading.Event(), threading.Event()

    class SlowModel(Model):
        def chat_json(self, prompt, **kwargs):
            if not self.prompts:
                entered.set()
                assert release.wait(10)
            return super().chat_json(prompt, **kwargs)

    source = cross_model()
    model = SlowModel(*source.answers)
    initial = workspace.open()["version"]
    job = start_preview(workspace, request(workspace, ["是否带加热", "温度调节"],
                        selection={"row_ids": ["2"]}), {}, client_factory=lambda _: model)
    try:
        assert entered.wait(5)
        deleted = workspace.delete_attribute("温度调节", initial)
        release.set()
        for _ in range(300):
            result = job_status(workspace, job["job_id"])
            if result["status"] not in ("pending", "running"):
                break
            time.sleep(.01)
        assert result["status"] == "done", result
        assert result["result"]["unavailable_attributes"] == ["温度调节"]
        applied = workspace.apply(result["result"]["id"], deleted["version"],
                                  selected_cells=[{"row_id": "2", "attribute": "是否带加热"}])
        assert applied["rows"][0]["attributes"]["是否带加热"]["value"] == "是"
        assert "温度调节" not in applied["rows"][0]["attributes"]
    finally:
        release.set()


def test_attribute_deletion_keeps_rejudge_prompt_free_of_dangling_cross_rules(workspace):
    from tools.attr_stability.step18_model import _prompt
    ws = workspace
    before = ws.open()
    state = ws.delete_attribute("温度调节", before["version"])
    prompt = _prompt("复核", ["是否带加热"], state["rows"], state)
    assert "温度调节" not in prompt


def test_fill_blank_preserves_not_applicable_and_its_evidence(workspace):
    result_path = workspace.root / "17_属性标注/稳定性结果.json"
    result = read_json(result_path)
    result["deliverable"][0]["attributes"]["温度调节"] = ""
    write(result_path, result)

    before = workspace.open()
    original = copy.deepcopy(before["rows"][1]["attributes"]["温度调节"])
    proposal = workspace.propose(request(workspace, ["温度调节"], "把空值改成未明确"))
    assert proposal["changed_cells"] == 1
    assert proposal["patches"][0]["row_id"] == "2"
    updated = workspace.apply(proposal["id"], before["version"])
    assert updated["rows"][0]["attributes"]["温度调节"]["value"] == "未明确"
    assert updated["rows"][1]["attributes"]["温度调节"] == original


@pytest.mark.parametrize("target", ["是", "否"])
@pytest.mark.parametrize("via_model", [False, True])
def test_explicit_not_applicable_mapping_can_be_previewed_and_applied(workspace, target, via_model):
    before = workspace.open()
    mapping = {"温度调节": {"不适用": target}}
    req = request(workspace, ["温度调节"], f"把不适用改成{target}",
                  selection={"values": {"温度调节": ["不适用"]}})
    if via_model:
        proposal = workspace.propose(req, llm=Model({"kind": "归类", "mappings": mapping}))
    else:
        proposal = workspace.propose({**req, "mapping": mapping})
    assert proposal["changed_cells"] == 1
    assert workspace.open()["version"] == before["version"]
    updated = workspace.apply(proposal["id"], before["version"])
    assert updated["rows"][1]["attributes"]["温度调节"]["value"] == target
    assert updated["rows"][0]["attributes"]["温度调节"] == before["rows"][0]["attributes"]["温度调节"]


@pytest.mark.parametrize("target", ["是", "否"])
@pytest.mark.parametrize("via_model", [False, True])
def test_unknown_cannot_be_directly_mapped_to_factual_value(workspace, target, via_model):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    result = read_json(path)
    result["deliverable"][1]["attributes"]["温度调节"] = "未明确"
    write(path, result)
    before = workspace.open()
    mapping = {"温度调节": {"未明确": target}}
    req = request(workspace, ["温度调节"], f"把未明确改成{target}")
    with pytest.raises(ValueError, match="不能直接归并"):
        if via_model:
            workspace.propose(req, llm=Model({"kind": "归类", "mappings": mapping}))
        else:
            workspace.propose({**req, "mapping": mapping})
    assert workspace.open()["version"] == before["version"]


def test_mixed_plan_applies_mapping_and_rejudges_only_requested_values(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["温度调节"] = "未明确"
    write(path, data)
    model = Model({"operations": [
        {"type": "归类", "attributes": ["温度调节"], "mappings": {"温度调节": {"不适用": "否"}}},
        {"type": "重判", "attributes": ["温度调节"], "values": {"温度调节": ["未明确"]}}], "unresolved": []},
        {"rows": [{"row_id": "2", "attributes": {"温度调节": {
            "value": "3档", "source": "标题", "basis": "3 heat levels"}}}]})
    p = workspace.propose(request(workspace, ["温度调节"], "不适用改成否，再根据原文重判未明确"), llm=model)
    assert [x["type"] for x in p["plan"]] == ["归类", "重判"]
    assert [x["counts"]["温度调节"]["changed"] for x in p["plan"]] == [1, 1]
    assert p["changed_cells"] == 2
    applied = workspace.apply(p["id"], p["base_version"])
    assert [r["attributes"]["温度调节"]["value"] for r in applied["rows"]] == ["3档", "否"]


@pytest.mark.parametrize("mode", ["auto", "vision"])
def test_semantic_value_classification_uses_only_existing_values(workspace, mode):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["材质"] = "橡木"
    data["deliverable"][1]["attributes"]["材质"] = "松木"
    write(path, data)
    model = Model({"operations": [{"type": "归类", "attributes": ["材质"],
            "mappings": {"材质": {"橡木": "木制", "松木": "木制"}}}], "unresolved": []})
    req = request(workspace, ["材质"], "将各种木的值归为木制")
    req["mode"] = mode
    p = workspace.propose(req, llm=model)
    assert len(model.prompts) == 1 and not model.images
    assert "橡木" in model.prompts[0] and "松木" in model.prompts[0]
    assert "Stainless steel" not in model.prompts[0] and "example.com" not in model.prompts[0]
    assert p["plan"][0]["method"] == "仅依据已有属性值"
    assert p["changed_cells"] == 2
    updated = workspace.apply(p["id"], p["base_version"])
    assert [r["attributes"]["材质"]["value"] for r in updated["rows"]] == ["木制", "木制"]


def test_value_only_request_rejects_model_rejudge_without_reading_images(workspace):
    model = Model({"operations": [{"type": "重判", "attributes": ["材质"]}], "unresolved": []})
    req = request(workspace, ["材质"], "将各种材质按属性值粗分类")
    req["mode"] = "vision"
    with pytest.raises(ValueError, match="未读取商品资料或图片"):
        workspace.propose(req, llm=model)
    assert len(model.prompts) == 1 and not model.images


def test_mixed_blank_rejudge_and_typo_mapping_retries_misclassified_explanation(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["温度调节"] = ""
    write(path, data)
    plan = {"operations": [
        {"type": "重判", "attributes": ["温度调节"], "values": {"温度调节": [""]}},
        {"type": "归类", "attributes": ["温度调节"], "mappings": {"温度调节": {"不适用": "否"}}}],
        "unresolved": []}
    incorrect = {**plan, "unresolved": ["不适应按不适用理解，已按不适用改成否处理"]}
    model = Model(incorrect, plan, {"rows": [{"row_id": "2", "attributes": {"温度调节": {
        "value": "3档", "source": "标题", "basis": "3 heat levels"}}}]})
    req = request(workspace, ["温度调节"], "将空值进行重判，不适应改成否",
                  selection={"values": {"温度调节": ["", "不适用"]}})
    proposal = workspace.propose(req, llm=model)
    assert len(model.prompts) == 3
    assert "用户写了「不适应」" in model.prompts[0]
    assert proposal["requirement"] == "将空值进行重判，不适应改成否"
    assert [op["type"] for op in proposal["plan"]] == ["重判", "归类"]
    updated = workspace.apply(proposal["id"], proposal["base_version"])
    assert [row["attributes"]["温度调节"]["value"] for row in updated["rows"]] == ["3档", "否"]


def test_each_mapping_is_validated_not_just_last(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["温度调节"] = "未明确"
    write(path, data)
    req = request(workspace, ["温度调节"], "人工指定归并",
                  mapping={"温度调节": {"未明确": "是", "不适用": "否"}})
    with pytest.raises(ValueError, match="不能直接归并"):
        workspace.propose(req)


def test_explicit_selected_values_are_mapped_including_compound_without_llm(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["材质"] = "木+塑料"
    write(path, data)
    req = request(workspace, ["材质"], "选中的改为木制",
                  selection={"values": {"材质": ["木+塑料", "ABS"]}})
    proposal = workspace.propose(req)
    assert proposal["plan"][0]["type"] == "归类"
    assert proposal["changed_cells"] == 2
    current = workspace.apply(proposal["id"], proposal["base_version"])
    assert [r["attributes"]["材质"]["value"] for r in current["rows"]] == ["木制", "木制"]


def test_selected_assignment_respects_scope_unknown_values_and_extra_instructions(workspace):
    from tools.attr_stability.step18_workspace import selected_assignment_target
    assert selected_assignment_target("选中的改为木制") == "木制"
    assert selected_assignment_target("选中的改为木制并重判") is None
    assert selected_assignment_target("选中的改为木制，再重判") is None
    with pytest.raises(ValueError, match="请先选择一个属性"):
        workspace.propose(request(workspace, ["材质"], "选中的改为木制"))
    data_path = workspace.root / "17_属性标注/稳定性结果.json"
    result = read_json(data_path)
    result["deliverable"][0]["attributes"]["材质"] = "未明确"
    write(data_path, result)
    workspace.refresh(workspace.open()["version"])
    req = request(workspace, ["材质"], "选中的改为木制", selection={"values": {"材质": ["未明确"]}})
    with pytest.raises(ValueError, match="不能直接归并"):
        workspace.propose(req)


def test_selected_assignment_preview_job_needs_no_model(workspace):
    from webui.step18_api import start_preview, job_status
    req = request(workspace, ["材质"], "选中的改为木制", selection={"values": {"材质": ["ABS"]}})
    def no_client(_):
        raise AssertionError("不应调用模型")
    job = start_preview(workspace, req, {}, client_factory=no_client)
    for _ in range(300):
        result = job_status(workspace, job["job_id"])
        if result["status"] not in ("pending", "running"):
            break
        time.sleep(.01)
    assert result["status"] == "done", result
    assert result["result"]["changed_cells"] == 1
    assert result["result"]["plan"][0]["type"] == "归类"


def test_unit_plan_converts_only_selected_cells_and_preserves_ambiguous_units(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["功率"] = "1800"
    data["deliverable"][1]["attributes"]["功率"] = "1.8kW"
    write(path, data)
    model = Model({"operations": [{"type": "统一单位", "attributes": ["功率"], "unit": "W"}], "unresolved": []})
    p = workspace.propose(request(workspace, ["功率"], "把功率统一成W"), llm=model)
    assert p["plan"][0]["counts"]["功率"] == {"matched": 2, "changed": 1, "unchanged": 1}
    assert p["warnings"][0]["row_id"] == "2"
    applied = workspace.apply(p["id"], p["base_version"])
    assert [r["attributes"]["功率"]["value"] for r in applied["rows"]] == ["1800", "1800W"]


def test_length_unit_plan_handles_composite_and_requested_precision(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["材质"] = "64oz:20.32cm、40oz:19.05cm"
    data["deliverable"][1]["attributes"]["材质"] = "20.07cm"
    write(path, data)
    model = Model({"operations": [{"type": "统一单位", "attributes": ["材质"], "unit": "inch"}], "unresolved": []})
    proposal = workspace.propose(request(workspace, ["材质"], "将选中值中的厘米统一换算为inch，保留两位小数。保留组合格式"), llm=model)
    values = [p["after"]["value"] for p in proposal["patches"] if p["type"] == "cell"]
    assert values == ["64oz:8.00inch、40oz:7.50inch", "7.90inch"]


@pytest.mark.parametrize("raw,basis,target,expected", [
    ("101.0 oz", "large 101 oz (3000 ml) tank", "L", "3L"),
    ("101 oz", "tank 3 L (101 oz)", "mL", "3000mL"),
    ("101 oz", "tank 101 oz", "L", None),
    ("101 oz", "tank 100 oz (3000 ml)", "L", None),
    ("101 oz", "tank 101 oz (300 ml)", "L", None),
    ("101 oz", "tank 101 oz (3000 ml)", "kg", None),
    ("101 oz/day", "101 oz (3000 ml) per day", "L", None),
])
def test_evidenced_oz_volume(raw, basis, target, expected):
    from tools.attr_stability.step18_workspace import evidenced_oz_volume
    assert evidenced_oz_volume(raw, basis, target) == expected


def test_unit_preview_uses_paired_volume_evidence(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"].update({"材质": "101.0 oz", "材质_依据": "large 101 oz (3000 ml) tank"})
    data["deliverable"][1]["attributes"].update({"材质": "101.0 oz", "材质_依据": "tank 101 oz"})
    write(path, data)
    model = Model({"operations": [{"type": "统一单位", "attributes": ["材质"], "unit": "L"}], "unresolved": []})
    result = workspace.propose(request(workspace, ["材质"], "单位统一为L"), llm=model)
    assert result["changed_cells"] == 1
    assert result["patches"][0]["after"]["value"] == "3L"
    assert result["patches"][0]["after"]["basis"] == "large 101 oz (3000 ml) tank"
    assert "可能表示重量或液量" in result["warnings"][0]["reason"]


def test_unit_plan_reconsiders_values_that_user_says_to_preserve(workspace):
    from tools.attr_stability.step18_model import plan_action
    state = workspace.open()
    bad = {"operations": [], "unresolved": ["空值及无法换算的质量单位需要确认"]}
    good = {"operations": [{"type": "统一单位", "attributes": ["功率"], "unit": "W"}], "unresolved": []}
    model = Model(bad, good)
    result = plan_action(model, "将功率统一为W，证据不足时保留原值", ["功率"], state["rows"], state)
    assert result["operations"][0]["type"] == "统一单位"
    assert len(model.prompts) == 2
    assert "质量与体积不可直接换算" in model.prompts[1]
    model = Model(bad, bad)
    with pytest.raises(ValueError, match="需要确认"):
        plan_action(model, "将功率统一为W，证据不足时保留原值", ["功率"], state["rows"], state)


def test_component_exclusion_and_coarse_rejudge_plan_can_reconsider_false_ambiguity(workspace):
    from tools.attr_stability.step18_model import plan_action
    state = workspace.open()
    ambiguous = {"operations": [], "unresolved": ["无法排除部件商品，缺少类别表"]}
    planned = {"operations": [{"type": "重判", "attributes": ["材质"]}], "unresolved": []}
    model = Model(ambiguous, planned)
    result = plan_action(model, "不看手柄材质，重判和粗化该列", ["材质"], state["rows"], state)
    assert result["operations"] == [{"type": "重判", "attributes": ["材质"]}]
    assert "不是排除含有该部件的商品行" in model.prompts[0]
    assert "无需事先枚举类别" in model.prompts[1]
    with pytest.raises(ValueError, match="缺少类别表"):
        plan_action(Model(ambiguous, ambiguous), "不看手柄材质，重判和粗化该列",
                    ["材质"], state["rows"], state)


def test_value_only_classification_retries_preserved_values_without_rejudge(workspace):
    from tools.attr_stability.step18_model import plan_action
    from tools.attr_stability.step18_workspace import is_value_only_classification
    state = workspace.open()
    requirement = "仅根据已有材质归类，不读取商品原文或图片，不重判。无法确定归属的值保留原样。"
    assert is_value_only_classification(requirement)
    initial = {"operations": [{"type": "归类", "mappings": {"材质": {"ABS": "塑料"}}}],
               "unresolved": ["无法确定金属的类别，保留原样"]}
    resolved = {"operations": [{"type": "归类", "mappings": {"材质": {"ABS": "塑料"}}}], "unresolved": []}
    model = Model(initial, resolved)
    plan = plan_action(model, requirement, ["材质"], state["rows"], state, value_only=True)
    assert plan["operations"] == [{"type": "归类", "attributes": ["材质"], "mappings": {"材质": {"ABS": "塑料"}}}]
    assert len(model.prompts) == 2 and "从mappings省略该值" in model.prompts[1]
    with pytest.raises(ValueError, match="无法确定"):
        plan_action(Model(initial, initial), requirement, ["材质"], state["rows"], state, value_only=True)


def test_rejudge_all_with_unspecified_format_reconsiders_false_ambiguity(workspace):
    from tools.attr_stability.step18_model import plan_action
    state = workspace.open()
    unclear = {"operations": [], "unresolved": ["统一格式未指定具体映射"]}
    rejudge = {"operations": [{"type": "重判", "attributes": ["材质"]}], "unresolved": []}
    model = Model(unclear, rejudge)
    result = plan_action(model, "重判所有的值，统一格式", ["材质"], state["rows"], state)
    assert result["operations"] == [{"type": "重判", "attributes": ["材质"]}]
    assert "保留有证据的不同维度" in model.prompts[0]
    with pytest.raises(ValueError, match="未指定"):
        plan_action(Model(unclear, unclear), "重判所有的值，统一格式", ["材质"], state["rows"], state)


def test_reannotation_timeout_splits_batch_and_preserves_failed_row(workspace):
    from tools.attr_stability.step18_model import _annotate_batch
    state = workspace.open()
    response = {"rows": [{"row_id": "2", "attributes": {"功率":
                {"value": "1800 W", "source": "标题", "basis": "1800 W"}}}]}
    model = Model(TimeoutError("LLM 请求超时(>300s)"), response, TimeoutError("请求超时"))
    progress = []
    updates, warnings = _annotate_batch(model, "按原文重判", ["功率"], state["rows"], state, progress.append)
    assert updates[("2", "功率")]["value"] == "1800 W"
    assert warnings[0]["row_id"] == "3" and "保留原值" in warnings[0]["reason"]
    assert "拆成 1+1" in progress[0]
    with pytest.raises(RuntimeError, match="认证失败"):
        _annotate_batch(Model(RuntimeError("认证失败")), "重判", ["功率"], state["rows"][:1], state)


def test_blank_rejudge_does_not_include_confirmed_values_in_unfiltered_attribute(workspace):
    path = workspace.root / "17_属性标注/稳定性结果.json"
    data = read_json(path)
    data["deliverable"][0]["attributes"]["材质"] = ""
    write(path, data)
    model = Model({"rows": [{"row_id": "2", "attributes": {"材质":
                   {"value": "不锈钢", "source": "标题", "basis": "Stainless steel"}}}]})
    p = workspace.propose(request(workspace, ["材质"], "对空值和未明确的的值进行重判"), llm=model)
    assert p["plan"][0]["counts"]["材质"]["matched"] == 1
    assert {(x["row_id"], x["attribute"]) for x in p["patches"] if x["type"] == "cell"} == {("2", "材质")}


def test_union_scope_is_enforced_for_each_cell_in_mapping_plan(workspace):
    model = Model({"operations": [{"type": "归类", "attributes": ["材质", "功率"],
             "mappings": {"材质": {"ABS": "塑料"}, "功率": {"18000 W": "1800 W"}}}], "unresolved": []})
    req = request(workspace, ["材质", "功率"], "统一选中的材质和功率",
                  selection={"values": {"材质": ["ABS"], "功率": ["1800 W"]}})
    with pytest.raises(ValueError, match="不在所选单元格范围"):
        workspace.propose(req, llm=model)


@pytest.mark.parametrize("mode", ["text", "vision"])
def test_union_scope_is_enforced_for_every_rejudged_cell(workspace, mode):
    plan = {"operations": [{"type": "重判", "attributes": ["材质", "功率"]}], "unresolved": []}
    results = {"rows": [
        {"row_id": "2", "attributes": {"材质": {"value": "不锈钢", "source": "标题", "basis": "Stainless steel"},
                                        "功率": {"value": "1800 W", "source": "标题", "basis": "1800 W"}}},
        {"row_id": "3", "attributes": {"材质": {"value": "塑料", "source": "标题", "basis": "ABS housing"},
                                        "功率": {"value": "999 W", "source": "标题", "basis": "1800 W"}}}]}
    if mode == "text":
        model = Model(plan, results)
    else:
        class PerRowModel(Model):
            def vision(self, prompt, images, **kwargs):
                return results
        model = PerRowModel(plan)
    req = request(workspace, ["材质", "功率"], "按原文重判选中值",
                  selection={"values": {"材质": ["ABS"], "功率": ["18000 W"]}})
    req["mode"] = mode
    p = workspace.propose(req, llm=model)
    changed = {(x["row_id"], x["attribute"]) for x in p["patches"] if x["type"] == "cell"}
    assert changed == {("2", "功率"), ("3", "材质")}
    assert p["scope_counts"] == {"材质": 1, "功率": 1}


def test_unresolved_plan_aborts_and_compound_fill_uses_planning(workspace):
    from tools.attr_stability.step18_workspace import is_fill_unknown
    assert is_fill_unknown("把空值改成未明确")
    assert not is_fill_unknown("把空值改成未明确，同时把ABS统一为塑料")
    version = workspace.open()["version"]
    model = Model({"operations": [{"type": "归类", "mappings": {"材质": {"ABS": "塑料"}}}],
                   "unresolved": ["没有指定功率单位"]})
    with pytest.raises(ValueError, match="没有指定功率单位"):
        workspace.propose(request(workspace, ["材质"], "统一材质和功率"), llm=model)
    assert workspace.open()["version"] == version


def test_intersection_of_filters_and_no_scope_expansion(workspace):
    ws = workspace
    _, rows = ws.select(ws.open(), {"attributes": ["材质", "功率"],
                      "values": {"材质": ["金属", "ABS"], "功率": ["1800 W"]}})
    assert [r["id"] for r in rows] == ["2", "3"]
    _, rows = ws.select(ws.open(), {"attributes": ["材质", "功率"], "match": "all",
                      "values": {"材质": ["金属", "ABS"], "功率": ["1800 W"]}})
    assert [r["id"] for r in rows] == ["3"]


def test_restart_preserves_data_and_jobs_are_not_effective(workspace):
    original = merge(workspace, "材质", "ABS", "ABS塑料")
    second = Workspace(workspace.root, rules_path=workspace.rules_path)
    assert second.open() == original
    assert len(second.summary()["history"]) == 2


def test_cross_reannotation_and_selective_undo_preserve_later_cells(workspace):
    ws = workspace
    first = cross(ws)
    second = merge(ws, "功率", "18000 W", "1800 W")
    result = ws.restore_proposal(first["version"], selective=True, expected=second["version"])
    restored = ws.apply(result["id"], second["version"])
    row = restored["rows"][0]["attributes"]
    assert row["功率"]["value"] == "1800 W"
    assert row["是否带加热"]["value"] == "否"
    assert row["温度调节"]["value"] == "3档"
    assert row["是否带加热"]["basis"] == ws.chain()[-1]["rows"][0]["attributes"]["是否带加热"]["basis"]
    assert len(ws.chain()) == 4


def test_selective_undo_rejects_same_cell_later_even_if_changed_back(workspace):
    ws = workspace
    first = cross(ws)
    merge(ws, "温度调节", "低/中/高", "三档")
    last = merge(ws, "温度调节", "三档", "低/中/高")
    pointer = ws.pointer.read_bytes()
    blocked = ws.restore_proposal(first["version"], selective=True, expected=last["version"])
    assert blocked["blocked"] is True
    assert any(c["attribute"] == "温度调节" for c in blocked["conflicts"])
    assert ws.pointer.read_bytes() == pointer


def test_full_rollback_is_a_new_version_and_can_be_undone(workspace):
    ws = workspace
    first = cross(ws)
    second = merge(ws, "功率", "18000 W", "1800 W")
    p = ws.restore_proposal(first["version"], selective=False, expected=second["version"])
    rollback = ws.apply(p["id"], p["base_version"])
    assert rollback["rows"][0]["attributes"]["功率"]["value"] == "18000 W"
    assert len(ws.chain()) == 4
    p = ws.restore_proposal(rollback["version"], selective=True, expected=rollback["version"])
    restored = ws.apply(p["id"], p["base_version"])
    assert restored["rows"] == second["rows"]


def test_stale_proposal_and_changed_source_are_rejected(workspace):
    ws = workspace
    p = ws.propose(request(ws, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    cross(ws)
    with pytest.raises(WorkspaceConflict):
        ws.apply(p["id"], p["base_version"])
    state = ws.open()
    rules = read_json(ws.rules_path)
    rules["note"] = "updated"
    write(ws.rules_path, rules)
    assert ws.summary()["stale"]
    with pytest.raises(WorkspaceConflict):
        ws.export(state["version"])
    ws.refresh(state["version"])
    assert not ws.summary()["stale"]
    assert len(ws.chain()) == 3


def test_version_pointer_failure_leaves_previous_effective(workspace, monkeypatch):
    import tools.attr_stability.spec_io as mod
    ws = workspace
    p = ws.propose(request(ws, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    exported = ws.export(p["base_version"])
    output = ws.root / "18_标准化" / exported["filename"]
    old_output = output.read_bytes()
    old = ws.pointer.read_bytes()
    replace_original = mod.os.replace
    def fail(src, dest):
        if Path(dest) == ws.pointer:
            raise OSError("disk failure")
        return replace_original(src, dest)
    monkeypatch.setattr(mod.os, "replace", fail)
    with pytest.raises(OSError):
        ws.apply(p["id"], p["base_version"])
    assert ws.pointer.read_bytes() == old
    assert output.read_bytes() == old_output
    assert len(ws.chain()) == 1


@pytest.mark.parametrize("response", [
    {"rows": []},
    {"rows": [{"row_id": "999", "attributes": {}}]},
    {"rows": [{"row_id": "2", "attributes": {"材质": {"value": "钢"}}}]},
])
def test_model_coverage_failures_never_modify_current(workspace, response):
    ws = workspace
    old = ws.open()
    with pytest.raises(ValueError):
        ws.propose(request(ws, ["材质"]), llm=Model({"action": "reannotate"}, response))
    assert ws.open() == old


def test_unsupported_evidence_is_kept_for_human_review(workspace):
    ws = workspace
    model = Model({"action": "reannotate"}, {"rows": [
        {"row_id": "2", "attributes": {"材质": {"value": "铝", "source": "标题", "basis": "aluminum"}}}]})
    p = ws.propose(request(ws, ["材质"], selection={"row_ids": ["2"]}), llm=model)
    assert p["warnings"][0]["row_id"] == "2"
    assert p["changed_cells"] == 0
    assert "按要求复核" in model.prompts[1]


def test_vision_only_uses_the_target_rows_images(workspace):
    model = Model({"action": "reannotate"}, vision={"rows": [
        {"row_id": "2", "attributes": {"材质": {"value": "不锈钢", "source": "图片", "basis": "外壳钢制"}}}]})
    p = workspace.propose({**request(workspace, ["材质"], selection={"row_ids": ["2"]}), "mode": "vision"}, llm=model)
    assert model.images == [["https://example.com/a.jpg"]]
    assert p["patches"][0]["after"]["source"] == "图片"
    assert "非原文摘录" in p["patches"][0]["after"]["basis"]


@pytest.mark.parametrize("ident", ["../current", "V../bad", "P" + "x" * 32])
def test_invalid_record_paths_are_rejected(workspace, ident):
    workspace.open()
    with pytest.raises(ValueError):
        workspace.apply(ident, workspace.open()["version"])


def test_export_has_all_rows_evidence_versions_and_is_invalidated_by_edit(workspace):
    ws = workspace
    current = cross(ws)
    out = ws.export(current["version"])
    path = ws.root / "18_标准化" / out["filename"]
    wb = openpyxl.load_workbook(path, read_only=True)
    assert {"属性标注", "来源", "依据", "版本信息", "操作记录"} <= set(wb.sheetnames)
    assert wb["属性标注"].max_row == 3
    assert wb["版本信息"]["B1"].value == current["version"]
    header = [c.value for c in wb["属性标注"][1]]
    assert wb["属性标注"].cell(2, header.index("是否带加热") + 1).value == "是"
    assert wb["依据"].cell(2, header.index("是否带加热") + 1).value == "3 heat levels"
    wb.close()
    assert ws.summary()["export"]["stale"] is False
    assert ws.summary()["export"]["exists"] is True
    merge(ws, "材质", "ABS", "ABS塑料")
    assert ws.summary()["export"]["stale"] is True
    assert not path.exists(), "通用下载和下游发现不能继续读到旧交付"


def test_missing_export_file_requires_export_again(workspace):
    state = cross(workspace)
    out = workspace.export(state["version"])
    (workspace.root / "18_标准化" / out["filename"]).unlink()
    exported = workspace.summary()["export"]
    assert exported["exists"] is False
    assert exported["stale"] is True


def test_pipeline_uses_workspace_without_writing_upstream(workspace):
    ws = workspace
    cfg = Config({"CAT": ws.cat, "DATA_ROOT": str(ws.root.parent)})
    ctx = StepContext(cfg, Paths(cfg))
    before = fingerprint(ws.root)
    state = cross(ws)
    ws.export(state["version"])
    step = Step18Standardization()
    step.validate_upstream(ctx)
    step.run(ctx)
    assert fingerprint(ws.root) == before


def test_jobs_survive_completed_request_and_restart_marks_interrupted(workspace):
    from webui.step18_api import start_preview, job_status
    req = request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}})
    job = start_preview(workspace, req, {})
    for _ in range(100):
        state = job_status(workspace, job["job_id"])
        if state["status"] not in ("pending", "running"):
            break
        time.sleep(.01)
    assert state["status"] == "done"
    assert workspace.open()["version"] == req["expected_version"]
    ident = "J" + "a" * 32
    write(workspace._path("jobs", ident, "J"), {"id": ident, "status": "running"})
    assert job_status(workspace, ident)["status"] == "interrupted"


def test_job_list_preserves_selected_values_and_legacy_requirement(workspace):
    from webui.step18_api import list_jobs
    ident = "J" + "b" * 32
    write(workspace._path("jobs", ident, "J"), {"id": ident, "status": "done",
        "created_at": "2026-10-29T07:00:00+00:00", "result": {
            "id": "P" + "c" * 32, "requirement": "按我的口径\n逐行复核",
            "selection": {"attributes": ["材质"], "values": {"材质": ["", "未明确"]}},
            "patches": [], "changed_cells": 0}})
    job = next(j for j in list_jobs(workspace) if j["id"] == ident)
    assert job["requirement"] == "按我的口径\n逐行复核"
    assert job["selection"]["values"]["材质"] == ["", "未明确"]


def test_parallel_jobs_keep_independent_changes_even_when_one_finishes_later(workspace):
    import threading
    from webui.step18_api import start_preview, job_status
    entered, release = threading.Event(), threading.Event()
    class SlowModel(Model):
        def chat_json(self, prompt, **kwargs):
            if not self.prompts:
                entered.set()
                assert release.wait(10)
            return super().chat_json(prompt, **kwargs)
    model = SlowModel({"action": "reannotate"}, {"rows": [
        {"row_id": "2", "attributes": {"功率": {"value": "1800 W", "source": "标题", "basis": "1800 W"}}}]})
    initial = workspace.open()["version"]
    slow = start_preview(workspace, request(workspace, ["功率"], selection={"row_ids": ["2"]}), {}, client_factory=lambda _: model)
    assert entered.wait(5)
    fast = start_preview(workspace, request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}), {})
    def finished(job):
        for _ in range(300):
            result = job_status(workspace, job["job_id"])
            if result["status"] not in ("pending", "running"):
                return result
            time.sleep(.01)
        raise AssertionError("job did not finish")
    try:
        first = finished(fast)
        assert first["status"] == "done"
        current = workspace.apply(first["result"]["id"], initial)
        release.set()
        second = finished(slow)
        assert second["status"] == "done"
        final = workspace.apply(second["result"]["id"], current["version"])
        assert final["rows"][0]["attributes"]["功率"]["value"] == "1800 W"
        assert final["rows"][1]["attributes"]["材质"]["value"] == "ABS塑料"
    finally:
        release.set()


def test_parallel_overlapping_previews_follow_last_confirmation(workspace):
    base = workspace.open()["version"]
    one = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    two = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "塑料"}}))
    current = workspace.apply(one["id"], base)
    final = workspace.apply(two["id"], current["version"])
    assert final["rows"][1]["attributes"]["材质"]["value"] == "塑料"


def test_http_facade_reads_and_exports_real_workspace(workspace, monkeypatch):
    module_path = Path(__file__).parents[1] / "webui/server.py"
    spec = importlib.util.spec_from_file_location("step18_test_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = workspace.root / "config.yaml"
    config.write_text("CAT: " + workspace.cat + "\nDATA_ROOT: '" + str(workspace.root.parent) + "'\n", encoding="utf-8")
    handler = module.Handler.__new__(module.Handler)
    summary = handler._api_std_workspace({"file": [str(config)]})
    assert summary["ready"] and summary["row_count"] == 2
    out = handler._api_std_workspace_action("export", {"config_file": str(config), "expected_version": summary["version"]})
    assert out["row_count"] == 2


def test_http_apply_returns_only_changed_attribute_and_current_metadata(workspace, monkeypatch):
    module_path = Path(__file__).parents[1] / "webui/server.py"
    spec = importlib.util.spec_from_file_location("step18_partial_test_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = module.Handler.__new__(module.Handler)
    monkeypatch.setattr(handler, "_std_workspace", lambda _: workspace)
    monkeypatch.setattr(module, "_lock_live_started", lambda *_: (False, None, False))
    state = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    result = handler._api_std_workspace_action("apply", {"expected_version": state["version"],
            "proposal_id": proposal["id"], "partial": True})
    assert result["partial"] is True
    assert [a["name"] for a in result["attributes"]] == ["材质"]
    assert result["version"] == workspace.open()["version"]
    assert result["changed_attributes"] == {"材质": 1}
    assert "issues" in result and result["history"][0]["kind"] == "归类"
    assert result["history_partial"] is True
    assert result["issues"]["scope"] == ["材质"]
    assert result["issues"]["numeric"] == []


def test_scoped_issues_recheck_only_related_cross_attribute_rules(workspace):
    state = workspace.open()
    before = workspace.issues(state)
    assert before["cross"] and before["numeric"]
    updated = cross(workspace)
    partial = workspace.issues(updated, attribute_names={"是否带加热", "温度调节"})
    assert partial["cross"] == []
    assert partial["numeric"] == []
    assert workspace.issues(updated)["numeric"] == before["numeric"]


def test_workspace_archive_preserves_nested_history(workspace, tmp_path):
    from core.archive import sync_step_dir
    ws = workspace
    original = merge(ws, "材质", "ABS", "ABS塑料")
    cfg = Config({"CAT": ws.cat, "DATA_ROOT": str(ws.root.parent)})
    paths = Paths(cfg)
    snapshot = tmp_path / "snapshot"
    sync_step_dir(18, str(ws.store), str(snapshot), paths)
    archived = snapshot / "18_标准化/版本数据/versions" / (original["version"] + ".json")
    assert archived.exists()


@pytest.mark.parametrize("selection", [[], {"attributes": [[]]},
    {"attributes": ["材质"], "row_ids": [{}]}])
def test_malformed_selection_is_user_error(workspace, selection):
    with pytest.raises(ValueError):
        workspace.select(workspace.open(), selection)


def test_numeric_evidence_issue_is_available_for_one_click_selection(workspace):
    issues = workspace.summary()["issues"]["numeric"]
    assert any(i["属性"] == "功率" and str(i["行"]) == "2" for i in issues)


def test_numeric_metadata_and_rule_are_available_to_model(workspace):
    from tools.attr_stability.step18_model import _prompt
    summary = workspace.summary()
    power = next(a for a in summary["attributes"] if a["name"] == "功率")
    assert power["rule"] == "numeric" and power["unit"] == "W"
    state = workspace.open()
    prompt = _prompt("统一功率单位", ["功率"], state["rows"], state)
    assert '"unit": "W"' in prompt and "统一功率单位" in prompt


@pytest.mark.parametrize(("raw", "group"), [
    ("", "空值／未明确"), ("未明确", "空值／未明确"),
    ("120mL", "mL"), ("120毫升", "mL"), ("2L", "L"), ("1kg", "kg"),
    ("100g", "g"), ("3oz", "oz（口径待确认）"), ("3 fl oz", "fl oz"),
    ("3/4 cup", "cup"), ("100g/3.5oz", "复合／多单位"),
    ("48g/136g", "复合／多单位"), ("0.7cup(约166ml)", "复合／多单位"),
    ("8 cubic inches", "立方英寸"), ("其他", "其他／无单位"),
])
def test_numeric_value_groups_are_display_only(raw, group):
    from tools.attr_stability.step18_workspace import numeric_value_group
    assert numeric_value_group(raw) == group


def test_numeric_summary_includes_original_values_and_unit_groups(workspace):
    power = next(a for a in workspace.summary()["attributes"] if a["name"] == "功率")
    assert {v["value"]: v["unit_group"] for v in power["values"]} == {"18000 W": "W", "1800 W": "W"}


def test_auto_vision_failure_keeps_text_result_and_warns(workspace):
    class NoVision(Model):
        def vision(self, *args, **kwargs):
            raise RuntimeError("unavailable vision service")
    model = NoVision({"action": "reannotate"}, {"rows": [
        {"row_id": "2", "attributes": {"材质": {"value": "未明确", "source": "", "basis": "无文字依据"}}}]})
    req = {**request(workspace, ["材质"], selection={"row_ids": ["2"]}), "mode": "auto"}
    proposal = workspace.propose(req, llm=model)
    assert "图片补判未完成" in proposal["warnings"][0]["reason"]
    assert proposal["patches"][0]["after"]["value"] == "未明确"


def test_model_failure_persists_failed_job_without_changing_data(workspace):
    from webui.step18_api import start_preview, job_status
    req = request(workspace, ["材质"])
    job = start_preview(workspace, req, {"LLM_API_KEY": "test-secret-key"},
        client_factory=lambda _: Model(RuntimeError("bad test-secret-key")))
    for _ in range(100):
        result = job_status(workspace, job["job_id"])
        if result["status"] not in ("pending", "running"):
            break
        time.sleep(.01)
    assert result["status"] == "failed"
    assert "test-secret-key" not in result["message"]
    assert workspace.open()["version"] == req["expected_version"]


def test_repeated_rename_collapses_aliases_and_undo_restores_them(workspace):
    first = merge(workspace, "材质", "ABS", "ABS塑料")
    second = merge(workspace, "材质", "ABS塑料", "塑料")
    aliases = workspace._defs(second)["材质"]["params"]["aliases"]
    assert aliases["ABS"] == aliases["ABS塑料"] == "塑料"
    p = workspace.restore_proposal(second["version"], selective=True, expected=second["version"])
    undo = workspace.apply(p["id"], second["version"])
    assert workspace._defs(undo) == workspace._defs(first)


def test_reset18_preserves_history_and_requires_new_export(workspace):
    from core.orchestrator import PipelineRunner
    ws = workspace
    state = cross(ws)
    ws.export(state["version"])
    saved_proposal = ws.propose(request(ws, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    job_path = ws._path("jobs", "J" + "1" * 32, "J")
    write(job_path, {"id": job_path.stem, "status": "failed", "message": "测试记录"})
    keep = [ws.root / "17_属性标注/input.json", ws.root / "17_属性标注/稳定性结果.json",
            ws.spec_path, ws.rules_path, ws.root / "18_标准化/词表聚类报告.json",
            ws.root / "18_标准化/词表聚类历史.json", ws.root / "18_标准化/待确认清单.json",
            ws.root / "18_标准化/待确认清单.md"]
    for p in keep[-2:]:
        p.write_text("protected upstream", encoding="utf-8")
    before = {p: p.read_bytes() for p in keep}
    cfg = Config({"CAT": ws.cat, "DATA_ROOT": str(ws.root.parent)})
    runner = PipelineRunner(cfg)
    snap = Path(runner.snapshot_dir())
    from core.archive import sync_step_dir, sync_step_files
    sync_step_dir(18, str(ws.store), str(snap), runner.paths)
    old_workbook = ws.root / "18_标准化" / ws.export.__globals__["OUTPUT"].format(cat=ws.cat)
    sync_step_files(18, [str(old_workbook)], str(snap), runner.paths)
    runner.reset_steps_from(18)
    assert ws.open()["version"] == state["version"]
    assert job_path.exists() and ws._path("proposals", saved_proposal["id"], "P").exists()
    assert not (ws.store / "export.json").exists()
    archived = snap / "18_标准化"
    assert (archived / "版本数据/current.json").exists()
    assert (archived / "版本数据/jobs" / job_path.name).exists()
    assert not (archived / "版本数据/export.json").exists()
    assert not (archived / old_workbook.name).exists()
    assert not (ws.root / "18_标准化" / ws.export.__globals__["OUTPUT"].format(cat=ws.cat)).exists()
    assert all(p.read_bytes() == content for p, content in before.items())
    from core.errors import WaitingForHuman
    step = Step18Standardization()
    ctx = StepContext(cfg, Paths(cfg))
    step._rules_json_path = lambda ctx: str(ws.rules_path)
    with pytest.raises(WaitingForHuman, match="尚未确认导出"):
        step.validate_upstream(ctx)
    ws.export(state["version"])
    assert (ws.store / "export.json").exists()


def test_reset18_refuses_to_run_during_export(workspace):
    from core.orchestrator import PipelineRunner
    from tools.attr_stability.export_state import export_operation
    ws = workspace
    state = ws.open()
    cfg = Config({"CAT": ws.cat, "DATA_ROOT": str(ws.root.parent)})
    with export_operation(ws.root, state["version"]):
        with pytest.raises(ValueError, match="正在导出"):
            PipelineRunner(cfg).reset_steps_from(18)
        assert ws.open()["version"] == state["version"]


def test_missing_llm_mapping_never_publishes_data(workspace):
    original = workspace.open()["version"]
    with pytest.raises(ValueError, match="归类映射"):
        workspace.propose(request(workspace, ["材质"]), llm=Model({"action": "merge"}))
    assert workspace.open()["version"] == original


@pytest.mark.parametrize("success_size,expected", [(7, [7]), (4, [7, 4]), (2, [7, 4, 2]), (1, [7, 4, 2, 1])])
def test_vision_400_reduces_images_and_records_actual_evidence(workspace, success_size, expected):
    class ImageModel(Model):
        def vision(self, prompt, images, **kwargs):
            self.images.append(list(images))
            assert kwargs["detail"] == "low"
            if len(images) > success_size:
                raise RuntimeError("HTTP 400 Bad Request")
            return {"rows": [{"row_id": "2", "attributes": {
                "材质": {"value": "不锈钢", "source": "图片", "basis": "金属外壳"}}}]}
    ip = workspace.root / "17_属性标注/input.json"
    inputs = read_json(ip)
    images = ["https://example.com/image%d.jpg" % i for i in range(7)]
    inputs["rows"][0]["图片链接"] = " ".join(images)
    write(ip, inputs)
    rp = workspace.root / "17_属性标注/稳定性结果.json"
    raw = read_json(rp)
    raw["annotation_contract"] = make_annotation_contract(read_json(workspace.spec_path), inputs["rows"],
                                                           source_path="qa", input_mode="snapshot")
    raw["annotation_contract"]["completed"] = True
    write(rp, raw)
    model = ImageModel({"action": "reannotate"})
    proposal = workspace.propose({**request(workspace, ["材质"], selection={"row_ids": ["2"]}), "mode": "vision"}, llm=model)
    assert [len(i) for i in model.images] == expected
    cell = next(p for p in proposal["patches"] if p["type"] == "cell")["after"]
    assert cell["images"] == images[:success_size]
    assert cell["image_count_used"] == success_size
    assert bool(proposal["warnings"]) == (success_size < 7)


def test_vision_retry_progress_keeps_row_position(workspace):
    from tools.attr_stability.step18_model import reannotate
    state = workspace.open()
    rows = copy.deepcopy(state["rows"])
    for row in rows:
        row["input"]["图片链接"] = "https://example.com/one.jpg https://example.com/two.jpg"

    class RetryModel:
        def vision(self, prompt, images, **kwargs):
            if len(images) > 1:
                raise ValueError("400 Bad Request")
            rid = "2" if '"row_id": "2"' in prompt else "3"
            return {"rows": [{"row_id": rid, "attributes": {"材质": {
                "value": "塑料", "source": "图片", "basis": "塑料外壳"}}}]}

    messages = []
    reannotate(RetryModel(), "按图片复核", "vision", ["材质"], rows, state, messages.append)
    assert "图片复核：第1/2行（商品行2） · 尝试2/2张图片" in messages
    assert "图片复核：第1/2行（商品行2） · 尝试1/2张图片" in messages
    assert "图片复核：已检查1/2行" in messages
    assert "图片复核：第2/2行（商品行3） · 尝试2/2张图片" in messages
    assert messages[-1] == "图片复核：已检查2/2行"


def test_vision_non400_does_not_reduce_and_duplicate_tiers_are_skipped():
    from tools.attr_stability.step18_model import vision_with_fallback
    class Failure(Model):
        def vision(self, prompt, images, **kwargs):
            self.images.append(images)
            raise RuntimeError(self.answers[0])
    error = Failure("401 unauthorized")
    with pytest.raises(RuntimeError, match="401"):
        vision_with_fallback(error, "test", ["a"] * 7)
    assert len(error.images) == 1
    small = Failure("400 Bad Request")
    with pytest.raises(RuntimeError, match="400"):
        vision_with_fallback(small, "test", ["a"] * 2)
    assert [len(i) for i in small.images] == [2, 1]


@pytest.mark.parametrize("requirement", [
    "统一容量规格的单位和写法。明确等价的 cup、mL、L 写法进行归并；仅在商品原文明确是液体体积且单位依据充分时换算为 mL 或 L。多碗、套装、范围值以及干粮和饮水容量分别保留，不合并为单一数值。证据不足时保留原值，并记录原因和依据。",
    "归并已有表达，只有商品标题明确为液体容量时才换算。",
    "先归并同义值，若商品资料确认适用，再按商品判断。",
])
def test_capacity_conditional_evidence_is_not_value_only(requirement):
    from tools.attr_stability.step18_workspace import is_value_only_classification
    assert not is_value_only_classification(requirement)

@pytest.mark.parametrize("requirement", [
    "仅根据已有材质归类，不读取商品原文或图片，不重判。无法确定归属的值保留原样。",
    "仅按已有值归并，只有商品原文明确时才换算。",
    "将各种材质按属性值粗分类",
])
def test_value_only_explicit_remains_restricted(requirement):
    from tools.attr_stability.step18_workspace import is_value_only_classification
    assert is_value_only_classification(requirement)

def test_capacity_conditional_evidence_routes_to_normal_planner(workspace, monkeypatch):
    from tools.attr_stability import step18_model
    captured = {}
    def fake_plan(llm,requirement,attrs,targets,base,scope,value_only):
        captured["value_only"] = value_only
        raise ValueError("route checked")
    monkeypatch.setattr(step18_model,"plan_action",fake_plan)
    requirement = "统一容量规格的单位和写法。明确等价的 cup、mL、L 写法进行归并；仅在商品原文明确是液体体积且单位依据充分时换算为 mL。"
    with pytest.raises(ValueError,match="route checked"):
        workspace.propose(request(workspace,["材质","功率"],requirement),llm=object())
    assert captured["value_only"] is False
