# 单元测试: `过滤类型_标准` 去掉「（材料已明确）/（材料未明确）」后缀
#
# 背景(2026-09-22, 用户要求): 材料是否明确，`过滤材料_标准` 已经表达；
#   类型列只保留**结构分类**：有过滤系统 / 有滤芯 / 无需过滤器 / 未明确。
# 实现: 声明式 `value_aliases["过滤类型_标准"]` —— 规则侧仍产出带后缀的完整语义值，
#   驱动在所有覆盖(含视觉文件)之后归一；这样文本来源与视觉来源写法一致。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

TYP = "过滤类型_标准"
MAT = "过滤材料_标准"
CNT = "过滤材料种类数_标准"


def _rules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def test_alias_declared():
    """别名必须声明在类目文件里（驱动/引擎不含类目词）。"""
    va = (_rules().get("value_aliases") or {}).get(TYP)
    assert va == {"有过滤系统（材料已明确）": "有过滤系统",
                  "有过滤系统（材料未明确）": "有过滤系统",
                  "有滤芯（材料未明确）": "有滤芯"}


def test_rule_still_emits_full_semantic_value():
    """规则侧保持完整语义值（带后缀）——归一发生在驱动侧。"""
    rules = _rules()
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    assert apply_rule(a, "5层过滤：活性炭+过滤棉", {})[0][TYP] == "有过滤系统（材料已明确）"
    assert apply_rule(a, "4层过滤", {})[0][TYP] == "有过滤系统（材料未明确）"
    assert apply_rule(a, "滤芯", {})[0][TYP] == "有滤芯（材料未明确）"


def test_normalization_yields_clean_values():
    """按声明归一后的取值域只应有 4 个（结构与材料形态对应）。"""
    va = (_rules().get("value_aliases") or {})[TYP]
    rules = _rules()
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    out = set()
    for raw in ["5层过滤：活性炭+过滤棉", "4层过滤", "多层过滤", "滤芯", "无需过滤器", "无需滤芯", "未明确"]:
        v = apply_rule(a, raw, {})[0][TYP]
        out.add(va.get(v, v))
    assert out <= {"有过滤系统", "有滤芯", "无需过滤器", "未明确"}, out
    assert "有过滤系统" in out and "有滤芯" in out


def test_no_parenthetical_left_anywhere_in_alias_targets():
    """归一目标值里不得再出现「材料已明确/材料未明确」。"""
    va = (_rules().get("value_aliases") or {})[TYP]
    for tgt in va.values():
        assert "材料已明确" not in tgt and "材料未明确" not in tgt


def test_material_column_unaffected():
    """类型归一不影响材料列的子列数。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "Cat Fountains-US-202608")
    rules = _rules()
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    cv = apply_rule(a, "5层过滤：活性炭+过滤棉", {})[0]
    assert set(cv) == {"过滤层数（营销宣称）_标准", MAT, CNT, TYP}
