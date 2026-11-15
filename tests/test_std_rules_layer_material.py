# 单元测试: 过滤类型(layer_material) 的括号归一 / 中文数字层数 / 别名 / 跨列自洽
#
# 背景(2026-09-21, 用户要求「验证过滤材料_标准 与 过滤材料种类数_标准」):
#   审计出 4 类缺陷 ——
#     ① 全角括号未剥: `2层过滤（活性炭+海绵）` 拆成 `（活性炭` / `海绵）` 两个词表外材料(4 行)
#     ② 中文数字层数: `三层过滤` / `三重过滤` → 层数留 未明确, 且「三层」被当成材料名(3 行)
#     ③ 无语义原值残留: `垂直过滤系统` → 材料=`垂直`; `集成过滤系统` → 材料=`集成`(2 行)
#     ④ 别名缺口: `碳过滤器` / `医用石` / `微孔过滤棉` / `可重复使用的粗过滤器`(7 行)
#   另有 35 行跨列冲突: 视觉只填了材料却没更新 `过滤类型_标准`。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

SUBS = ["过滤层数（营销宣称）_标准", "过滤材料_标准", "过滤材料种类数_标准", "过滤类型_标准"]


def _cat_attr():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        rules = json.load(fh)
    return next(a for a in rules["attributes"] if a["name"] == "过滤类型")


def _run(raw):
    cv, st, mt, iss = apply_rule(_cat_attr(), raw, {})
    return cv[SUBS[0]], cv[SUBS[1]], cv[SUBS[2]], cv[SUBS[3]]


def test_attr_has_strip_parens():
    assert _cat_attr()["params"].get("strip_parens") is True


@pytest.mark.parametrize("raw,mat,cnt", [
    ("2层过滤（活性炭+海绵）", "活性炭+海绵", 2),
    ("4层过滤（活性炭+离子交换树脂+海绵）", "活性炭+离子交换树脂+海绵", 3),
    ("3层过滤(活性炭+过滤棉)", "活性炭+过滤棉", 2),
])
def test_fullwidth_parens_stripped(raw, mat, cnt):
    """全角/半角括号必须剥掉, 否则拆出「（活性炭」这种词表外材料。"""
    _lay, m, c, _t = _run(raw)
    assert m == mat
    assert c == cnt
    assert "（" not in m and "）" not in m and "(" not in m


@pytest.mark.parametrize("raw", ["三层过滤", "三重过滤"])
def test_chinese_numeral_layer(raw):
    """中文数字层数: 层数=3, 且「三层/三重」不得被当成材料。"""
    lay, m, c, t = _run(raw)
    assert lay == 3
    assert m == "未明确"
    assert c == "未明确"
    assert t == "有过滤系统（材料未明确）"


@pytest.mark.parametrize("raw", ["垂直过滤系统", "集成过滤系统"])
def test_meaningless_residue_not_treated_as_material(raw):
    """后缀正则剥掉「过滤系统」后剩下的「垂直」「集成」不是材料。"""
    _lay, m, c, t = _run(raw)
    assert m == "未明确"
    assert t == "有过滤系统（材料未明确）"


@pytest.mark.parametrize("raw,mat", [
    ("碳过滤器", "活性炭"),
    ("5层过滤：活性炭+离子交换树脂+无纺布+海绵+医用石", "活性炭+离子交换树脂+无纺布+海绵+麦饭石"),
    ("6层过滤：活性炭+麦饭石+矿化球+海绵+微孔过滤棉+KDF滤料",
     "活性炭+麦饭石+矿化球+海绵+过滤棉+KDF滤料"),
    ("可重复使用的粗过滤器", "滤网"),
])
def test_material_aliases(raw, mat):
    _lay, m, _c, t = _run(raw)
    assert m == mat
    assert t == "有过滤系统（材料已明确）"


def test_material_count_always_matches_tokens():
    """核心不变量: 种类数 == 材料按 '+' 拆开的个数(值域内)。"""
    rules_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(rules_p):
        pytest.skip("类目规则未部署")
    allowed = set(json.load(open(rules_p, encoding="utf-8"))["attributes"][0]["params"].get(
        "material_map", {}).values()) or None
    for raw in ["2层过滤（活性炭+海绵）", "5层过滤：活性炭+过滤棉", "滤芯",
                "碳过滤器", "可重复使用的粗过滤器", "6层过滤：活性炭+麦饭石+矿化球+海绵+微孔过滤棉+KDF滤料"]:
        _lay, m, c, _t = _run(raw)
        if c == "未明确":
            continue
        toks = [x for x in m.split("+") if x]
        assert c == len(toks), "%s: 种类数 %s != 材料个数 %d" % (raw, c, len(toks))


@pytest.mark.parametrize("raw,expect_type", [
    ("滤芯", "有滤芯（材料未明确）"),
    ("5层过滤：活性炭+过滤棉", "有过滤系统（材料已明确）"),
    ("4层过滤", "有过滤系统（材料未明确）"),
    ("无需过滤器", "无需过滤器"),
])
def test_type_consistent_with_material(raw, expect_type):
    """跨列自洽: 类型必须与材料是否明确一致(本轮 C4 审计 35 行冲突的根因)。"""
    _lay, _m, _c, t = _run(raw)
    assert t == expect_type


def test_no_filter_special_still_na():
    lay, m, c, t = _run("无需过滤器")
    assert (lay, m, c, t) == ("不适用", "不适用", "不适用", "无需过滤器")
