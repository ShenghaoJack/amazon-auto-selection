# 单元测试: A+ 图层数结论 + 「残缺页」判别口径
#
# 背景(2026-09-22, 用户提供 B0EDF096CD 的 A+ 图):
#   图上逐层标注 High-density Filter Cotton + Sintered Activated Carbon = 2 层（底部托盘不计）。
#   复盘发现该 ASIN 之前的缓存页是**残缺页**(1.4MB, 不含 AplusModule) → A+ 抓到 0 张,
#   看起来像"没有 A+"。重抓后 A+ 39 张, 独立复现 2 层；B080EB8541 同款 A+ 也是 2 层。
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LAY = "过滤层数（营销宣称）_标准"
MAT = "过滤材料_标准"


def _cat():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "Cat Fountains-US-202608")


def test_aplus_layer_file():
    p = os.path.join(_cat(), "cat_fountains.vision_layers_aplus.json")
    if not os.path.exists(p):
        pytest.skip("视觉文件未部署")
    d = json.load(open(p, encoding="utf-8"))
    assert set(d) == {"B0EDF096CD", "B080EB8541"}
    for asin, e in d.items():
        assert e[LAY] == 2, "%s 应为 2 层" % asin
        assert e[MAT] == "过滤棉+活性炭"
        assert e["过滤材料种类数_标准"] == 2
        assert e["过滤类型_标准"] == "有过滤系统（材料已明确）"
        assert "A+" in e["过滤类型_依据"]


def test_aplus_rows_do_not_collide_with_text_overrides():
    """A+ 结论行不应同时也是文本改判行(否则两个来源打架)。"""
    p = os.path.join(_cat(), "cat_fountains.rules.json")
    if not os.path.exists(p):
        pytest.skip("类目规则未部署")
    rules = json.load(open(p, encoding="utf-8"))
    text_asins = {r["asin"] for r in rules["row_overrides"] if r["attr"] == "过滤类型"}
    ap = set(json.load(open(os.path.join(_cat(), "cat_fountains.vision_layers_aplus.json"),
                            encoding="utf-8")))
    assert not (text_asins & ap)


def test_partial_page_detection_rule():
    """残缺页判别口径: 页大小 <1.8MB 或 不含 AplusModule → 需重抓（离线校验已缓存的 4 行）。"""
    hc = os.path.join(_cat(), "htmlcache")
    if not os.path.isdir(hc):
        pytest.skip("无 htmlcache")
    partial = {"B0EDF096CD", "B0EBE87423"}
    for asin in partial:
        p = os.path.join(hc, asin + ".html")
        if not os.path.exists(p):
            continue
        html = open(p, encoding="utf-8", errors="replace").read()
        # 重抓后应为完整页(A+ 模块存在) —— 若仍是残缺页说明重抓未生效
        assert os.path.getsize(p) > 1_800_000 or "AplusModule" in html, \
            "%s 仍是残缺页, A+ 抓取会返回 0" % asin
