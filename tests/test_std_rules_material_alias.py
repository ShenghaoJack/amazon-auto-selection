# 单元测试: 过滤材料「椰壳活性炭 → 活性炭」归并（声明式别名）
#
# 背景(2026-09-21, 用户要求): 「过滤材料_标准 中包含椰壳活性炭的都改成活性炭」。
# 同 #15「塑料细分牌号归并到塑料」的口径 —— 细分牌号不单独成类。
# 实测: 51 行含椰壳活性炭；其中 **2 行同时含 活性炭 与 椰壳活性炭**，
#       归并后必须**去重**并把 过滤材料种类数_标准 减 1。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

MAT, CNT = "过滤材料_标准", "过滤材料种类数_标准"


def _rules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _attr(order=True):
    rules = _rules()
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    a = json.loads(json.dumps(a))
    if order:
        a["params"]["material_order"] = rules["value_order"][MAT]
    return a


def test_alias_declared_in_rules():
    """别名必须声明在类目文件里(驱动/引擎不含类目词)。"""
    rules = _rules()
    assert (rules.get("value_aliases") or {}).get(MAT, {}).get("椰壳活性炭") == "活性炭"
    # material_map 侧也要能处理文本来源
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    assert a["params"]["material_map"]["椰壳活性炭"] == "活性炭"


def test_coconut_maps_to_carbon_in_rule():
    sub, _, _, _ = apply_rule(_attr(), "椰壳活性炭", {})
    assert sub[MAT] == "活性炭"
    assert sub[CNT] == 1


def test_coconut_inside_multi_value():
    sub, _, _, _ = apply_rule(_attr(), "过滤棉+椰壳活性炭+离子交换树脂", {})
    assert sub[MAT] == "过滤棉+活性炭+离子交换树脂"
    assert sub[CNT] == 3
    assert "椰壳活性炭" not in sub[MAT]


def test_collision_dedupes():
    """⚠ 同时含 活性炭 与 椰壳活性炭 的原值, 归并后必须去重且种类数-1。"""
    sub, _, _, _ = apply_rule(_attr(), "过滤棉+海绵+活性炭+椰壳活性炭+离子交换树脂", {})
    assert sub[MAT] == "过滤棉+海绵+活性炭+离子交换树脂"
    assert sub[CNT] == 4, "去重后应为 4 种"
    assert sub[MAT].count("活性炭") == 1


def test_declared_count_pair_present():
    """计数字列联动必须声明在属性 params 里(驱动借此重算, 不硬编码)。"""
    a = next(x for x in _rules()["attributes"] if x["name"] == "过滤类型")
    assert a["params"].get("count_subcol") == CNT
    assert a["params"].get("count_source_subcol") == MAT


def test_other_materials_unaffected():
    assert apply_rule(_attr(), "活性炭+海绵", {})[0][MAT] == "海绵+活性炭"
    assert apply_rule(_attr(), "滤芯", {})[0][MAT] == "滤芯"
    assert apply_rule(_attr(), "4层过滤", {})[0][MAT] == "未明确"
