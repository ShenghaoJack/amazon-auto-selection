# -*- coding: utf-8 -*-
"""计数值的「单品默认值」不算异常（用户口径 2026-10-09）。

背景（100 行实测）
===============================================================================
`数值异常校验` 报 16 处，**15 处是 `套装数量=1`** —— 而单品砧板本来就 1 件，
填 1 是**合理的默认推断**，不是"上游误判"。这些噪音会把真异常淹掉
（同一批里 `套装数量=10` 而原文只写了 3/6/8/12 —— **那才是真误读**）。

判据
===============================================================================
**无物理量**（计数值，单位是"件/个"这种）+ **值为 1** → 放过。
· 只放过 1：`=2`/`=10` 仍报（它们不是"单品默认"，必须有原文依据）。
· 有物理量的属性不受影响：`容量=1`（1 ml?）仍要报 —— 它没有"默认 1"的语义。
"""
from __future__ import annotations

from tools.attr_stability.num_check import check_no_evidence

ROWS = {
    1: {"标题": "Teak Cutting Board 24 inch", "五点描述": "18 x 12 x 1.2 in"},
    2: {"标题": "Cutting Board Set of 12", "五点描述": "3 pack 6 pack 8 pack 12"},
}


def test_count_value_one_is_not_flagged():
    """计数值 = 1 → 单品默认值，放过。"""
    assert check_no_evidence("套装数量", {1: 1}, ROWS, quantity=None) == []


def test_count_value_two_still_flagged():
    """计数值 = 2 → 不是默认值，必须报（原文没依据）。"""
    r = check_no_evidence("套装数量", {1: 2}, ROWS, quantity=None)
    assert len(r) == 1


def test_count_value_ten_still_flagged():
    """★ 真异常不能被放过：`套装数量=10` 而原文只有 3/6/8/12 → 报。"""
    r = check_no_evidence("套装数量", {1: 10}, ROWS, quantity=None)
    assert len(r) == 1
    assert r[0]["值"] == 10


def test_physical_quantity_one_is_still_flagged():
    """有物理量时不受豁免：`容量=1` 没有"默认 1"的语义，仍要报。"""
    r = check_no_evidence("容量", {1: 1}, ROWS, quantity="volume")
    assert len(r) == 1
