# -*- coding: utf-8 -*-
"""「主体材质写 1 个还是 2 个」↔ 信号属性（`是否双面不同材质`）的双向一致性。

用户口径（2026-10-08）：
  · 其他属性表明**多材质**时 → 主体材质写 **2 个** `A+B`（`不锈钢+塑料`）；
  · 没有该信号时 → 主体材质**只写 1 个**。

为此把原先混在一起的语义**拆开**：
  · `是否双面可用`     —— 能不能两面用
  · `是否双面不同材质` —— **两面材质是否不同**（新增，专作分量数信号）

设计要点（都是踩出来的）：
  ① **双向**判：信号=是 却写 1 个要报；信号=否 却写 2 个也要报；
  ② 信号=未明确/空 → **不判**（「没填」不等于「填错」）；
  ③ 信号属性**整列缺失**（刚加属性、表还是上一轮的）→ 明确报「未检查」，
     **不许静默通过**（这和"没交付表就不算通过"是同一条原则）。
"""
from __future__ import annotations

from tools.attr_stability.cross_checks import check_parts_dependency

SIG = "是否双面不同材质"
RULES = {"attributes": [
    {"name": "主体材质", "params": {
        "max_parts": 2,
        "parts_when": [{"属性": SIG, "含": ["是"]}]}},
    {"name": SIG, "params": {"values": ["是", "否"]}},
]}


def _rows(*pairs):
    return [{"xl_row": i + 2, "attributes": {"主体材质": m, SIG: s}}
            for i, (m, s) in enumerate(pairs)]


def test_signal_yes_requires_two_parts():
    """★ 信号=是 却只写 1 个 → 报。"""
    bad = check_parts_dependency(_rows(("竹制", "是")), RULES)
    assert len(bad) == 1 and "应写 2 个" in bad[0]["说明"]
    assert bad[0]["行号"] == 2


def test_no_signal_requires_single_part():
    """★ 信号=否 却写了 2 个 → 也报（双向，不是单向）。"""
    bad = check_parts_dependency(_rows(("不锈钢+塑料", "否")), RULES)
    assert len(bad) == 1 and "应只写 1 个" in bad[0]["说明"]


def test_compliant_rows_pass():
    assert check_parts_dependency(_rows(("不锈钢+塑料", "是"), ("竹制", "否")), RULES) == []


def test_unknown_signal_is_not_judged():
    """信号=未明确/空 → 不判。否则会把「没填」误判成「填错」。"""
    assert check_parts_dependency(_rows(("不锈钢+塑料", "未明确"),
                                        ("竹制", ""),
                                        ("不锈钢+塑料", None)), RULES) == []


def test_missing_signal_column_is_reported_not_passed():
    """★ 信号属性**整列缺失** → 报「未检查」，不许静默通过。"""
    rows = [{"xl_row": 2, "attributes": {"主体材质": "竹制"}}]
    bad = check_parts_dependency(rows, RULES)
    assert len(bad) == 1 and bad[0].get("未检查") is True
    assert "未检查" in bad[0]["说明"] and SIG in bad[0]["说明"]


def test_three_parts_also_flagged_via_dependency():
    """3 个分量且信号=否 → 报（`max_parts` 另有独立检查，这里只验依赖逻辑）。"""
    bad = check_parts_dependency(_rows(("不锈钢+塑料+木制", "否")), RULES)
    assert bad and "应只写 1 个" in bad[0]["说明"]


def test_no_parts_when_declaration_is_noop():
    assert check_parts_dependency(_rows(("竹制", "是")),
                                 {"attributes": [{"name": "主体材质"}]}) == []


def test_empty_rows_is_noop():
    assert check_parts_dependency([], RULES) == []


def test_shipped_category_declares_the_signal():
    """锁住**发布的那份声明**：主体材质 必须声明 parts_when，且信号属性在 spec 里存在。"""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "data" / "Cutting Boards-US-202608"
    # ★ 定义层走解析器（2026-10-10 迁移后）—— 测试也是消费者
    from tools.attr_stability.catalog import resolve_rules_path, resolve_spec_path
    rules = json.loads(resolve_rules_path(root.parent, root.name, "cutting_boards.rules.json")
                       .read_text(encoding="utf-8-sig"))
    spec = json.loads(resolve_spec_path(root.parent, root.name)
                      .read_text(encoding="utf-8-sig"))
    names = {a["name"] for a in spec["attributes"]}
    mat = next(a for a in rules["attributes"] if a["name"] == "主体材质")
    pw = (mat.get("params") or {}).get("parts_when")
    assert pw, "主体材质 必须声明 parts_when"
    assert mat["params"].get("max_parts") == 2
    for c in pw:
        assert c["属性"] in names, "信号属性必须在 spec 里存在: %s" % c["属性"]
    assert SIG in names, "spec 里必须有 %s" % SIG
