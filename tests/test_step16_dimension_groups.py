# -*- coding: utf-8 -*-
"""步骤16 维度分组建议 + 取值形态约定（2026-11-11）。"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from steps.step16_attribute_annotation import Step16AttributeAnnotation as S  # noqa: E402
from tools.attr_stability.spec import build_hint_summary_prompt  # noqa: E402

SRC = (ROOT / "steps" / "step16_attribute_annotation.py").read_text(encoding="utf-8")


def _attrs(*names):
    return [{"name": n, "hint": "h-" + n} for n in names]


def test_valid_group_marks_members_without_deleting():
    attrs = _attrs("是否带支架", "支架高度", "高度档位数", "颜色")
    groups = S._apply_dimension_groups(attrs, {"groups": [
        {"维度": "支架高度", "主属性": "支架高度", "成员": ["是否带支架", "支架高度", "高度档位数"],
         "建议": "可收拢", "理由": "无支架可写不适用"}]})
    assert len(groups) == 1 and len(attrs) == 4
    by = {a["name"]: a for a in attrs}
    assert by["支架高度"]["维度建议"] == "主属性"
    assert by["是否带支架"]["维度建议"].startswith("可收拢到「支架高度」")
    assert "维度分组" not in by["颜色"]


def test_rejects_unknown_overlapping_and_singleton_groups():
    attrs = _attrs("A", "B", "C")
    groups = S._apply_dimension_groups(attrs, {"groups": [
        {"主属性": "A", "成员": ["A", "虚构"], "建议": "保留"},       # 虚构成员
        {"主属性": "A", "成员": ["A"], "建议": "保留"},               # 单成员
        {"主属性": "A", "成员": ["A", "B"], "建议": "随便"},           # 建议非法
        {"主属性": "A", "成员": ["A", "B"], "建议": "保留"},           # 合法
        {"主属性": "C", "成员": ["B", "C"], "建议": "保留"},           # 与上组重叠
    ]})
    assert [g["成员"] for g in groups] == [["A", "B"]]
    assert "维度分组" not in attrs[2]


def test_bad_response_is_ignored():
    attrs = _attrs("A", "B")
    assert S._apply_dimension_groups(attrs, None) == []
    assert S._apply_dimension_groups(attrs, {"groups": "x"}) == []


def test_group_prompt_avoids_mock_trigger_words():
    p = S._dimension_group_prompt(_attrs("A", "B"))
    assert "提取" not in p and "标注" not in p


def test_extract_prompt_requires_value_form_and_has_no_pet_examples():
    assert "形态：X；" in SRC and "多个值统一用「、」分隔" in SRC
    for word in ("食盆", "粮仓", "餐格", "慢饮"):
        assert word not in SRC, word


def test_hint_summary_keeps_value_form():
    p = build_hint_summary_prompt([{"name": "颜色", "hint": "形态：多选；多值用「、」分隔"}])
    assert "形态：…；" in p
