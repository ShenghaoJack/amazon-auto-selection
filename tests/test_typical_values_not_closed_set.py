# -*- coding: utf-8 -*-
"""「典型取值」是**示例**，不是闭集（用户口径 2026-10-09，只做 ①）。

问题是怎么产生的
===============================================================================
`产品类型` 的 hint 里写着「典型取值 竹制砧板 / 实木砧板 / 不锈钢砧板 / …」，
而提示词渲染成：

    逐条照此取值
    凡标出受控值的属性，输出必须逐字符取自该列表

→ 模型把「典型取值」当成**闭集**。实测证据：

    产品类型 的聚类输入（步骤17 产出的原值）= 7 个，**恰好等于** hint 的典型取值，
                                               完全没有「钛金属砧板」
    主体材质 的聚类输入 = 12 个，**有**「钛金属」还有 4 个含钛金属的组合

26 个纯钛砧板全被塞进「不锈钢砧板」→ **缺的那个类从未被产出** →
**聚类永远聚不出它**（聚类只能聚"已经产出过的值"）→ 冲突永久存在。

修法（**通用、不绑类目**）
===============================================================================
不去假设"某属性必须按某维度命名"（有的类目产品类型按材质命名，有的按功能命名）——
而是**把闭集语义解开**：说清「典型取值（示例）」与「受控值（强制值域）」的区别，
并明确「证据指向示例之外就照实写，走新词候选通路」。

本文件锁三件事：
  ① 提示词有 §1b.9 这一节，且写明「不是闭集」；
  ② 属性块渲染里不再出现「逐条照此取值」，且带上了「判据永远是原文证据」；
  ③ §1b 既有编号与交叉引用**没有被破坏**（我插入时撞过号，导致 10 处引用指错）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "prompts" / "23_llm_only_pipeline.md"
STEP17 = ROOT / "steps" / "step17_attr_annotation.py"


def test_prompt_has_section_1b9():
    txt = PROMPT.read_text(encoding="utf-8")
    assert "### 1b.9" in txt
    assert "不是闭集" in txt
    assert "判据永远是原文证据" in txt
    # 必须区分两个概念
    assert "典型取值" in txt and "受控值" in txt
    # 必须给出新词候选通路，否则模型只能"硬塞"
    assert "新词候选" in txt


def test_1b_numbering_and_crossrefs_intact():
    """★ 我插入新节时撞过号 → §1b.2/1b.3/1b.4/1b.5 共 10 处引用全部指错。

    所以锁：编号连续无重复，且每条 `§1b.N` 引用都指向**真实存在**的小节。
    """
    txt = PROMPT.read_text(encoding="utf-8")
    nums = re.findall(r"^### (1b\.\d+)", txt, flags=re.M)
    assert nums == ["1b.%d" % i for i in range(1, len(nums) + 1)], \
        "§1b 编号必须从 1b.1 连续递增且不重复，实际: %s" % nums
    have = set(nums)
    for m in re.finditer(r"§(1b\.\d+)", txt):
        assert m.group(1) in have, "交叉引用指向不存在的小节: §%s" % m.group(1)


def test_hint_block_lifts_closed_set_semantics():
    """属性块渲染文案：不再「逐条照此取值」，且明确判据是证据。"""
    src = STEP17.read_text(encoding="utf-8")
    assert "逐条照此取值" not in src, "这句把「典型取值」变成了闭集"
    assert "判据永远是原文证据" in src
    assert "不是可选清单、更不是闭集" in src
    # 必须点明「没标受控值时按证据写」，否则冷启动首轮只能硬塞
    assert "按证据写出真实的形态/类别" in src
    # 必须点明新词候选通路
    assert "新词候选" in src


def test_hint_block_does_not_hardcode_category_terms():
    """★ 通用性：这段文案**不许绑任何类目术语**。

    用户明确要求「有些类目产品类型按功能命名，有些按材质命名」都要适配 ——
    所以规则必须与类目无关（示例用「有线连接/无线充电」这种跨类目例子）。
    """
    src = STEP17.read_text(encoding="utf-8")
    i = src.find("先分清两个概念")
    assert i > 0
    seg = src[i:i + 2400]
    for term in ("砧板", "材质大类", "钛金属", "竹制", "不锈钢砧板"):
        assert term not in seg, "属性块文案里不该出现类目术语: %s" % term


# ---------------------------------------------------------------- 强化：自洽 + 构词一致
def test_hint_block_requires_value_basis_self_consistency():
    """★★ 最关键的一条：**值必须与自己写的 `依据` 自洽**。

    为什么必须有（实测，2026-10-09 全新类目冷启动）：
      `产品类型=不锈钢砧板`，而它的 `依据` 是
      「标题：100% Pure **Titanium** Cutting Board」—— **值与依据直接矛盾**。

    只写「典型取值不是闭集」**不够**：20 个纯钛砧板仍然全填了「不锈钢砧板」。
    因为模型不是被"闭集"逼的，而是属性说明里写着「本属性看用途形态，材质看主体材料」
    → 它认为"类型和材质无关"。所以必须加**可自查**的自洽规则。
    """
    src = STEP17.read_text(encoding="utf-8")
    assert "你填的值必须与你写的" in src
    assert "自洽" in src
    # 必须给出自查方法（否则模型不会真的去查）
    assert "对不上就是填错" in src


def test_hint_block_requires_isomorphic_naming():
    """★★ 样例里嵌着别的属性语义时，要**照样例的构词方式补出缺失类别**。

    例：`产品类型` 的样例是「竹制砧板 / 实木砧板 / 玻璃砧板」——名字里嵌着材质，
    说明它实际按材质细分；证据是钛就该补「钛金属砧板」，而不是挑近似的「不锈钢砧板」。

    注意这条是**数据驱动**的（看样例嵌了什么），**不是**写死"类型从材质派生" ——
    因为有的类目按功能命名、有的按材质命名（用户明确要求两者都适配）。
    """
    src = STEP17.read_text(encoding="utf-8")
    assert "嵌着" in src
    assert "同样的构词方式补出" in src or "构词方式补出它" in src
    # 必须点明"按什么维度命名是数据决定的"这条通用前提
    assert "数据决定的" in src


def test_prompt_has_both_strengthened_rules():
    txt = PROMPT.read_text(encoding="utf-8")
    assert "你填的值必须与你写的" in txt
    assert "嵌着" in txt
    assert "数据决定的" in txt

