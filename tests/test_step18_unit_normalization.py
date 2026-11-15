import json

import openpyxl
import pytest

from tools.attr_stability.numeric_values import normalize_numeric
from tools.attr_stability.spec_io import make_annotation_contract
from tools.attr_stability.step18_model import plan_action
from tools.attr_stability.step18_workspace import Workspace, explicit_unit_requirement
from tools.attr_stability.units import convert, parse_measure, quantity_of_unit


@pytest.mark.parametrize("raw, expected", [
    ("1m²", 10.7639104167), ("1㎡", 10.7639104167), ("1平方米", 10.7639104167),
    ("60 square_meters", 645.834625003), ("250 square_centimeters", 0.269097760417),
    ("144 sq.in", 1), ("144 square_inches", 1), ("464平方厘米", 0.499445443335),
    ("1ft²", 1), ("1 square_feet", 1),
])
def test_area_aliases_use_squared_factors(raw, expected):
    converted, status = normalize_numeric(raw, {"unit": "sq.ft"})
    assert status == "ok" and float(converted[:-5]) == pytest.approx(expected)
    assert quantity_of_unit("sq.ft") == "area"


@pytest.mark.parametrize("raw", ["100 cubic feet", "15 cubic feet", "27 cubic feet", "30 cubic feet",
                                  "300 cubic feet", "50 cubic feet", "60 cubic feet", "5L"])
def test_volume_never_becomes_area(raw):
    assert parse_measure(raw)[1] == "volume"
    assert normalize_numeric(raw, {"unit": "sq.ft"})[1] == "failed"
    assert convert(100, "cubic feet", "sq.ft") is None


@pytest.mark.parametrize("raw", ["30-350 sq.ft", "1000 sq.ft", "0.417 sq.ft", "30.0–350.00 sq.ft"])
def test_existing_target_scalar_and_interval_keep_original_text(raw):
    assert normalize_numeric(raw, {"unit": "sq.ft"}, preserve_target_format=True) == (raw, "ok")


def test_area_intervals_convert_both_bounds_without_collapsing():
    text, status = normalize_numeric("1-2m²", {"unit": "sq.ft"})
    assert status == "ok"
    lo, hi = map(float, text[:-5].split("-"))
    assert lo == pytest.approx(10.7639104167) and hi == pytest.approx(21.5278208334)
    assert normalize_numeric("2-1m²", {"unit": "sq.ft"})[1] == "failed"
    assert normalize_numeric("1m²-2L", {"unit": "sq.ft"})[1] == "failed"


@pytest.mark.parametrize("requirement, target", [("单位统一为sq.ft", "sq.ft"), ("统一单位为sq.ft", "sq.ft"),
    ("将覆盖面积统一为平方米", "平方米"), ("把功率统一成W", "W"), ("覆盖面积的单位统一为sq.ft", "sq.ft")])
def test_explicit_unit_request_does_not_need_model(requirement, target):
    assert explicit_unit_requirement(requirement, ["覆盖面积", "功率"]) == target
    result = plan_action(None, requirement, ["覆盖面积", "功率"], [], {})
    assert result["operations"][0]["unit"] == target


@pytest.mark.parametrize("requirement", ["单位统一为sq.ft，同时删除未明确", "单位统一为sq.ft并取最大值",
    "单位统一为sq.ft，重判体积值", "单位统一", "单位统一为未知单位", "单位统一为oz"])
def test_compound_or_ambiguous_instructions_still_require_planning(requirement):
    assert explicit_unit_requirement(requirement, ["覆盖面积"]) is None


def build_workspace(tmp_path, values):
    root = tmp_path / "Area-US-202611"
    root.mkdir()
    spec = {"cat": root.name, "status": "confirmed", "definition_revision": "test-v1",
            "attributes": [{"name": "覆盖面积", "hint": "保留原始物理量"}]}
    definitions = {"attributes": [{"name": "覆盖面积", "rule": "enum",
        "params": {"values": [v for v in values if v not in ("", "未明确")], "aliases": {}}}]}
    inputs = [{"xl_row": i + 2, "变体ASIN": f"B{i:09}", "标题": v} for i, v in enumerate(values)]
    deliverable = [{"xl_row": i + 2, "attributes": {"覆盖面积": v, "覆盖面积_来源": "标题", "覆盖面积_依据": v}}
                   for i, v in enumerate(values)]
    contract = make_annotation_contract(spec, inputs, source_path="unused", input_mode="snapshot")
    contract["completed"] = True
    for path, data in ((root / f"{root.name}-attribute-spec.json", spec), (root / "area.rules.json", definitions),
                       (root / "17_属性标注/input.json", {"rows": inputs}),
                       (root / "17_属性标注/稳定性结果.json", {"deliverable": deliverable, "cells": {}, "annotation_contract": contract})):
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Workspace(root, rules_path=root / "area.rules.json")


def test_enum_numeric_values_convert_partially_preserve_evidence_and_export(tmp_path):
    values = ["1m²", "144 square_inches", "30-350 sq.ft", "5L", "100 cubic feet", "19.39", "未明确"]
    workspace = build_workspace(tmp_path, values)
    before = workspace.open()
    class NoModel:
        def chat_json(self, *args, **kwargs):
            pytest.fail("单纯单位整理不应调用模型")
    proposal = workspace.propose({"expected_version": before["version"], "requirement": "单位统一为sq.ft",
        "selection": {"attributes": ["覆盖面积"]}}, llm=NoModel())
    assert proposal["plan"][0]["counts"]["覆盖面积"] == {"matched": 7, "changed": 2, "unchanged": 5}
    assert len(proposal["warnings"]) == 4
    assert workspace.open()["version"] == before["version"]  # Preview alone never applies changes.
    applied = workspace.apply(proposal["id"], before["version"])
    actual = [r["attributes"]["覆盖面积"]["value"] for r in applied["rows"]]
    assert actual == ["10.7639104167sq.ft", "1sq.ft", *values[2:]]
    assert applied["rows"][0]["attributes"]["覆盖面积"]["basis"] == "1m²"
    definition = workspace._defs(applied)["覆盖面积"]
    assert definition["rule"] == "enum"
    assert definition["params"]["aliases"]["1m²"] == actual[0]
    workspace.export(applied["version"])
    book = openpyxl.load_workbook(workspace.root / "18_标准化" / f"{workspace.cat}-属性标注-标准化交付.xlsx",
                                  read_only=True, data_only=True)
    try:
        rows = list(book["属性标注"].iter_rows(values_only=True))
        col = rows[0].index("覆盖面积")
        assert [r[col] for r in rows[1:]] == actual
    finally:
        book.close()


def test_unit_conversion_obeys_selected_rows_and_values(tmp_path):
    workspace = build_workspace(tmp_path, ["1m²", "2m²", "3m²"])
    before = workspace.open()
    proposal = workspace.propose({"expected_version": before["version"], "requirement": "单位统一为sq.ft",
        "selection": {"attributes": ["覆盖面积"], "row_ids": ["2", "3"], "values": {"覆盖面积": ["2m²"]}}})
    assert proposal["changed_cells"] == 1
    applied = workspace.apply(proposal["id"], before["version"])
    assert [r["attributes"]["覆盖面积"]["value"] for r in applied["rows"]] == ["1m²", "21.5278208334sq.ft", "3m²"]
