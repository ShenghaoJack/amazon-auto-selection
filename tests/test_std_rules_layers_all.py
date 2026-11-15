# 单元测试: 过滤层数全量补值（vision_layers2 69 行 + 4 文本改判 + dict-then 合并）
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

LAY = "过滤层数（营销宣称）_标准"


def _cat():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "Cat Fountains-US-202608")


def test_layers2_vision_file_shape():
    p = os.path.join(_cat(), "cat_fountains.vision_layers2.json")
    if not os.path.exists(p):
        pytest.skip("视觉文件未部署")
    vis = json.load(open(p, encoding="utf-8"))
    assert len(vis) == 69
    for asin, e in vis.items():
        assert set(e) == {LAY, "过滤类型_依据"}
        assert isinstance(e[LAY], int) and 1 <= e[LAY] <= 9


def test_layers2_no_overlap_with_layers1():
    """两轮稳定结论不应与「多层」任务(8 行)重复。"""
    p2 = os.path.join(_cat(), "cat_fountains.vision_layers2.json")
    p1 = os.path.join(_cat(), "cat_fountains.vision_layers.json")
    if not (os.path.exists(p2) and os.path.exists(p1)):
        pytest.skip("视觉文件未部署")
    v2 = set(json.load(open(p2, encoding="utf-8")))
    v1 = set(json.load(open(p1, encoding="utf-8")))
    assert not (v1 & v2), "两层视觉文件不应有重叠 ASIN"


def test_four_text_overrides_present():
    p = os.path.join(_cat(), "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署")
    rules = json.load(open(p, encoding="utf-8"))
    ov = {r["asin"]: r for r in rules["row_overrides"]
          if r["attr"] == "过滤类型" and isinstance(r.get("then"), dict)
          and LAY in r["then"]}
    expect = {"B00A114A55": 5, "B04AE33E7D": 4, "B089D476B2": 4, "B0E70A25A0": 4}
    assert {k: ov[k]["then"][LAY] for k in expect if k in ov} == expect
    # 这 4 条只补层数（不得误写材料/种类数/类型）。
    # 注意(2026-09-22): `过滤类型` 下另有 27 条「跨列一致性」dict 改判（19 条写 LAY=不适用，
    # 8 条写 材料/种类数/类型），它们不由本测试管，见 test_std_rules_filter_consistency.py。
    for k in expect:
        assert set(ov[k]["then"]) == {LAY}, k


def test_partial_dict_then_keeps_other_subcols():
    """⚠ 回归: dict then 是**合并**语义——只写层数时, 规则算出的材料/种类数/类型必须保留。

    这里直接验证规则侧: 对 B04AE33E7D 的原值「活性炭+离子交换树脂+无纺布+海绵过滤」,
    规则应产出 材料=无纺布+海绵+活性炭+离子交换树脂、种类数=4、类型=材料已明确;
    层数改判只叠加 LAY=4, 不得清空其余三个子列。
    """
    p = os.path.join(_cat(), "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署")
    rules = json.load(open(p, encoding="utf-8"))
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    cv, _, _, _ = apply_rule(a, "活性炭+离子交换树脂+无纺布+海绵过滤", {})
    assert cv["过滤材料_标准"] == "活性炭+离子交换树脂+无纺布+海绵"  # 规则侧=源顺序(驱动侧才做 value_order 归一)
    assert cv["过滤材料种类数_标准"] == 4
    assert cv["过滤类型_标准"] == "有过滤系统（材料已明确）"
    # 模拟驱动侧的合并语义: 只叠层数
    merged = dict(cv)
    merged.update({LAY: 4})
    assert merged["过滤材料_标准"] == "活性炭+离子交换树脂+无纺布+海绵"  # 材料被保留(不因改判被清空)
    assert merged[LAY] == 4
