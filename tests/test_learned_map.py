# -*- coding: utf-8 -*-
"""学习映射（Learned Value Map）—— 枚举表的**正确来源**。

用户口径（2026-10-08）：
  枚举映射表**可以存在**，但必须由标准化**自动生成**、供**下次同类目运行**复用，
  而不是人手写。

本文件锁住三件事：
  ① 只沉淀「目标值在当前受控值词表内」的映射（否则下次照抄即产出非法值 ——
     旧 `<cat>.dict.json` 的 `mapping` 正是因此被废弃）
  ② 落盘/读回自洽（原子写、损坏不炸流水线）
  ③ 复用是**确定性**的：只需（机械收敛 + 学习映射），不需要 LLM
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.attr_stability.learned_map import (
    MAP_SUFFIX, apply_learned, learned_path, load_learned, merge_learned, save_learned,
)


# --------------------------------------------------------------- 沉淀规则
def test_merge_only_keeps_targets_in_vocab():
    """★ 目标不在词表内的不沉淀 —— 这是旧 dict.json 被废弃的根因。"""
    old = {}
    new = {"主体材质": {"竹木": "竹制", "柚木": "木制", "怪东西": "木制+玉石"}}
    allowed = {"主体材质": ["竹制", "木制", "塑料"]}
    got = merge_learned(old, new, allowed=allowed)
    assert got == {"主体材质": {"竹木": "竹制", "柚木": "木制"}}
    assert "怪东西" not in got["主体材质"]


def test_merge_skips_identity_and_empty():
    got = merge_learned({}, {"A": {"x": "x", "": "y", "z": "  "}})
    assert got == {}


def test_merge_accumulates_across_runs():
    a = merge_learned({}, {"M": {"竹木": "竹制"}})
    b = merge_learned(a, {"M": {"柚木": "木制"}})
    assert b == {"M": {"竹木": "竹制", "柚木": "木制"}}
    # 新值覆盖旧映射
    c = merge_learned(b, {"M": {"柚木": "塑料"}}, allowed={"M": ["竹制", "木制", "塑料"]})
    assert c["M"]["柚木"] == "塑料"


def test_merge_drops_attr_when_all_rejected():
    got = merge_learned({}, {"M": {"a": "不在词表"}}, allowed={"M": ["竹制"]})
    assert got == {}


# --------------------------------------------------------------- 落盘/读回
def test_save_and_load_roundtrip(tmp_path):
    d = str(tmp_path)
    p = save_learned(d, "t", {"M": {"竹木": "竹制"}}, cat="T")
    assert p.endswith(MAP_SUFFIX) and Path(p).is_file()
    o = json.loads(Path(p).read_text(encoding="utf-8"))
    assert o["cat"] == "T" and o["属性"] == {"M": {"竹木": "竹制"}}
    assert load_learned(d, "t") == {"M": {"竹木": "竹制"}}
    # 真正的写入是原子的：不留 .tmp
    assert not Path(p + ".tmp").exists()


def test_load_missing_or_broken_is_empty(tmp_path):
    assert load_learned(str(tmp_path), "nope") == {}
    p = Path(learned_path(str(tmp_path), "bad"))
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_learned(str(tmp_path), "bad") == {}      # 损坏不炸流水线
    p.write_text(json.dumps({"属性": "不是对象"}), encoding="utf-8")
    assert load_learned(str(tmp_path), "bad") == {}


# --------------------------------------------------------------- 复用
class _Res:
    def __init__(self, cells):
        self.cells = cells


def _cell(v):
    return {"mode": v, "kind": "value", "values": [{"value": v, "by": ["a1"]}]}


def test_apply_learned_fills_from_cells():
    cells = {"2|attr.主体材质": _cell("竹木"), "3|attr.主体材质": _cell("柚木"),
             "4|attr.主体材质": _cell("陌生素材")}
    learned = {"主体材质": {"竹木": "竹制", "柚木": "木制"}}
    std, n = apply_learned({}, learned, cells=cells)
    assert n == 2
    assert std[(2, "主体材质")] == "竹制" and std[(3, "主体材质")] == "木制"
    assert (4, "主体材质") not in std            # 没见过的值不硬套，留给 LLM


def test_apply_learned_ignores_stale_target():
    """学到的目标已被词表淘汰时不再使用（否则等于把废弃值又请回来）。"""
    cells = {"2|attr.M": _cell("x")}
    std, n = apply_learned({}, {"M": {"x": "旧值"}}, cells=cells, allowed={"M": ["新值"]})
    assert n == 0 and not std


def test_apply_learned_zero_llm_path():
    """★ 复用是确定性的：仅靠 cells + 学习映射即可复现，无需 LLM。"""
    cells = {"2|attr.M": _cell("竹木"), "3|attr.M": _cell("柚木")}
    learned = {"M": {"竹木": "竹制", "柚木": "木制"}}
    std, _ = apply_learned({}, learned, cells=cells, allowed={"M": ["竹制", "木制"]})
    assert {v for v in std.values()} == {"竹制", "木制"}
