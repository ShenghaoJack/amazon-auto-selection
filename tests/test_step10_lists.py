# -*- coding: utf-8 -*-
"""单元测试: 步骤11/12 清单(过滤后口径, 2026-09-09 用户定稿)。

覆盖 core/steps/step10_variants._aggregate_families 与 _step11_12_inputs:
- parents = 原多变体父体(>=2 子)且至少一个子变体被保留 -> 步骤11 查流量;
- variants = 保留的子体 ∪ 保留的单变体产品自身(纯多变体父体自身 keep 也不进)
  -> 步骤12 查竞品。
"""
from __future__ import annotations

from steps.step10_variants import _aggregate_families, _step11_12_inputs


def _rec(asin, parent=None, variants=()):
    r = {"request_asin": asin, "ok": True, "title": f"t-{asin}",
         "parent_asin": parent or asin, "variant_asins": list(variants)}
    return r


def _kept(*asins):
    return [{"request_asin": a, "keep": True} for a in asins]


def test_aggregate_families_and_filtered_inputs():
    # P1: 多变体父体(C1,C2); C2 被剔, C1 保留 -> P1 仍有效
    # P2: 只有 1 个子变体 D1(非"多变体父体", 不进步骤11); D1 保留
    # P3: 多变体父体但子变体 E1 被剔, 自身即使保留也不进步骤11/12
    # S1: 单变体产品自身(无子变体) -> 保留后进步骤12
    records = [
        _rec("P1", variants=("C1", "C2")),
        _rec("C1", parent="P1"),
        _rec("C2", parent="P1"),
        _rec("P2", variants=("D1",)),
        _rec("D1", parent="P2"),
        _rec("P3", variants=("E1",)),
        _rec("E1", parent="P3"),
        _rec("S1"),
    ]
    fam = _aggregate_families(records, None)
    assert set(fam) == {"P1", "P2", "P3", "S1"}
    assert fam["P1"]["children"] == {"C1", "C2"}
    assert fam["P2"]["children"] == {"D1"}
    assert fam["P3"]["children"] == {"E1"}

    kept = _kept("C1", "D1", "S1", "P3")          # P3 自身保留但无保留子体
    parents, variants = _step11_12_inputs(fam, records, kept)
    assert parents == ["P1"]                        # 只有 P1 还有保留子体
    assert variants == ["C1", "D1", "S1"]           # 保留子体 + 保留单变体
    # P3(纯多变体父体)与 P1/P2 父体自身都不进步骤12


def test_child_self_merge_and_variant_texts_parentage():
    # 无父页记录但自身记录证明 parent=B09D5861BC 的变体并入家族 children
    records = [_rec("B09D5861BC", variants=("B089C6FAC0",)), _rec("B089C6FAC0", parent="B09D5861BC")]
    fam = _aggregate_families(records, None)
    assert fam["B09D5861BC"]["children"] == {"B089C6FAC0"}
    # 补抓记录补父子关系(父页未列全)
    fam2 = _aggregate_families([], [{"request_asin": "V1", "ok": True,
                                     "parent_asin": "PA", "variant_asins": []}])
    assert fam2["PA"]["children"] == {"V1"}
    # V1 保留 -> PA 只有 1 个孩子(非多变体), 不进步骤11; V1 进步骤12
    kept = _kept("V1")
    parents, variants = _step11_12_inputs(fam2, [], kept)
    assert parents == []
    assert variants == ["V1"]


def test_all_dropped_children_removes_parent_from_step11():
    records = [
        _rec("PA", variants=("C1", "C2", "C3")),
        _rec("C1", parent="PA"), _rec("C2", parent="PA"), _rec("C3", parent="PA"),
    ]
    fam = _aggregate_families(records, None)
    kept = _kept()                                   # 全被剔除
    parents, variants = _step11_12_inputs(fam, records, kept)
    assert parents == []
    assert variants == []
    kept = _kept("C2")                               # 仅 C2 保留
    parents, variants = _step11_12_inputs(fam, records, kept)
    assert parents == ["PA"]
    assert variants == ["C2"]
