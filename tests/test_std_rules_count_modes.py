# 单元测试: count_modes 规则的「枚举即计数」闸门 (count_from_modes)
#
# 背景(2026-09-21): 猫饮水机 `水流模式` 的标准列出现自相矛盾 —— 同一行的
# 「模式集」填出了 `水龙头模式+气泡模式`，而「模式数」却是 `未明确`。
# 根因: rule_count_modes 只在原值写了「N种」时才给模式数；原值只**枚举**模式名
# (如 `水龙头模式、气泡模式`、`喷泉模式`) 时一律留 未明确，共 72 行。
# 口径: 既然该原值可信到能填「模式集」，就该同样可信地推出「模式数」。
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

SC = ["水流模式数_标准", "水流模式_标准"]
MODES = {
    "水龙头模式": "水龙头模式", "喷泉模式": "喷泉模式", "气泡模式": "气泡模式",
    "涌泉模式": "涌泉模式", "瀑布模式": "瀑布模式", "自由落水模式": "自由落水模式",
}
BASE = {"name": "水流模式", "rule": "count_modes", "subcols": SC,
        "params": {"mode_map": MODES, "special": [{"contains": "水流可调",
                                                   "cnt": "可调（未给模式数）",
                                                   "modes": "未明确", "status": "REVIEW"}]}}
ON = {"name": "水流模式", "rule": "count_modes", "subcols": SC,
      "params": dict(BASE["params"], count_from_modes=True)}


def test_without_param_keeps_legacy_behaviour():
    """未声明 count_from_modes 时保持旧行为(向后兼容)。"""
    sub, _, _, _ = apply_rule(BASE, "水龙头模式、气泡模式", {})
    assert sub == {"水流模式数_标准": "未明确", "水流模式_标准": "水龙头模式+气泡模式"}


def test_enumeration_implies_count():
    """声明后: 枚举出几个模式, 模式数就是几。"""
    sub, _, _, _ = apply_rule(ON, "水龙头模式、气泡模式", {})
    assert sub == {"水流模式数_标准": 2, "水流模式_标准": "水龙头模式+气泡模式"}


def test_single_mode_implies_one():
    sub, _, _, _ = apply_rule(ON, "喷泉模式", {})
    assert sub == {"水流模式数_标准": 1, "水流模式_标准": "喷泉模式"}


def test_explicit_count_still_wins():
    """原值明确写了「N种」时以它为准, 不受枚举个数影响。"""
    sub, _, _, _ = apply_rule(ON, "3种水流：水龙头模式、气泡模式", {})
    assert sub["水流模式数_标准"] == 3


def test_unknown_stays_unknown():
    sub, _, _, _ = apply_rule(ON, "未明确", {})
    assert sub == {"水流模式数_标准": "未明确", "水流模式_标准": "未明确"}


def test_special_branch_not_affected():
    """special 分支(如「水流可调」)先返回, 不应被 count_from_modes 覆盖。"""
    sub, _, _, _ = apply_rule(ON, "水流可调", {})
    assert sub["水流模式数_标准"] == "可调（未给模式数）"
    assert sub["水流模式_标准"] == "未明确"


# ---- 集成: 真实类目配置 ---------------------------------------------------

def _cat_rules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    import json
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("attr", ["水流模式", "运行模式"])
def test_category_declares_count_from_modes(attr):
    rules = _cat_rules()
    a = next(x for x in rules["attributes"] if x["name"] == attr)
    assert a["params"].get("count_from_modes") is True


def test_category_flow_enumeration_resolves():
    """真实配置下, 枚举原值不再产出「模式集有值但模式数未明确」的矛盾行。"""
    rules = _cat_rules()
    a = next(x for x in rules["attributes"] if x["name"] == "水流模式")
    sub, _, _, _ = apply_rule(a, "水龙头模式、气泡模式", {})
    assert isinstance(sub["水流模式数_标准"], int)
    assert sub["水流模式数_标准"] == len(sub["水流模式_标准"].split("+"))


def test_category_unknown_modes_order_line_still_unknown():
    """源表原值就是「未明确」的行(运行模式 390 行)不受影响。"""
    rules = _cat_rules()
    for attr in ("水流模式", "运行模式"):
        a = next(x for x in rules["attributes"] if x["name"] == attr)
        sub, _, _, _ = apply_rule(a, "未明确", {})
        assert sub[a["subcols"][0]] == "未明确"
