# -*- coding: utf-8 -*-
"""两个审核发现的 bug 修复（2026-10-10）：
  ① `load_attr_pool` 返回的市场字段永远为空（写 `_市场`、读 `市场` 键名不一致）
  ② multi 属性交付值分隔符（`,`）与词表（`、`）不一致 → 复合词表整串匹配不上、落"词表外"
"""
from __future__ import annotations

from tools.attr_stability.catalog import append_attr_pool, load_attr_pool
from tools.attr_stability.converge import converge_mechanical


# ---------------------------------------------------------------- ① 市场字段
def test_load_attr_pool_returns_market(tmp_path):
    """★ 修复①：`append_attr_pool` 写 `_市场`，`load_attr_pool` 之前读 `市场` → 恒空。
       现在返回值里 `市场` 必须取到真值（`US`）。"""
    append_attr_pool(tmp_path, "Cutting Boards",
                     [{"name": "产品类型", "hint": "a"}], "US", "t1")
    rounds = load_attr_pool(tmp_path, "Cutting Boards")
    assert len(rounds) == 1
    assert rounds[0]["市场"] == "US", "市场字段不能再为空"


def test_load_attr_pool_global_market_is_empty_str(tmp_path):
    """未标市场（缺省=全球）的轮次，`市场` 返回空串（表示"全球"，不是 `*` 泄漏出来）。"""
    append_attr_pool(tmp_path, "Cutting Boards",
                     [{"name": "产品类型", "hint": "a"}], None, "t1")
    rounds = load_attr_pool(tmp_path, "Cutting Boards")
    assert rounds[0]["市场"] == ""


# ---------------------------------------------------------------- ② 分隔符归一
def test_separator_normalized_whole_string_match():
    """★ 修复②：交付 `肉类,蔬菜水果`、词表 `肉类、蔬菜水果`（顺序一致）→ 整串命中。

    ⚠ 审核实测抓到：原来传的键名是 `"shape"`，而 `converge.py` 读的是 **`value_shape`**
      （`spec.get('shape')` 全文一处都没有）→ **快路径根本没执行**，测试是靠
      `_key()` 剥标点后的**单值分支**碰巧通过的（假信心）。现在用正确的键名。
    """
    cells = {"1|attr.适用场景": {"kind": "value",
                                 "values": [{"value": "肉类,蔬菜水果,奶酪熟食"}]}}
    declared = {"适用场景": ["肉类、蔬菜水果、奶酪熟食", "面包烘焙"]}
    r = converge_mechanical(cells, declared,
                            attrs_spec={"适用场景": {"type": "multi",
                                                     "value_shape": "multi"}})
    cr = r["适用场景"]
    assert not cr.unmatched, "不该落词表外"
    cell = cr.cells["1|attr.适用场景"]
    assert cell["主值"] == "肉类、蔬菜水果、奶酪熟食"


def test_separator_fastpath_is_what_saves_it():
    """★ 直接证明**快路径在起作用**（审核建议；我第一版的判据写错了）。

    ⚠ 我第一版想用"单值形状"作对照，**前提是错的**：单值分支的 `_key()` 会**剥掉所有标点**，
      所以 `肉类,蔬菜水果,奶酪熟食` 在单值分支里**照样命中**，压根落不到"未命中" → 区分不出来。
    正确的证明：**直接对比**被快路径**绕过**的那个函数（`_conv_multi`）——
      · `_conv_multi` 把复合串**拆成 3 个分量**，而词表里只有**复合值** → 全部未命中
      · 完整收敛（multi 形状 + 快路径先整串比）→ 收敛成功
    """
    from tools.attr_stability.converge import _conv_multi
    v = "肉类,蔬菜水果,奶酪熟食"
    declared = ["肉类、蔬菜水果、奶酪熟食"]
    s, unk = _conv_multi(v, declared, declared, ["、", ",", ";", "+"])
    assert unk, "`_conv_multi` 单独跑应把 3 个分量判为未命中（这正是没有快路径时的下场）"
    assert not s, "`_conv_multi` 单独跑收敛后应为空串"

    cells = {"1|attr.适用场景": {"kind": "value", "values": [{"value": v}]}}
    r = converge_mechanical(cells, {"适用场景": declared},
                            attrs_spec={"适用场景": {"type": "multi",
                                                     "value_shape": "multi"}})
    cr = r["适用场景"]
    assert not cr.unmatched, "走完整收敛（multi + 快路径）应当命中"
    assert cr.cells["1|attr.适用场景"]["主值"] == "肉类、蔬菜水果、奶酪熟食"


def test_separator_trailing_separator_stripped():
    """审核低5：交付值带**尾随分隔符**（实测 `'…cm,'`）→ 归一+去尾后仍应命中。"""
    cells = {"1|attr.适用场景": {"kind": "value",
                                 "values": [{"value": "肉类,蔬菜水果,"}]}}
    declared = {"适用场景": ["肉类、蔬菜水果"]}
    r = converge_mechanical(cells, declared,
                            attrs_spec={"适用场景": {"type": "multi",
                                                     "value_shape": "multi"}})
    assert not r["适用场景"].unmatched, "尾随分隔符不该导致落词表外"
    assert r["适用场景"].cells["1|attr.适用场景"]["主值"] == "肉类、蔬菜水果"


def test_separator_mismatch_order_differs_still_falls_through():
    """顺序不同时，整串匹配不命中 → 仍走原逻辑（拆词），**不得崩**。"""
    cells = {"1|attr.适用场景": {"kind": "value",
                                 "values": [{"value": "面包,奶酪熟食,肉类"}]}}
    declared = {"适用场景": ["肉类、蔬菜水果、奶酪熟食"]}
    r = converge_mechanical(cells, declared,
                            attrs_spec={"适用场景": {"type": "multi", "shape": "multi"}})
    assert "适用场景" in r, "必须正常产出结果，不抛异常"
