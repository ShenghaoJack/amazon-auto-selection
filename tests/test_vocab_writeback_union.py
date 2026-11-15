# -*- coding: utf-8 -*-
"""已有类目的词表更新必须**写得回去**（2026-10-10 审核修复）。

背景（审核实测，不跑 LLM 就能复现）
===============================================================================
`step17._write_vocab_to_rules` 的两道闸门都只放行"**原本受控值为空**"的属性：

    :649  if name in empty and vals:            # 只采纳"受控值为空"的属性
    :1277 if not params.get("values"):          # 只在"值为空"时才写

后果：**已有类目每轮为新值付 LLM 费 → 一个词都写不回去** → 用户只能手工补词表。
而同一函数开头的注释（L510-516）**明确宣称"已有类目也要跑聚类"** —— 注释与实现矛盾。
**冷启动测不出**（那时全部属性都为空、全被采纳），所以多轮验证都漏过了它。

修复
===============================================================================
  · 采纳闸门去掉 `name in empty`（采纳全部有值的属性）
  · 写回改**并集**（只增不减，与 `_append_vocab_values` 同语义）：
      已有值**保留不动**（人工确认过的词表不会被 LLM 悄悄改掉）
      新值**追加**（新原值/新属性才收敛得进来）
    —— 合并/清理交给**停② 的人工确认**，不做自动删除。
"""
from __future__ import annotations

import json
from pathlib import Path

from steps.step17_attr_annotation import Step17AttrAnnotation


def _step() -> Step17AttrAnnotation:
    st = Step17AttrAnnotation()
    st._cat = "t"
    st.log = lambda *a, **k: None                              # 静音
    return st


def _run(tmp_path: Path, adopted: dict) -> dict:
    p = tmp_path / "t.rules.json"
    p.write_text(json.dumps({"attributes": [
        {"name": "有词表的", "params": {"values": ["A", "B"]}},
        {"name": "空词表的", "params": {}},
        {"name": "旧格式", "values": ["X"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    _step()._write_vocab_to_rules(str(tmp_path), "t", adopted, None)
    o = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for a in o["attributes"]:
        out[a["name"]] = ((a.get("params") or {}).get("values") if "params" in a
                          else a.get("values"))
    return out


def test_new_values_are_appended_for_existing_vocab(tmp_path):
    """★★ **核心修复**：已有词表的属性，新增值必须写得进去（原来被整条丢弃）。"""
    got = _run(tmp_path, {"有词表的": ["A", "B", "C"]})
    assert got["有词表的"] == ["A", "B", "C"], "新增的 'C' 不能被丢"


def test_existing_values_are_never_dropped(tmp_path):
    """**只增不减** —— 人工确认过的值不会被 LLM 悄悄改掉。"""
    got = _run(tmp_path, {"有词表的": ["B"]})                  # LLM 只回了一个子集
    assert got["有词表的"] == ["A", "B"], "'A' 必须在（并集，不覆盖）"


def test_empty_vocab_still_adopted(tmp_path):
    """原本为空的属性照样采纳（这是原行为，不能被修坏）。"""
    got = _run(tmp_path, {"空词表的": ["X"]})
    assert got["空词表的"] == ["X"]


def test_old_top_level_format_also_union(tmp_path):
    """老格式（顶层 `values`）走同一条并集逻辑。"""
    got = _run(tmp_path, {"旧格式": ["Y"]})
    assert got["旧格式"] == ["X", "Y"]


def test_unknown_attribute_is_ignored(tmp_path):
    """rules 里没有的属性名不该凭空造出来。"""
    got = _run(tmp_path, {"不存在的": ["Z"]})
    assert "不存在的" not in got
