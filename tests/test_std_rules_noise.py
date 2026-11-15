# 单元测试: 噪音规则(noise)的营销宣称闸门与视觉复核覆盖
#
# 背景(2026-09-20): 交付表 `噪音_dB_标准` 出现异常值 0 —— 两行 ASIN(B0AF13FD56 /
# B09D6EDDE1)的卖点写「operating volume of 0 dB」「zero decibels in the standby mode」,
# 机械「数值提取/单位转换」照单全收产出 0。0 dB 是人耳听阈, 整机(含水泵)不可能为 0 dB;
# 且后者自述是 **standby mode 待机**噪音, 并非运行噪音。图片证据(图廊大图 + A+ 长图)
# 均无分贝数值, 只有定性静音字样。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import (  # noqa: E402
    MT_REVIEW,
    MT_SYN,
    MT_UNIT,
    MT_VISION_CHECK,
    RULES,
    ST_REVIEW,
    ST_SYN,
    ST_UNIT,
    ST_VISION_CHECK,
    apply_rule,
)

PARAMS = {
    "subcols": ["噪音_dB_标准", "静音等级_标准"],
    "special": {"超静音": "超静音", "静音": "静音"},
    "unit": "dB",
    "level_threshold": 30,
    "min_plausible_db": 10,
    "vision_override": {
        "0dB": {"db": "未明确", "level": "静音", "note": "图片无分贝数值"}
    },
}
CFG = {"name": "噪音水平", "rule": "noise", "subcols": PARAMS["subcols"], "params": PARAMS}


def test_noise_registered():
    assert RULES["noise"] is not None


def test_zero_db_without_guard_keeps_legacy_behaviour():
    """未配闸门时保持旧行为(向后兼容) —— 闸门是类目声明式参数, 不是硬编码。"""
    cfg = json.loads(json.dumps(CFG))
    cfg["params"].pop("vision_override", None)
    cfg["params"].pop("min_plausible_db", None)
    sub, st, mt, _ = apply_rule(cfg, "0dB", {})
    assert sub["噪音_dB_标准"] == 0
    assert st == ST_UNIT and mt == MT_UNIT


def test_zero_db_below_floor_goes_to_review_not_zero():
    """有闸门但无视觉结论时: 0 不得写进交付表, 降级为 未明确+静音 并转复核。"""
    cfg = json.loads(json.dumps(CFG))
    cfg["params"].pop("vision_override", None)
    sub, st, mt, issues = apply_rule(cfg, "0dB", {})
    assert sub["噪音_dB_标准"] == "未明确", "0 dB 是听阈, 不得作为实测值产出"
    assert sub["静音等级_标准"] == "静音"
    assert st == ST_REVIEW and mt == MT_REVIEW
    assert issues and "物理下限" in issues[0]


def test_zero_db_vision_override_wins():
    """实测两行: 有视觉复核结论时按视觉改判, 状态记 已图片视觉复核。"""
    sub, st, mt, issues = apply_rule(CFG, "0dB", {})
    assert sub == {"噪音_dB_标准": "未明确", "静音等级_标准": "静音"}
    assert st == ST_VISION_CHECK and mt == MT_VISION_CHECK
    assert issues and "图片视觉复核" in issues[0]


def test_plausible_values_unchanged():
    """正常数值不受影响: <=30 静音, >30 普通。"""
    sub, st, mt, _ = apply_rule(CFG, "20dB", {})
    assert sub == {"噪音_dB_标准": 20, "静音等级_标准": "静音"}
    assert st == ST_UNIT and mt == MT_UNIT

    sub, st, _, _ = apply_rule(CFG, "45 dB", {})
    assert sub == {"噪音_dB_标准": 45, "静音等级_标准": "普通"}


def test_value_exactly_at_floor_is_kept():
    """等于下限(10dB)仍视为有效实测值, 边界不含糊。"""
    sub, _, _, _ = apply_rule(CFG, "10dB", {})
    assert sub["噪音_dB_标准"] == 10


def test_qualitative_special_still_maps():
    """定性值仍走同义词分支, 不受新逻辑影响。"""
    sub, st, mt, _ = apply_rule(CFG, "静音", {})
    assert sub == {"噪音_dB_标准": "未明确", "静音等级_标准": "静音"}
    assert st == ST_SYN and mt == MT_SYN


