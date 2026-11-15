# -*- coding: utf-8 -*-
"""VOC 评分测试: 阈值边界、两条公式、以及 skill 的安全等级硬性规则。"""
import pytest

from tools.voc.scoring import (breadth_level, conf_level, lvl5, mean_strength,
                               negative_priority_score, positive_driver_score,
                               positive_verdict, priority_of, safety_h)


@pytest.mark.parametrize("share,expect", [
    (0.20, 5), (0.1999, 4), (0.10, 4), (0.0999, 3), (0.05, 3), (0.0499, 2), (0.02, 2), (0.0199, 1),
])
def test_lvl5_boundaries(share, expect):
    assert lvl5(share) == expect


@pytest.mark.parametrize("n,expect", [(30, 5), (29, 4), (15, 4), (14, 3), (8, 3), (7, 2), (3, 2), (2, 1)])
def test_conf_level_boundaries(n, expect):
    assert conf_level(n) == expect


def test_breadth_level():
    assert breadth_level(10, 50) == 5
    assert breadth_level(5, 10) == 5          # ≥50% 样本
    assert breadth_level(1, 1) == 5           # 只分析 1 个父体时它占 100%
    assert breadth_level(6, 50) == 4
    assert breadth_level(3, 50) == 3
    assert breadth_level(2, 50) == 2
    assert breadth_level(1, 50) == 1


def test_positive_driver_formula():
    assert positive_driver_score(5, 5, 5, 5, 5) == 100.0
    assert positive_driver_score(1, 1, 1, 1, 1) == 20.0
    # 20×(0.30×3 + 0.20×4 + 0.20×2 + 0.20×5 + 0.10×4) = 20×3.5 = 70.0
    assert positive_driver_score(3, 4, 2, 5, 4) == 70.0


def test_negative_priority_formula():
    # 20×(0.25×4 + 0.30×3 + 0.25×5 + 0.10×3 + 0.10×5) = 20×(1.0+0.9+1.25+0.3+0.5) = 20×3.95 = 79.0
    assert negative_priority_score(4, 3, 5, 3, 5) == 79.0
    # 20×(0.25×1 + 0.30×1 + 0.25×1 + 0.10×3 + 0.10×1) = 20×(0.25+0.30+0.25+0.30+0.10) = 20×1.2 = 24.0
    # 注意 T 固定为 3(趋势数据不足) 也贡献 0.30
    assert negative_priority_score(1, 1, 1, 3, 1) == 24.0


def test_safety_h_levels():
    assert safety_h(["A"]) == 5
    assert safety_h(["b"]) == 5
    assert safety_h(["C"]) == 3
    assert safety_h(["D"]) == 1
    assert safety_h([]) == 1


def test_safety_floor_ab_cannot_rank_below_p1():
    """skill 硬性规则: 可信 A/B 级问题不得低于 P1。"""
    assert priority_of(55.0, ["A"]) == "P1"
    assert priority_of(30.0, ["B"]) == "P1"
    assert priority_of(55.0, []) == "P2"


def test_safety_grade_d_alone_cannot_create_p0_or_p1():
    """仅 D 级(传闻)不得单独产生 P0/P1。"""
    assert priority_of(85.0, ["D"]) == "P2"
    assert priority_of(70.0, ["D"]) == "P2"
    assert priority_of(85.0, ["A"]) == "P0"
    assert priority_of(85.0, ["C"]) == "P0"


def test_priority_thresholds():
    assert priority_of(80.0, []) == "P0"
    assert priority_of(79.9, []) == "P1"
    assert priority_of(65.0, []) == "P1"
    assert priority_of(64.9, []) == "P2"
    assert priority_of(50.0, []) == "P2"
    assert priority_of(49.9, []) == "P3"


def test_mean_strength_and_verdict():
    assert mean_strength(["repeat_purchase", "vague_praise"]) == 3
    assert mean_strength([]) == 1
    assert positive_verdict(80.0).startswith("核心差异点")
    assert positive_verdict(65.0).startswith("强价值驱动")
    assert positive_verdict(50.0).startswith("辅助价值")
    assert positive_verdict(49.9).startswith("新兴")
