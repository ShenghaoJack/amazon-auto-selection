# 单元测试: `过滤类型_标准` 与源表原始列 `是否含滤芯` 的一致性修正
#
# 背景(2026-09-22, 用户提问):「过滤类型列里有『有过滤系统』，是否含滤芯列里有『是』，
#   不是相冲突了吗?」
#   结论: **不冲突**。两列口径不同 ——
#     · `过滤类型_标准` = 过滤**结构**分类(有过滤系统/有滤芯/无需过滤器/未明确)
#     · 源表 `是否含滤芯` = 商品是否**随附滤芯**
#   `有过滤系统` + `是` 是本类目主体(453/605 行)，属正常组合。
#   真正的矛盾只有两类:
#     A. 过滤类型=有滤芯 × 是否含滤芯=否   → 19 行(标题全部 No Filter/Filterless/Filter-Free)
#     B. 过滤类型=未明确 × 是否含滤芯=是   →  8 行(标题含 with Filter / 6 Filters)
#
# A 组根因: 文本层规则已从原始列算对(raw `滤芯`/`无过滤器`)，但
#   `cat_fountains.vision.json` 的视觉条目把 `过滤类型_标准` 写成 `有滤芯(材料未明确)`，
#   而**视觉在改判之后生效** → 覆盖回错值。且这些条目的「依据」原文恰是
#   'No Filters' / 'Zero Filter' / 'no replacement cartridges' —— 结论与自身依据相反。
#
# 后续(同日, 用户决定): **`是否含滤芯` 这一列不要了，直接关注材料** →
#   标准化属性已从 rules.json 移除(源表原始列仍在输入表里，跨列守卫改为对源列做)。
# 本测试锁死修正结果 + "该列不再输出"，防止视觉文件重生成或误加列时把问题带回来。
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

TYP = "过滤类型_标准"
MAT = "过滤材料_标准"
CNT = "过滤材料种类数_标准"
LAY = "过滤层数（营销宣称）_标准"

A_ASINS = {
    "B01E8486C0", "B0E1DDEA9A", "B0831F2D17", "B01657C5AA", "B0AF13FD56",
    "B0055FBF45", "B09D6EDDE1", "B041E008C7", "B08BDF026D", "B097A22329",
    "B05736DDB4", "B060C40707", "B0F0C71A04", "B06EB13D3E", "B0AF8D1FB8",
    "B0166377D9", "B0B5A22487", "B0BE39C98F", "B0DAC04B23",
}
B_ASINS = {
    "B0F183A006", "B06166075B", "B0F79F8921", "B0860D5824",
    "B0603AB709", "B0A70B0E54", "B06C45A7B3", "B02F6B5191",
}
A_THEN = {LAY: "不适用", MAT: "不适用", CNT: "不适用", TYP: "无需过滤器"}
B_THEN = {MAT: "滤芯", CNT: 1, TYP: "有滤芯"}