def test_unknown_still_unknown():
    sub, _, _, _ = apply_rule(CFG, "", {})
    assert sub == {"噪音_dB_标准": "未明确", "静音等级_标准": "未明确"}


@pytest.mark.parametrize("raw", ["0 dB", "0dB"])
def test_zero_variants_all_guarded(raw):
    """写法变体(含空格)也要落闸。"""
    sub, _, _, _ = apply_rule(CFG, raw, {})
    assert sub["噪音_dB_标准"] == "未明确"


@pytest.mark.parametrize("raw", ["0 dB", "0dB", "0decibels"])
def test_no_variant_ever_emits_numeric_zero(raw):
    """核心不变量: 任何 0 写法都不得产出数值 0。

    `0decibels` 单位拼写非 `dB`, 走既有的「单位解析失败」分支(保留原值文本 + 待人工复核),
    同样不会产出数值 0 —— 该分支行为是既有约定, 此处只锁"不出数值 0"这条底线。
    """
    sub, st, _, _ = apply_rule(CFG, raw, {})
    assert sub["噪音_dB_标准"] != 0
    assert isinstance(sub["噪音_dB_标准"], str)


# ---- 集成: 真实类目配置(cat_fountains.rules.json) 的噪音属性 -------------------

def _cat_attr():
    """读类目 rules.json 的 噪音水平 配置; 找不到则 skip。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        rules = json.load(fh)
    for a in rules["attributes"]:
        if a["name"] == "噪音水平":
            return rules, a
    pytest.skip("规则里没有 噪音水平")


def test_category_declares_plausibility_floor():
    """类目必须声明物理下限, 否则 0dB 会被当实测值写出去(本 bug 的根因)。"""
    _, a = _cat_attr()
    assert a["params"].get("min_plausible_db") == 10


def test_category_has_no_value_level_zero_override():
    """`0dB` 不能用 params.vision_override 按原值一刀切。

    实测: 同为原值 `0dB`, B09D6EDDE1 的正解是 30(卖点同段有 "less than 30 decibels"),
    B0AF13FD56 的正解是 未明确。一刀切必然错一行 → 必须走 row_overrides 逐行改判。
    """
    _, a = _cat_attr()
    assert "0dB" not in (a["params"].get("vision_override") or {})


def test_category_zero_db_is_downgraded_by_floor():
    """真实配置下 0dB 落闸: 不出数值, 降级 未明确+静音, 转人工复核。"""
    _, a = _cat_attr()
    sub, st, mt, issues = apply_rule(a, "0dB", {})
    assert sub["噪音_dB_标准"] == "未明确"
    assert sub["静音等级_标准"] == "静音"
    assert st == ST_REVIEW and mt == MT_REVIEW
    assert issues and "物理下限" in issues[0]


def test_category_row_overrides_cover_both_zero_rows():
    """两行 0dB 的逐行改判必须都在, 且用 dict 写法(否则第二个子列会丢)。"""
    rules, a = _cat_attr()
    ov = {r["asin"]: r for r in rules.get("row_overrides", [])
          if r["attr"] == "噪音水平" and r["when"] == "0dB"}
    assert set(ov) == {"B0AF13FD56", "B09D6EDDE1"}
    for asin, r in ov.items():
        assert isinstance(r["then"], dict), "%s 的 then 必须是子列字典" % asin
        assert set(r["then"]) == {"噪音_dB_标准", "静音等级_标准"}
    assert ov["B09D6EDDE1"]["then"]["噪音_dB_标准"] == 30, "待机0dB的同行运行值是 <30dB"
    assert ov["B0AF13FD56"]["then"]["噪音_dB_标准"] == "未明确"


def test_category_normal_values_unaffected():
    """真实配置下常规值不变(改动面必须只有那 2 行)。"""
    _, a = _cat_attr()
    assert apply_rule(a, "25dB", {})[0] == {"噪音_dB_标准": 25, "静音等级_标准": "静音"}
    assert apply_rule(a, "40dB", {})[0] == {"噪音_dB_标准": 40, "静音等级_标准": "普通"}
    assert apply_rule(a, "静音", {})[0] == {"噪音_dB_标准": "未明确", "静音等级_标准": "静音"}
