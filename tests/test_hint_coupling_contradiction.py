# -*- coding: utf-8 -*-
"""验证 (a)：hint 同步时**消除"自相矛盾的断言"**（2026-10-09 用户口径）。

矛盾长什么样（实测原文，Cutting Boards 的 `产品类型` hint）
===============================================================================
    …典型取值 竹制砧板 / 实木砧板 / 不锈钢砧板 / …；
    与材质区分：本属性看用途形态，材质看主体材料

前半句的**样例名字里嵌着材质**，后半句却断言**与材质无关** —— 自相矛盾。
模型信了后半句 → 20 个纯钛砧板全填 `不锈钢砧板`，
而它自己写的 `依据` 里明明是 `Titanium`（**值与依据直接矛盾**）。

为什么必须**代码**消掉它（而不是继续在提示词里加固）
===============================================================================
两轮提示词加固都失败了（"典型取值不是闭集"、"值要与依据自洽"）——
因为那句断言写在**属性自己那一行**，比表头的通用规则更具体、更权威。
所以改在**产生矛盾的地方**：hint 同步时按**数据**判断耦合关系并换掉那句断言。

判据全部由数据算出（看样例嵌了什么），**不写死任何"某属性从某属性派生"的假设** ——
按材质命名、按功能命名都适用（用户明确要求两者都适配）。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.attr_stability.spec_sync import (
    find_coupled_attrs, strip_coupling_contradiction, sync_typical_values,
)

# 真实数据（Cutting Boards 冷启动时 spec 里的原文）
HINT_PT = ("从标题主体词判断商品形态：出现 cutting board 为砧板；"
           "典型取值 竹制砧板 / 实木砧板 / 不锈钢砧板 / 塑料软砧板垫；"
           "与材质区分：本属性看用途形态，材质看主体材料")
VALS = {
    "主体材质": ["竹制", "木制", "钛金属", "不锈钢", "塑料", "玻璃"],
    "产品类型": ["竹制砧板", "实木砧板", "不锈钢砧板", "塑料软砧板垫"],
    "形状": ["长方形", "圆形", "椭圆形"],
}


# --------------------------------------------------------------- ① 耦合检测
def test_detects_coupling_from_embedded_values():
    """`产品类型` 的样例嵌着 `主体材质` 的取值 → 判定为语义耦合。"""
    from tools.attr_stability.spec_sync import extract_typical
    c = find_coupled_attrs(extract_typical(HINT_PT), VALS, exclude="产品类型")
    assert "主体材质" in c, "样例里有 竹制/木制/不锈钢 → 应判定与主体材质耦合"
    assert c["主体材质"] >= 2
    assert "形状" not in c, "样例「竹制砧板」不含「长方形」等 → 不该判定与形状耦合"


def test_no_coupling_when_samples_are_independent():
    """反例：样例不带别的属性语义时，**不得**误判耦合。

    例：某属性的样例是「整只 / 切块 / 打散」—— 与材质、形状都无关。
    """
    c = find_coupled_attrs(["整只", "切块", "打散"], VALS, exclude="产品类型")
    assert c == {}, "不该凭空判定耦合（否则会把好 hint 改坏）"


def test_single_hit_is_not_enough():
    """只有 1 个样例嵌着 → 不算耦合（防单例噪音，`COUPLING_MIN=2`）。"""
    c = find_coupled_attrs(["竹制砧板", "圆形菜板"], VALS, exclude="产品类型")
    assert "主体材质" not in c


def test_short_values_do_not_trigger():
    """长度 <2 的受控值不参与匹配（防单字噪音）。"""
    c = find_coupled_attrs(["大号砧板", "小号砧板"], {"尺码": ["大", "小"]}, exclude="产品类型")
    assert c == {}


# --------------------------------------------------------------- ② 断言替换
def test_strips_contradicting_clause():
    """★ 核心：那句"与材质区分"被换掉，且**不丢信息**（换成建设性说法）。"""
    coupled = {"主体材质": 3}
    new, removed = strip_coupling_contradiction(HINT_PT, coupled)
    assert removed, "必须识别出那句矛盾断言"
    assert "与材质区分" not in new, "矛盾断言必须被换掉"
    assert "主体材质" in new and "自洽" in new, "要换成「与它自洽」的建设性说法"
    # ★ 最重要的：**「典型取值」那段一个字都不能动**
    assert "典型取值 竹制砧板 / 实木砧板 / 不锈钢砧板 / 塑料软砧板垫" in new
    # 前面那句正常判据也不能丢
    assert "从标题主体词判断商品形态" in new


def test_keeps_clause_that_merely_mentions_the_attr():
    """提到另一属性、但**没有下"无关"断言**的子句不许删。

    （否则会把「材质与木材种类区分」这类**正确**说明一并删掉。）
    """
    h = "看 wood / bamboo；木材种类仅在木质时细到树种"
    new, removed = strip_coupling_contradiction(h, {"主体材质": 2})
    assert removed == []
    assert new == h


def test_no_coupling_means_no_change():
    new, removed = strip_coupling_contradiction(HINT_PT, {})
    assert removed == []
    assert new == HINT_PT


# --------------------------------------------------------------- ③ 端到端
def test_sync_applies_both_rewrites(tmp_path):
    """端到端：`sync_typical_values` 既同步「典型取值」，又消除矛盾断言。"""
    spec = {"category": "t", "attributes": [
        {"name": "产品类型", "hint": HINT_PT},
        {"name": "主体材质", "hint": "找 wood/bamboo/plastic；典型取值 竹制 / 木制"},
    ]}
    p = tmp_path / "t-attribute-spec.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    n = sync_typical_values(p, VALS)
    assert n >= 1
    after = json.loads(p.read_text(encoding="utf-8"))
    pt = next(a for a in after["attributes"] if a["name"] == "产品类型")
    assert "与材质区分" not in pt["hint"], "矛盾断言应已消除"
    assert "典型取值 竹制砧板 / 实木砧板 / 不锈钢砧板 / 塑料软砧板垫" in pt["hint"], \
        "典型取值应按受控值同步"


def test_strip_is_idempotent():
    """幂等：重复跑不会越改越乱。"""
    coupled = {"主体材质": 3}
    once, r1 = strip_coupling_contradiction(HINT_PT, coupled)
    twice, r2 = strip_coupling_contradiction(once, coupled)
    assert r2 == [], "第二次不该再改动"
    assert twice == once
