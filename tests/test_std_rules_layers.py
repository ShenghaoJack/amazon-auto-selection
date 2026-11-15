# 单元测试: 过滤层数「多层」→ 图片视觉查出实际层数
#
# 背景(2026-09-21, 用户要求「过滤层数=多层 的用图片视觉查看到底多少层」):
#   12 行原值仅「多层过滤」，文本无任何层数线索(阿拉伯/中文/英文数词都没有)。
#   视觉(大图 + A+ 长图)查到 8 行: 结构图 6 行(剖面/爆炸图数层) + 文字 2 行(Quadruple Filtration)。
#   4 行图上确实没有分层证据 → 保持未明确。
#   一致性: 两轮多图判定完全一致(B020C953E4=5 经 R2/R3 两轮确认)。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

LAY = "过滤层数（营销宣称）_标准"


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cat():
    return os.path.join(_root(), "data", "Cat Fountains-US-202608")


def test_layers_vision_file_shape():
    p = os.path.join(_cat(), "cat_fountains.vision_layers.json")
    if not os.path.exists(p):
        pytest.skip("视觉文件未部署")
    vis = json.load(open(p, encoding="utf-8"))
    assert len(vis) == 8
    for asin, e in vis.items():
        # 只写层数子列, 不越界写其它子列
        assert set(e) == {LAY, "过滤类型_依据"}
        assert isinstance(e[LAY], int) and 1 <= e[LAY] <= 9
        assert "图片视觉" in e["过滤类型_依据"]


def test_layer_counts_are_stable_known_values():
    p = os.path.join(_cat(), "cat_fountains.vision_layers.json")
    if not os.path.exists(p):
        pytest.skip("视觉文件未部署")
    vis = json.load(open(p, encoding="utf-8"))
    expect = {"B0B13483EF": 4, "B076E78938": 3, "B0E18E6718": 4, "B09D4A7F1A": 4,
              "B020C953E4": 5, "B0F92000B7": 3, "B0A3DD0967": 4, "B089C180B6": 4}
    assert {k: e[LAY] for k, e in vis.items()} == expect


def test_remaining_multi_rows_kept_unknown():
    """4 行图上无分层证据, 不得硬猜层数。"""
    p = os.path.join(_cat(), "cat_fountains.vision_layers.json")
    if not os.path.exists(p):
        pytest.skip("视觉文件未部署")
    vis = set(json.load(open(p, encoding="utf-8")))
    remaining = {"B0EDF096CD", "B080EB8541", "B0EBE87423", "B02A175724"}
    assert not (vis & remaining), "无证据的行不应出现在视觉文件里"


def test_rule_keeps_multi_and_unknown():
    """规则本身: 原值「多层过滤」仍落 多层; 视觉覆盖在驱动侧才改成具体数字。"""
    p = os.path.join(_cat(), "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署")
    rules = json.load(open(p, encoding="utf-8"))
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    assert apply_rule(a, "多层过滤", {})[0][LAY] == "多层"
    assert apply_rule(a, "未明确", {})[0][LAY] == "未明确"
