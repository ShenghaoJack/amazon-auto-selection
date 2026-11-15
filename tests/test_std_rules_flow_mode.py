# 单元测试: 水流模式_标准 的清洁化(词表外值清零)
#
# 背景(2026-09-21, 用户报「水流模式_标准 中出现多个与水流模式无关的值」):
#   审计出 27 行含词表外 token, 三类根因 ——
#     ① **括号残留**(13 行): `2种水流（已明确：喷泉模式）` → 值变成 `（已明确：喷泉模式`。
#        根因是 `prefix_res` **顺序敏感**: re.sub 顺序执行, 通用正则 `^\d+种(水流|模式)`
#        排在前面先把 `2种水流` 吃掉, 后面那条具体正则成了死代码。
#     ② **近义写法**(10 行): `360度多向水流`、`普通水流模式`、`柔和涌泉模式`、`气泡喷泉`、
#        `柔和瀑布`、`双漩涡水流` 等未进 mode_map。
#     ③ **真的与水流无关**(4 行): `80mL/100mL/120mL`(容量档位)、`2种喷头, 3档水流调节`、
#        `3档可调`、`单向或双向` —— 这些不是出水形态, 应落 `可调（未给模式数）` + 待人工复核。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.std_rules import apply_rule  # noqa: E402

CNT, MODES = "水流模式数_标准", "水流模式_标准"


def _attr():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", "Cat Fountains-US-202608", "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署: %s" % p)
    with open(p, encoding="utf-8") as fh:
        rules = json.load(fh)
    return rules, next(a for a in rules["attributes"] if a["name"] == "水流模式")


def _run(raw):
    _, a = _attr()
    cv, st, mt, iss = apply_rule(a, raw, {})
    return cv[CNT], cv[MODES], st


def test_specific_prefix_regex_comes_first():
    """⚠ prefix_res 顺序敏感: 具体形态必须排在通用形态之前, 否则成死代码。"""
    _, a = _attr()
    pr = a["params"]["prefix_res"]
    specific = [i for i, p in enumerate(pr) if "已明确" in p]
    generic = [i for i, p in enumerate(pr) if "已明确" not in p]
    assert specific, "缺少处理「（已明确：X）」的具体正则"
    assert min(specific) < min(generic), "具体正则必须排在通用正则之前"
    assert a["params"].get("strip_parens") is True


@pytest.mark.parametrize("raw,mode", [
    ("2种水流（已明确：喷泉模式）", "喷泉模式"),
    ("3种水流（已明确：花瓣瀑布模式）", "花瓣瀑布模式"),
    ("2种水流（已明确：柔和气泡模式）", "柔和气泡模式"),
    ("2种水流（已明确：气泡模式）", "气泡模式"),
])
def test_paren_residue_cleared(raw, mode):
    """括号残留必须清零, 且保留原值里的已识别模式。"""
    _n, m, _st = _run(raw)
    assert m == mode
    assert "（" not in m and "）" not in m and "已明确" not in m


@pytest.mark.parametrize("raw,mode", [
    ("360度多向水流", "360°多向水流"),
    ("3种模式：叶片水流模式、涌泉模式、普通水流模式", "叶片水流模式+涌泉模式+标准模式"),
    ("2种模式：流动水模式、柔和涌泉模式", "流动水+涌泉模式"),
    ("气泡喷泉、柔和瀑布", "气泡模式+瀑布模式"),
    ("双漩涡水流", "旋涡水流"),
])
def test_synonyms_mapped_into_vocab(raw, mode):
    _, a = _attr()
    vocab = set(a["params"]["mode_map"].values())
    _n, m, _st = _run(raw)
    assert m == mode
    assert set(m.split("+")) <= vocab, "输出必须全部落在取值域内"


@pytest.mark.parametrize("raw", ["80mL/100mL/120mL", "2种喷头, 3档水流调节", "3档可调", "单向或双向"])
def test_non_mode_values_go_to_review(raw):
    """与水流无关的值(容量档位/调节档数/方向)不得混进模式集。"""
    n, m, st = _run(raw)
    assert m == "未明确"
    assert n == "可调（未给模式数）"
    assert st == "待人工复核—语义无法判断"


def test_new_vocab_values_are_self_mapped():
    """本轮新增的受控值必须自映射(否则会掉出词表)。"""
    _, a = _attr()
    for v in ["平流水模式", "叶片水流模式", "多级水流", "多路径水流", "旋涡水流"]:
        assert a["params"]["mode_map"].get(v) == v


def test_existing_behaviour_unchanged():
    """回归: 原有正常值不受影响。"""
    assert _run("2种水流：水龙头模式、喷泉模式")[:2] == (2, "水龙头模式+喷泉模式")
    assert _run("喷泉模式")[:2] == (1, "喷泉模式")
    assert _run("水流可调")[:2] == ("可调（未给模式数）", "未明确")
    assert _run("未明确")[:2] == ("未明确", "未明确")


def test_source_claimed_count_kept():
    """源表自述「3种水流（已明确：X）」: 数保留宣称值 3, 模式只写已识别的 —— 这是源语义, 不改成 1。"""
    n, m, _ = _run("3种水流（已明确：喷泉模式）")
    assert n == 3 and m == "喷泉模式"
