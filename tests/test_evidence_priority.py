# -*- coding: utf-8 -*-
"""来源优先级：**三处必须一致、且无例外**（2026-10-08 用户口径）。

用户明确规定：`商品标题 > 五点描述 > 详细参数 > 图片OCR > 图片视觉 > SKU`

这个顺序原先在**三处各写了一份**：
  ① `tools/attr_stability/gapfill.py` 的 `SOURCE_PRIORITY`（追问/视觉提示词用）
  ② `prompts/23_llm_only_pipeline.md` §1b.1（主标注提示词用）
  ③ `data/<cat>/rules.json` 的 `evidence_priority`（提示词 §3.6 让模型去读的键）

问题在于 ②③ 长期不一致：
  · ② 里有一条例外，把「材质牌号 / 商品尺寸与厚度」提到 `详细参数` 优先；
  · ③ **根本不存在**，而提示词却写「取 rules.json 里该属性的 evidence_priority 列表」。
实测后果：模型对**同一类型**的冲突执行不一致 —— 它自己记的 `冲突` 里
"详细参数1mm，五点0.4\"约10mm，取五点" 与 "参数标1.6 inch，五点标6mm，取参数" 并存。

本文件把"三处一致 + 无例外"钉死，防止再次漂移。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAT = "Cutting Boards-US-202608"
EXPECTED = ["标题", "五点描述", "详细参数", "图片OCR", "图片视觉", "SKU"]
CHAIN = " > ".join(EXPECTED)

PROMPT = ROOT / "prompts" / "23_llm_only_pipeline.md"


def test_gapfill_source_priority():
    from tools.attr_stability.gapfill import SOURCE_PRIORITY
    assert SOURCE_PRIORITY == EXPECTED


def test_rules_json_evidence_priority():
    """③ 必须存在且与代码一致 —— 它此前**根本不存在**，而提示词却在引用它。"""
    r = json.loads((ROOT / "data" / CAT / "cutting_boards.rules.json")
                   .read_text(encoding="utf-8-sig"))
    assert r.get("evidence_priority") == EXPECTED


def test_prompt_declares_same_chain():
    txt = PROMPT.read_text(encoding="utf-8")
    assert CHAIN in txt, "提示词 §1b.1 必须写明同一条顺序"


def test_no_priority_exception_in_prompt():
    """例外必须清除：它把 `详细参数` 提到 `五点描述` 前面，与固定顺序冲突。"""
    txt = PROMPT.read_text(encoding="utf-8")
    assert "例外（这几类以" not in txt
    assert "全类目统一，没有例外" in txt
    # 绝不能出现「详细参数优先于五点」这类措辞
    assert "商品尺寸 / 厚度" not in txt
    assert "材质牌号 / 不锈钢等级 / 具体树种" not in txt


def test_no_priority_exception_in_gapfill():
    src = (ROOT / "tools" / "attr_stability" / "gapfill.py").read_text(encoding="utf-8")
    assert "例外（这几类以" not in src
    assert "全类目统一，没有例外" in src


def test_gapfill_priority_block_contains_chain():
    from tools.attr_stability.gapfill import _PRIORITY_BLOCK
    assert CHAIN in _PRIORITY_BLOCK
    assert "没有例外" in _PRIORITY_BLOCK


@pytest.mark.parametrize("src,label", [
    (PROMPT, "prompt"),
    (ROOT / "tools" / "attr_stability" / "gapfill.py", "gapfill"),
])
def test_no_other_priority_order(src, label):
    """除这条链之外，不允许再出现另一条 6 来源的排序（防第二份定义悄悄长出来）。"""
    txt = src.read_text(encoding="utf-8")
    orders = re.findall(r"标题\s*>\s*五点描述\s*>\s*详细参数[^\n，。；]*", txt)
    for o in orders:
        assert o.strip().startswith("标题 > 五点描述 > 详细参数"), (label, o)
