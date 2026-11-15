# -*- coding: utf-8 -*-
"""词表放权(方案 A/B) 回归：vocab_policy 三级 + 新词候选收集 + 批量确认。"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.attr_stability import (  # noqa: E402
    AttrSpec,
    RuleSource,
    apply_vocab_acceptance,
    build_vocab_review_prompt,
    check_row,
    gate_run,
)


def _rules(attrs):
    rs = RuleSource()
    rs.attrs = {a["name"]: AttrSpec(**a) for a in attrs}
    return rs


def _row(rules, **std_map):
    A = {}
    for name in rules.attrs:
        A[name] = std_map.get(name, "未明确")
        A[name + "_来源"] = std_map.get(name + "_来源", "")
        # ★ 2026-10-09：新 schema 要求**每个属性都给 `_依据` 原文**（提示词 §3.3e），
        #   闸门会硬拦漏依据。本辅助函数统一补上，让各用例只关心自己那条断言
        #   （否则一加就整批 REJECT，测试全红却看不出是"依据"在拦）。
        A[name + "_依据"] = std_map.get(name + "_依据", "测试用依据原文")
    return {
        "xl_row": 1, "category_check": {"判定": "属于本类目"}, "attributes": A,
        "尺寸_拆列": {"长": "", "宽": "", "高": ""}, "尺寸_依据": "",
        "suitable_scene_规范串": "", "词表外词": std_map.get("词表外词", []),
        "pack_count_数值": None, "跨列纠错": [], "冲突": [],
    }


# ---------------------------------------------------------------- 方案 A：vocab_policy
def test_closed_rejects_out_of_vocab(ws_tmp):
    """closed(默认)：单选词表外值 = ERR，行为不变。"""
    rules = _rules([{"name": "供电方式", "values": ["电池供电", "USB充电"]}])
    issues = check_row(_row(rules, 供电方式="太阳能充电"), rules)
    assert any(i.level == "ERR" and "不在受控值词表内" in i.why for i in issues)


def test_suggestive_allows_and_collects_with_evidence(ws_tmp):
    """suggestive：词表外值不再 ERR，但收集为新词候选(新 schema 无证据原文, 只收词)。"""
    rules = _rules([{"name": "供电方式", "values": ["电池供电", "USB充电"],
                     "vocab_policy": "suggestive"}])
    nv = []
    issues = check_row(_row(rules, 供电方式="太阳能充电",
                            词表外词=["太阳能充电"]),
                       rules, new_vocab=nv)
    assert not any(i.level == "ERR" for i in issues), issues
    assert nv == [{"属性": "供电方式", "词": "太阳能充电", "行号": 1,
                   "policy": "suggestive"}]


def test_open_allows_without_evidence(ws_tmp):
    """open：词表外值完全放开，无 ERR 也无 WARN，但仍收集候选。"""
    rules = _rules([{"name": "使用场景", "values": ["厨房"], "shape": "multi",
                     "vocab_policy": "open"}])
    nv = []
    issues = check_row(_row(rules, 使用场景="厨房、野营"), rules, new_vocab=nv)
    assert not any(i.level in ("ERR", "WARN") for i in issues), issues
    assert any(x["词"] == "野营" for x in nv)


def test_multi_closed_still_warns_not_errors(ws_tmp):
    """回归：多值 closed 属性词表外词仍只 WARN(要求列入词表外词)，不是 ERR。"""
    rules = _rules([{"name": "suitable_scene", "values": ["厨房"], "shape": "multi"}])
    issues = check_row(_row(rules, suitable_scene="厨房、野营"), rules)
    assert not any(i.level == "ERR" for i in issues)
    assert any(i.level == "WARN" and "词表外词" in i.why for i in issues)


def test_gate_run_collects_vocab(ws_tmp):
    """gate_run 的 new_vocab 参数把整份运行的候选收集起来。"""
    rules = _rules([{"name": "供电方式", "values": ["电池供电"],
                     "vocab_policy": "open"}])
    nv = []
    gate_run([_row(rules, 供电方式="太阳能充电")], rules, new_vocab=nv)
    assert any(x["词"] == "太阳能充电" for x in nv)


# ---------------------------------------------------------------- 方案 B：初审 + 批量确认
def test_vocab_review_prompt_contains_candidates(ws_tmp):
    candidates = [{"属性": "供电方式", "词": "太阳能充电", "出现次数": 3,
                   "行号": [1, 2, 5], "policy": "suggestive", "依据样本": ["solar powered"]}]
    p = build_vocab_review_prompt(candidates, "cat")
    assert "太阳能充电" in p and "独立" in p and "采纳|驳回" in p


def test_apply_vocab_acceptance_writes_and_dedupes(ws_tmp):
    """批量确认：采纳的词写进 rules.json values，重复跳过，驳回不写。"""
    rules_path = os.path.join(ws_tmp, "cat.rules.json")
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump({"attributes": [
            {"name": "供电方式", "values": ["电池供电", "USB充电"]},
        ]}, fh, ensure_ascii=False)

    out = apply_vocab_acceptance(rules_path, [
        {"属性": "供电方式", "词": "太阳能充电", "结论": "采纳", "规范化写法": "太阳能供电"},
        {"属性": "供电方式", "词": "电池供电", "结论": "采纳", "规范化写法": "电池供电"},  # 重复
        {"属性": "供电方式", "词": "solar pwoer", "结论": "驳回", "规范化写法": ""},     # 驳回
        {"属性": "不存在属性", "词": "x", "结论": "采纳", "规范化写法": "x"},            # 未匹配
    ])

    assert out["已新增"] == [{"属性": "供电方式", "词": "太阳能供电"}]
    assert out["跳过重复"] == [{"属性": "供电方式", "词": "电池供电"}]
    assert out["驳回"] == [{"属性": "供电方式", "词": "solar pwoer"}]
    assert out["未匹配属性"] == [{"属性": "不存在属性", "词": "x"}]

    with open(rules_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert "太阳能供电" in saved["attributes"][0]["values"]
    assert saved["attributes"][0]["values"] == ["电池供电", "USB充电", "太阳能供电"]


def test_attr_spec_from_modern_maps_rule_params(tmp_path):
    """新格式(rule/subcols/params) → AttrSpec: 受控值取自 params.values / main_map。"""
    from tools.attr_stability import attr_spec_from_modern

    enum = attr_spec_from_modern({"name": "供电方式", "rule": "enum",
                                  "params": {"values": ["电池供电", "USB供电"]}})
    assert enum.type == "enum" and enum.shape == "single"
    assert enum.values == ["电池供电", "USB供电"] and enum.vocab_policy == "closed"

    mat = attr_spec_from_modern({"name": "材质", "rule": "material",
                                 "params": {"main_map": {"不锈钢": "不锈钢", "陶瓷": "陶瓷"}}})
    assert mat.type == "enum" and mat.values == ["不锈钢", "陶瓷"]

    multi = attr_spec_from_modern({"name": "过滤类型", "rule": "multi",
                                   "params": {"values": ["活性炭", "滤网"]}})
    assert multi.shape == "multi" and multi.values == ["活性炭", "滤网"]

    numeric = attr_spec_from_modern({"name": "容量", "rule": "numeric",
                                     "params": {"unit_factors": {"L": 1000}}})
    assert numeric.type == "numeric" and numeric.values == []

    ocr = attr_spec_from_modern({"name": "图片OCR文字", "rule": "ocr", "params": {}})
    assert ocr.vocab_policy == "open" and ocr.values == []


def test_parse_vocab_cluster_map():
    """聚类结果里取「原值→规范值」归并映射(供人工审核)。"""
    from tools.attr_stability.onboard import parse_vocab_cluster_map

    raw = {"属性": "材质", "规范值": ["不锈钢", "塑料"],
           "归并": [{"原值": "Stainless Steel", "归入": "不锈钢"},
                    {"原值": "不锈钢", "归入": "不锈钢"}]}
    assert parse_vocab_cluster_map(raw, "材质") == [
        {"原值": "Stainless Steel", "归入": "不锈钢"},
        {"原值": "不锈钢", "归入": "不锈钢"},
    ]
    assert parse_vocab_cluster_map({}, "材质") == []
    assert parse_vocab_cluster_map("bad", "材质") == []