def _load(name):
    p = os.path.join(ROOT, "data", "Cat Fountains-US-202608", name)
    if not os.path.exists(p):
        pytest.skip("类目文件未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def rules():
    return _load("cat_fountains.rules.json")


@pytest.fixture(scope="module")
def vision():
    return _load("cat_fountains.vision.json")


def _ro(rules):
    out = {}
    for e in rules["row_overrides"]:
        if e["attr"] == "过滤类型":
            out[(e["asin"], e["when"])] = e["then"]
    return out


# ---------------- A 组: 无滤芯款 ----------------

def test_A_overrides_declared(rules):
    """19 个无滤芯 ASIN 都必须在 rules.json 里有声明式改判(驱动不含类目词)。"""
    ro = _ro(rules)
    got = {a for a, w in ro if a in A_ASINS and w in ("滤芯", "无过滤器")}
    assert got == A_ASINS, "缺少: %s" % sorted(A_ASINS - got)
    for a in A_ASINS:
        w = "无过滤器" if a in ("B041E008C7", "B0166377D9") else "滤芯"
        assert ro[(a, w)] == A_THEN, a


def test_A_target_matches_rule_special(rules):
    """改判目标值必须与规则里 `无需过滤器` 的 special 输出**逐子列一致**，
    否则下次改 special 会出现两处口径打架。"""
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    spec = a["params"]["special"]
    by_name = dict(zip(a["subcols"], spec["无需过滤器"]))
    assert by_name == A_THEN


def test_special_covers_no_filter_synonyms(rules):
    """原始列里「无滤芯」有两种写法(无需过滤器 5 行 / 无过滤器 2 行)。
    后者原先不在 special 里 → 被当材料处理成「有过滤系统(材料已明确)」(审计闸 C2 会报
    token 超域)。已补同名同口径键；本测试锁死三个同义键取值一致。"""
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    spec = a["params"]["special"]
    for k in ("无需过滤器", "无需滤芯", "无过滤器"):
        assert list(spec[k]) == list(spec["无需过滤器"]), k
        cv = apply_rule(a, k, {})[0]
        assert dict(zip(a["subcols"], [cv[c] for c in a["subcols"]])) == A_THEN, k


def test_A_vision_entries_corrected(vision):
    """18 个被视觉覆盖的 ASIN，视觉条目必须已改成与依据一致的无滤芯结论。"""
    in_vis = [a for a in A_ASINS if a in vision]
    assert len(in_vis) == 18, "应恰好 18 个 ASIN 命中视觉文件，实际 %d" % len(in_vis)
    for a in in_vis:
        e = vision[a]
        assert e[TYP] == "无需过滤器", "%s: %r" % (a, e[TYP])
        assert e[MAT] == "不适用" and e[CNT] == "不适用", a
        assert "修正说明" in e, "%s 缺少修正说明(应保留原结论以便追溯)" % a


def test_A_vision_evidence_is_filterless(vision):
    """回归闸: 结论=无需过滤器的条目，其「依据」必须含无滤芯证据字样 ——
    原 bug 正是结论与依据相反(依据 'No Filters' 却写 有滤芯)。"""
    marks = ("no filter", "filter-free", "filterless", "0 filter",
             "zero filter", "no ongoing", "without filter", "no replacement")
    for a in A_ASINS:
        e = vision.get(a)
        if not e:
            continue
        ev = (e.get("依据", "") + e.get("原值", "")).lower()
        assert any(m in ev for m in marks), "%s 依据无无滤芯字样: %r" % (a, e.get("依据"))


# ---------------- B 组: 有滤芯但类型未明确 ----------------

def test_B_overrides_declared(rules):
    ro = _ro(rules)
    for a in B_ASINS:
        assert ro[(a, "未明确")] == B_THEN, a


def test_B_keeps_layer_from_rule(rules):
    """B 组只改 材料/种类数/类型，**不得**顺手改层数（层数由视觉或规则给）。"""
    for a in B_ASINS:
        for (asin, w), then in _ro(rules).items():
            if asin == a:
                assert LAY not in then, a


# ---------------- 覆盖链: 规则 → 改判 → 视觉 ----------------

def test_precedence_chain_filterless(rules, vision):
    """模拟驱动 apply 顺序(规则 → row_overrides → 视觉)，证明最终值是 无需过滤器。
    原 bug 正是最后一步视觉把结论覆盖回 有滤芯。"""
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    ro = _ro(rules)
    for asin, raw in (("B0055FBF45", "滤芯"), ("B0166377D9", "无过滤器")):
        assert asin in vision, asin
        cv = dict(apply_rule(a, raw, {})[0])
        cv.update(ro[(asin, raw)])                    # row_overrides（合并语义）
        cv.update({k: v for k, v in vision[asin].items() if k in a["subcols"]})
        assert cv[TYP] == "无需过滤器", asin
        assert cv[MAT] == "不适用" and cv[CNT] == "不适用", asin


def test_precedence_chain_no_vision_row(rules, vision):
    """未被视觉覆盖的那 1 行(B0BE39C98F)只能靠 row_overrides 生效。"""
    assert "B0BE39C98F" not in vision
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    cv = dict(apply_rule(a, "滤芯", {})[0])
    cv.update(_ro(rules)[("B0BE39C98F", "滤芯")])
    assert cv[TYP] == "无需过滤器"


# ---------------- 跨列语义(用户问题本身) ----------------

def test_two_dimensions_are_orthogonal(rules):
    """`有过滤系统` 与 `是` 不是同一维度 → 不构成冲突，本测试把口径写成断言。
    类型列里 `有过滤系统` 与 `有滤芯` 是**两个不同结构**(材料是否明确)。"""
    a = next(x for x in rules["attributes"] if x["name"] == "过滤类型")
    assert TYP in a["subcols"]
    spec = a["params"]["special"]
    assert spec["无需过滤器"][3] == "无需过滤器"
    assert spec["滤芯"][3] == "有滤芯（材料未明确）"


def test_cartridge_column_removed(rules):
    """用户要求(2026-09-22)：`是否含滤芯` 这一列不要了，直接关注材料。
    该属性已从 rules.json 移除 → 交付表里不再有 `是否含滤芯_标准`。
    ⚠ 源表**原始列**仍在输入表里，跨列守卫改为对源列做（见 verify_filter_consistency.py C3/C4），
    本断言只锁"交付视图不再输出该标准化列"，防止被误加回来。"""
    names = [x["name"] for x in rules["attributes"]]
    assert "是否含滤芯" not in names, "用户已要求移除该属性列"
    subs = [c for x in rules["attributes"] for c in x["subcols"]]
    assert not [c for c in subs if "是否含滤芯" in c], subs
    assert len(names) == 23, "猫饮水机标准化属性数应为 23（原 24）"


def test_conflicting_counts_only_A_and_B(rules):
    """改判集合大小必须正好 27(19+8)，防止误伤正常行。"""
    ro = [e for e in rules["row_overrides"]
          if e["attr"] == "过滤类型" and isinstance(e["then"], dict)]
    hit = [e for e in ro if e["asin"] in A_ASINS or e["asin"] in B_ASINS]
    assert len(hit) == 27, len(hit)
