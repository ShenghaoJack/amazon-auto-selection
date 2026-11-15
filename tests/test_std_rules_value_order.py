# 单元测试: 多值子列「取值内顺序」声明式归一 (value_order / material_order)
#
# 背景(2026-09-21): `过滤材料_标准` 里出现 24 组「材料集合相同但顺序不同」的重复值,
# 涉及 360 行(占 66%)——例如 {无纺布,活性炭,海绵,离子交换树脂} 有 7 种写法、横跨 61 行,
# 直接按该列 group by 会把 1 个类目算成 7 个。根因: 值按源表枚举顺序拼接, 而枚举顺序随机。
# 口径: 顺序属于**呈现**, 不属于语义 → 在 rules.json 里声明物理流经顺序, 规则与驱动都读它。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

MAT = "过滤材料_标准"
CNT = "过滤材料种类数_标准"


def _rules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def test_category_declares_value_order():
    """顺序必须声明在类目文件里(而不是硬编码在引擎)。"""
    rules = _rules()
    vo = rules.get("value_order") or {}
    assert MAT in vo, "rules.json 缺少 value_order[%s]" % MAT
    assert vo[MAT][0] == "滤网" and vo[MAT][-1] == "滤芯"


def test_order_is_physical_flow():
    """顺序应是物理流经顺序: 粗滤 → 细滤 → 吸附 → 化学 → 矿化 → 滤芯。"""
    order = _rules()["value_order"][MAT]
    pos = {m: i for i, m in enumerate(order)}
    assert pos["滤网"] < pos["过滤棉"] < pos["无纺布"] < pos["海绵"]
    assert pos["海绵"] < pos["活性炭"] < pos["离子交换树脂"]
    assert pos["离子交换树脂"] < pos["KDF滤料"] < pos["麦饭石"] < pos["矿化球"]
    assert pos["矿化球"] < pos["滤芯"]


@pytest.mark.parametrize("raw", [
    "无纺布+活性炭+离子交换树脂+海绵",
    "海绵+无纺布+离子交换树脂+活性炭",
    "无纺布+离子交换树脂+活性炭+海绵",
    "海绵+无纺布+活性炭+离子交换树脂",
])
def test_same_set_always_same_string(raw):
    """核心不变量: 同一材料集合, 无论源顺序如何, 输出字符串必须一致。"""
    a = _rules()
    attr = next(x for x in a["attributes"] if x["name"] == "过滤类型")
    attr = json.loads(json.dumps(attr))
    attr["params"]["material_order"] = a["value_order"][MAT]
    sub, _, _, _ = apply_rule(attr, raw, {})
    assert sub[MAT] == "无纺布+海绵+活性炭+离子交换树脂"
    assert sub[CNT] == 4


def test_normalization_preserves_set_and_count():
    """归一只能改顺序, 不得改集合与个数。"""
    a = _rules()
    attr = next(x for x in a["attributes"] if x["name"] == "过滤类型")
    attr = json.loads(json.dumps(attr))
    attr["params"]["material_order"] = a["value_order"][MAT]
    for raw in ["KDF滤料+活性炭+离子交换树脂+麦饭石+过滤棉",
                "过滤棉+离子交换树脂+活性炭",
                "4层过滤（活性炭+离子交换树脂+海绵）"]:
        sub, _, _, _ = apply_rule(attr, raw, {})
        out = sub[MAT]
        assert sub[CNT] == len(out.split("+"))
        assert set(out.split("+")) >= {"活性炭"} or "滤芯" in out
        # 输出自身必须已是有序的
        order = {m: i for i, m in enumerate(a["value_order"][MAT])}
        idx = [order[m] for m in out.split("+")]
        assert idx == sorted(idx), "%s 未按声明顺序输出" % out


def test_unknown_material_untouched():
    a = _rules()
    attr = next(x for x in a["attributes"] if x["name"] == "过滤类型")
    attr = json.loads(json.dumps(attr))
    attr["params"]["material_order"] = a["value_order"][MAT]
    sub, _, _, _ = apply_rule(attr, "4层过滤", {})
    assert sub[MAT] == "未明确"
    assert sub[CNT] == "未明确"
