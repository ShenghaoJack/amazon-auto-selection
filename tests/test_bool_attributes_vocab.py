# -*- coding: utf-8 -*-
"""「是否*」属性统一成 是/否/未明确（2026-10-08 用户决定）。

背景：11 个 是否* 属性的口径原先**项目自己就矛盾**——
`19_再聚类/主体材质-粒度建议.txt` 说「二值属性: 强制 是/否」，
而 spec hint / rules.json 写的是三档描述值（可洗碗机清洗/仅手洗/不可洗碗机 …），
于是模型按后者填，出现 `双面不同材质` 这种「材质事实」占着「是否双面」的值。

本文件锁住：
  ① 受控值必须是 `是/否`（保留值 未明确/不适用 由闸门放行，不写进词表）
  ② hint 必须给出**极性**（哪个方向算「是」）—— 否则模型会猜，猜反比不填更糟
  ③ hint 必须说明「具体证据写进来源列，不要写进值」
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAT = "Cutting Boards-US-202608"
# ★ 定义层走解析器（2026-10-10 迁移后）：**测试也是消费者** ——
#   原先直接拼 `data/<CAT>/<CAT>-attribute-spec.json`，定义层迁到 `_catalog/<类目>/`
#   之后**整个测试文件在收集阶段就 `FileNotFoundError`**（全量回归实测抓到）。
from tools.attr_stability.catalog import resolve_rules_path, resolve_spec_path  # noqa: E402

SPEC = json.loads(resolve_spec_path(ROOT / "data", CAT).read_text(encoding="utf-8-sig"))
RULES = json.loads(resolve_rules_path(ROOT / "data", CAT,
                                      "cutting_boards.rules.json").read_text(encoding="utf-8-sig"))

BOOL_NAMES = [a["name"] for a in SPEC["attributes"]
              if str(a.get("name", "")).startswith("是否")]
SPEC_BY = {a["name"]: a for a in SPEC["attributes"] if a.get("name")}
RULES_BY = {a["name"]: a for a in RULES["attributes"] if a.get("name")}


def test_all_bool_attrs_present():
    assert len(BOOL_NAMES) == 11, BOOL_NAMES   # 2026-10-08 新增「是否双面不同材质」


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_controlled_values_are_yes_no(name):
    vals = (RULES_BY[name].get("params") or {}).get("values") or []
    assert vals == ["是", "否"], "%s 受控值=%s" % (name, vals)


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_reserved_values_not_in_vocab(name):
    """`未明确/不适用` 是闸门放行的保留值，不该写进受控值词表（否则会当成可选项列给模型）。"""
    from tools.attr_stability.attr_stability import RESERVED_VALUES
    vals = (RULES_BY[name].get("params") or {}).get("values") or []
    assert not (set(vals) & set(RESERVED_VALUES)), name


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_hint_declares_yes_no_and_polarity(name):
    h = str(SPEC_BY[name].get("hint") or "")
    assert "典型取值 是 / 否" in h, name
    assert "本属性是是非题" in h, name
    assert "**极性**" in h, name
    assert h.count("本属性是是非题") == 1, "说明被重复叠加: %s" % name


@pytest.mark.parametrize("name", BOOL_NAMES)
def test_hint_tells_where_evidence_goes(name):
    h = str(SPEC_BY[name].get("hint") or "")
    assert "来源列" in h, name


def test_polarity_is_not_derived_from_word_order():
    """★ 极性不能靠「典型取值第一项」推：这三条原来的词序会推出相反的结论。"""
    cases = {
        "是否食品级安全": ("有食品级认证", "明确不是食品级"),
        "是否环保认证": ("有环保认证", "明确无任何环保认证"),
        "是否双面可用": ("双面可用", "单面使用"),
    }
    for name, (yes, no) in cases.items():
        h = str(SPEC_BY[name].get("hint") or "")
        assert f"`是` = {yes}" in h, (name, h[-160:])
        assert f"`否` = {no}" in h, (name, h[-160:])
    # 反例：绝不能出现「是=BPA-Free、否=食品级」这种从词序推出来的荒谬极性
    h = str(SPEC_BY["是否食品级安全"].get("hint") or "")
    assert "`是` = BPA-Free" not in h
    h2 = str(SPEC_BY["是否双面可用"].get("hint") or "")
    assert "`否` = 单面使用 / 双面不同材质" not in h2


def test_hint_block_injects_yes_no_vocab():
    """走生产同一条拼提示词路径，确认受控值真的注入了 `是 ｜ 否`。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from steps.step17_attr_annotation import Step17AttrAnnotation
    from tools.attr_stability import RuleSource

    rules = RuleSource.load(str(ROOT / "data" / CAT), "cutting_boards")
    st = Step17AttrAnnotation()
    st._cat = CAT
    st._spec_attributes = [a for a in SPEC["attributes"] if a.get("name")]
    blk = st._build_attr_hint_block(None, rules)
    assert blk
    for name in BOOL_NAMES:
        # 紧跟该属性名之后的续行里应有「受控值...是 ｜ 否」
        m = re.search(r"- %s：[^\n]*\n\s*\*\*受控值[^\n]*是 ｜ 否" % re.escape(name), blk)
        assert m, "%s 的受控值没注入成 是｜否" % name
