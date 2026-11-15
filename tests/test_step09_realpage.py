# 回归测试: 用桌面保存的真实 Amazon 搜索页验证"只收有机结果、排除所有广告位"
# 依赖外部快照文件(缺文件自动跳过, 不影响 CI/离线):
#   C:\\Users\\Jack_New\\Desktop\\Amazon.com _ cat water fountain.html
# 地面真值: 卡标签类含 AdHolder = 广告(2026-09-08 实测该页 12 个, 新布局广告卡
#   也带 data-component-type="s-search-result", 故必须按卡聚合、任一副本带广告标记即排除)
import os
import re

import pytest

from steps.step09_search_collect import _extract_result_items

PAGE = r"C:\Users\Jack_New\Desktop\Amazon.com _ cat water fountain.html"

pytestmark = pytest.mark.skipif(
    not os.path.exists(PAGE), reason="桌面快照 HTML 不存在(仅本机回归)"
)


def _adholder_asins(html: str) -> set:
    clean = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
    clean = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", clean, flags=re.I)
    clean = re.sub(r"<!--[\s\S]*?-->", " ", clean)
    ads = set()
    for m in re.finditer(r'data-component-type="s-search-result"', clean, re.I):
        ts = clean.rfind("<div", 0, m.start())
        gt = clean.find(">", m.start())
        if ts < 0 or gt < 0:
            continue
        tag = clean[ts:gt + 1]
        ma = re.search(r'\bdata-asin="([A-Z0-9]{10})"', tag)
        if ma and "AdHolder" in tag:
            ads.add(ma.group(1))
    return ads


def test_realpage_excludes_all_adholder_ads():
    """真实页: 解析结果与地面真值广告集零交集, 且有机结果非空。"""
    html = open(PAGE, encoding="utf-8", errors="replace").read()
    ads = _adholder_asins(html)
    items = _extract_result_items(html)
    got = {it["asin"] for it in items}
    assert ads, "地面真值未检出广告(页面结构变化?), 本断言前提失效"
    assert got, "未解析到任何有机结果"
    assert got & ads == set(), f"广告 ASIN 混入: {got & ads}"
    assert len(got) >= 20, f"有机结果过少: {len(got)}"


def test_realpage_title_capture_mostly_ok():
    """真实页: 标题快照大部分能取到(剩余空行的 ASIN 至少仍会被收集)。"""
    html = open(PAGE, encoding="utf-8", errors="replace").read()
    items = _extract_result_items(html)
    empty = [it for it in items if not it["title"]]
    assert items
    assert len(empty) < len(items) / 2, f"标题缺失过多: {len(empty)}/{len(items)}"
