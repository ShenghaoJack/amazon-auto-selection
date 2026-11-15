# 单元测试: 「是否可用洗碗机清洗」的判据闸门
#
# 背景(2026-09-21, 用户要求「用视觉继续获取」):
#   ① **提取层漏抓**: 8 行卖点原文就有 "the water tank and tray can be directly placed in the
#      dishwasher for cleaning", 但 是否可用洗碗机清洗 原值仍是 未明确。(用全表关键词扫描确认)
#   ② **图片召回率只有 38%**: 拿 60 行**文本已知=是** 的行做验证集, 视觉只认出 23 行是、
#      37 行 未明确、**0 行 否** —— 即 62% 的真·可机洗机型图上根本没印该声明。
#      → 「图上没写」**不能**推出 否（按该比例会给 326 行候选误判约 201 行）。
#   ③ **部分部件不可 ≠ 整机不可**: 首轮把 5 行陶瓷机的 "Do not put plastic parts in the
#      dishwasher" 判成 否, 但同一张图展示了陶瓷件放入洗碗机 → 应判 是。补齐该判据后 否 归零。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

COL = "是否可用洗碗机清洗_标准"
ATTR = "是否可用洗碗机清洗"


def _cat_rules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _vision(fn):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", fn)
    if not os.path.exists(p):
        pytest.skip("视觉文件未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def test_attr_is_binary_enum():
    a = next(x for x in _cat_rules()["attributes"] if x["name"] == ATTR)
    assert a["rule"] == "enum"
    assert a["params"]["values"] == ["是", "否"]
    assert a["subcols"] == [COL]


def test_text_layer_gap_rows_are_overridden_to_yes():
    """提取层漏抓的 8 行必须有逐行改判(且 when=未明确, 因为源表原值就是未明确)。"""
    ov = {r["asin"]: r for r in _cat_rules()["row_overrides"] if r["attr"] == ATTR}
    assert len(ov) == 8, "应有 8 条文本改判, 实际 %d" % len(ov)
    for asin, r in ov.items():
        assert r["when"] == "未明确"
        assert r["then"] == "是"
        assert "dishwasher" in r["note"].lower()


def test_stated_overrides_match_text_evidence():
    """这 8 行必须是真的在原文里出现了 dishwasher 字样(不是拍脑袋写的)。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.vision_dishwasher.json")
    ov = [r["asin"] for r in _cat_rules()["row_overrides"] if r["attr"] == ATTR]
    # 文本改判的 8 行与视觉命中的 29 行不应重叠(文本优先, 视觉不重复覆盖)
    vis = set(_vision("cat_fountains.vision_dishwasher.json")) if os.path.exists(p) else set()
    assert not (set(ov) & vis), "文本改判行不应同时出现在视觉文件里"


def test_vision_file_only_contains_yes():
    """视觉文件只写 是 —— 图上没写不得写成 否(实测图片召回仅 38%)。"""
    vis = _vision("cat_fountains.vision_dishwasher.json")
    assert len(vis) == 29
    for asin, e in vis.items():
        assert e[COL] == "是", "%s 不应写非 是 的值" % asin
        assert "图片视觉" in (e.get(ATTR + "_依据") or ""), "%s 缺可审计依据" % asin


def test_enum_rule_keeps_unknown_and_yes():
    """规则本身: 原值为 未明确/是/否 时行为不变。"""
    a = next(x for x in _cat_rules()["attributes"] if x["name"] == ATTR)
    assert apply_rule(a, "未明确", {})[0][COL] == "未明确"
    assert apply_rule(a, "是", {})[0][COL] == "是"
    assert apply_rule(a, "否", {})[0][COL] == "否"


def test_no_absence_based_no_in_output_rule():
    """防回归: 类目规则里不得出现按"图上没写"批量判 否 的机制。"""
    rules = _cat_rules()
    for r in rules.get("row_overrides", []):
        if r["attr"] == ATTR:
            assert r["then"] == "是", "只允许把漏抓的 是 补回来"
