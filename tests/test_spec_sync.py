# -*- coding: utf-8 -*-
"""spec.hint 的「典型取值」与 rules.values 的口径同步（通用自检 ③ 的修法）。

不变量：**hint 里列出的典型取值必须 ⊆ rules.values**。
例子越界 = 提示词在诱导模型输出闸门不接受的值 ——
实测 `是否含负离子` rules 只有 1 个值、hint 写了 5 个 → 58 格全掉。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.attr_stability.spec_sync import (
    check_caliber, extract_typical, rewrite_hint, sync_typical_values,
)


def test_extract_typical():
    h = "标题/五点找 juice groove；典型取值 带汁槽 / 无汁槽；与防滑区分：…"
    assert extract_typical(h) == ["带汁槽", "无汁槽"]
    assert extract_typical("没有这一段") == []


def test_check_caliber_flags_out_of_domain():
    h = "典型取值 含负离子 / 带负离子 / 不含负离子 / 不带负离子 / 未明确"
    assert check_caliber(h, ["含负离子"]) == ["带负离子", "不含负离子", "不带负离子", "未明确"]
    assert check_caliber(h, ["含负离子", "带负离子", "不含负离子", "不带负离子", "未明确"]) == []


def test_check_caliber_allows_subset():
    """例子可以是法条的**子集**（只列常见几个），不算错。"""
    h = "典型取值 竹制 / 木制"
    assert check_caliber(h, ["竹制", "木制", "塑料", "金属"]) == []


def test_check_caliber_no_vocab_is_not_an_error():
    assert check_caliber("典型取值 A / B", []) == []


def test_rewrite_hint_rewrites_only_that_segment():
    h = "标题找 wood；典型取值 竹木 / 柚木；与材质区分：本属性看材料"
    out = rewrite_hint(h, ["竹制", "木制"])
    assert "典型取值 竹制 / 木制" in out
    assert "标题找 wood" in out and "与材质区分" in out      # 其余文字不动
    assert check_caliber(out, ["竹制", "木制"]) == []


def test_rewrite_hint_caps_long_lists():
    vals = ["v%d" % i for i in range(30)]
    out = rewrite_hint("典型取值 v0 / v1", vals)
    assert "等" in out and "v29" not in out


def test_rewrite_hint_noop_without_segment():
    h = "本属性没有取值枚举那一段"
    assert rewrite_hint(h, ["A"]) == h


def test_rewrite_hint_guards_against_prose():
    """★ 散文里偶然出现「典型取值」四个字时不能被误改（单测抓到的真实缺陷）。"""
    h = ("本属性的判据见下表，历史上曾经把典型取值那一段删掉过，"
         "后来又加回来了，因此这里只是一段说明文字，并不是取值枚举。")
    assert rewrite_hint(h, ["A", "B"]) == h


def test_sync_typical_values_roundtrip(tmp_path):
    sp = Path(tmp_path) / "t-attribute-spec.json"
    sp.write_text(json.dumps({"attributes": [
        {"name": "主体材质", "hint": "找 wood/bamboo；典型取值 竹木 / 柚木；与产品类型区分"},
        {"name": "形状", "hint": "看外形；典型取值 长方形 / 方形"},
    ]}, ensure_ascii=False), encoding="utf-8")
    n = sync_typical_values(sp, {"主体材质": ["竹制", "木制"], "形状": ["长方形", "圆形"]})
    assert n == 2
    spec = json.loads(sp.read_text(encoding="utf-8"))
    by = {a["name"]: a["hint"] for a in spec["attributes"]}
    assert check_caliber(by["主体材质"], ["竹制", "木制"]) == []
    assert "典型取值 竹制 / 木制" in by["主体材质"]
    assert "找 wood/bamboo" in by["主体材质"]                 # 判据没被动
    assert check_caliber(by["形状"], ["长方形", "圆形"]) == []


def test_sync_missing_file_is_noop(tmp_path):
    assert sync_typical_values(str(Path(tmp_path) / "nope.json"), {"A": ["x"]}) == 0


def test_extract_typical_does_not_split_on_dunhao():
    """★ 受控值本身可能含 、（风嘴、导梳 是一个组合值）——
    按 、 拆会把一个合法值拆成两个不存在的值，报假越界（实测 Dog Dryer 3 个属性）。"""
    from tools.attr_stability.spec_sync import extract_typical
    h = "典型取值 风嘴 / 风嘴、导梳 / 支架 / 风嘴、支架、壁挂架、导梳"
    assert extract_typical(h) == ["风嘴", "风嘴、导梳", "支架", "风嘴、支架、壁挂架、导梳"]
    vals = ["风嘴", "风嘴、导梳", "支架", "风嘴、支架、壁挂架、导梳"]
    assert check_caliber(h, vals) == []
