# -*- coding: utf-8 -*-
"""跨属性约束的**方向选择**：优先「右必须含」，少用「右不得含」。

背景（2026-10-09，核实行 159 得出）
===============================================================================
商品 B09D30DFBB 标题 `Titanium Cutting Board ... Pure Titanium & Wood Fiber`，
详细参数 `Material: Titanium, Wood Fiber` —— 是**纯钛 + 木纤维**双面砧板。

标注结果：`主体材质=钛金属+木制` ✅ 正确，`产品类型=不锈钢砧板` ❌ **错**。
模型在 `冲突` 里**3 次运行全都记录了**这个矛盾，仍然硬填了 `不锈钢砧板` ——
根因是那一轮 `产品类型` 词表里**没有「钛金属砧板」**，它只能挑最接近的金属类。

步骤16 为这一对生成的约束是::

    产品类型 含 [不锈钢砧板] → 主体材质 不得含 [竹制, 木制]

**方向反了**：真正的问题是「**缺**不锈钢」，不是「**有**木头」。
后果（实测）：合法的「不锈钢+木制」双面砧板会被**误报**。

正确方向（`check_row` 本来就支持「右侧必须成立」）::

    产品类型 含 [不锈钢砧板] → 主体材质 含 [不锈钢]

本文件把这个语义选择钉死，防止再退化成「不得含」。
"""
from __future__ import annotations

from tools.attr_stability.cross_checks import check_row

# 修正后的约束：右侧「必须含」
GOOD = {"互斥": [{"说明": "不锈钢砧板必须含不锈钢",
                  "左": {"属性": "产品类型", "含": ["不锈钢砧板"]},
                  "右": {"属性": "主体材质", "含": ["不锈钢"]}}]}

# 旧写法：右侧「不得含」（方向反、会误伤组合值）
BAD = {"互斥": [{"说明": "不锈钢砧板不可能是竹制或木制材质",
                 "左": {"属性": "产品类型", "含": ["不锈钢砧板"]},
                 "右": {"属性": "主体材质", "不得含": ["竹制", "木制"]}}]}


def test_good_direction_catches_the_real_problem():
    """行 159 的真问题：类型说不锈钢，材质里根本没有不锈钢。"""
    assert check_row({"产品类型": "不锈钢砧板", "主体材质": "钛金属+木制"}, GOOD)


def test_good_direction_does_not_false_positive_on_combos():
    """★ 合法组合值不许误报 —— 这正是旧写法的问题。"""
    for m in ("不锈钢", "不锈钢+木制", "不锈钢+塑料", "不锈钢+钛金属"):
        assert check_row({"产品类型": "不锈钢砧板", "主体材质": m}, GOOD) == [], m


def test_bad_direction_false_positives_on_combo():
    """锁住旧写法的缺陷（作为反面证据，防止有人改回去）。"""
    assert check_row({"产品类型": "不锈钢砧板", "主体材质": "不锈钢+木制"}, BAD), \
        "旧写法在合法的「不锈钢+木制」上会误报 —— 这就是要改方向的理由"


def test_both_directions_catch_mutual_exclusion():
    """真正的互斥（竹 vs 实木）两种方向都抓得到 —— 所以改了不丢能力。"""
    mutual = {"互斥": [{"说明": "竹制砧板必须含竹",
                        "左": {"属性": "产品类型", "含": ["竹制砧板"]},
                        "右": {"属性": "主体材质", "含": ["竹制"]}}]}
    assert check_row({"产品类型": "竹制砧板", "主体材质": "木制"}, mutual)
    old = {"互斥": [{"说明": "竹制砧板不可能是木制",
                     "左": {"属性": "产品类型", "含": ["竹制砧板"]},
                     "右": {"属性": "主体材质", "不得含": ["木制"]}}]}
    assert check_row({"产品类型": "竹制砧板", "主体材质": "木制"}, old)


def test_prompt_prefers_must_contain():
    """步骤16 的提示词里必须写明「优先用右必须含」这条方向指引。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "steps"
           / "step16_attribute_annotation.py").read_text(encoding="utf-8-sig")
    assert "优先用「右必须含」" in src, "提示词缺「方向选择」指引"
    assert "主体材质 含 [不锈钢]" in src, "提示词缺正向示例"


def test_step17_prompt_forbids_force_fitting():
    """步骤17 的归类提示词必须**禁止硬塞**（行 159 的根因：无类可归就硬填）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "steps"
           / "step17_attr_annotation.py").read_text(encoding="utf-8-sig")
    assert "不许硬塞" in src
    assert "新增: 钛金属砧板" in src, "缺行 159 的真实教训"
