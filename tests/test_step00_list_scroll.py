# 单元测试: 榜单抓取完整性(2026-09-15 用户报障)
# ---------------------------------------------------------------------------
# 现象: UK 榜单每页本应 50 个商品, 实际"每榜只抓到 32 个 ASIN", 日志无任何告警。
# 根因(两处叠加):
#   ① `BrowserAutomation.scroll_to_bottom` 原来只滚**固定 5 次** × 8000px ≈ 4 万像素;
#      一页 50 个商品的懒加载页面高约 6~8 万像素 → 滚到 3 万多像素就停了, 后 20 个卡片没渲染;
#   ② `_extract_asins` 全文扫 `/dp/` 链接, 既会把推荐/轮播模块的 ASIN 混进来, 也无法
#      判断"主列表到底渲染了几条", 于是"抓漏了"这件事完全没有信号。
# 修复: 滚动改为"滚到底或高度不再增长"; 提取改为优先只在主列表卡片范围内取, 并打印
#      "主列表卡数/是否到底/是否回退全文"三项自查信息。
import re

import core.browser as browser_mod
from core.browser import BrowserAutomation
from steps.step00_bestsellers import _main_list_scope, _page_bottom_state


# ---------------------------------------------------------------- ① 滚动到底
class _FakePage:
    """模拟懒加载页面: 每滚一次高度增长 `grow`, 到 `total` 个卡片后不再增长。"""

    def __init__(self, heights: list[int]):
        self._heights = list(heights)          # 每次滚动后应报告的 scrollHeight
        self.scrolls = 0
        self.wheels = 0

    @property
    def mouse(self):
        return self

    def wheel(self, x, y):
        self.wheels += 1

    def evaluate(self, js):
        self.scrolls += 1
        idx = min(self.scrolls - 1, len(self._heights) - 1)
        return self._heights[idx]


def test_scroll_to_bottom_keeps_scrolling_while_page_grows(monkeypatch):
    """页面持续变高时必须继续滚 —— 原实现固定 5 次, 这正是"只抓到 32 个"的原因。"""
    monkeypatch.setattr(browser_mod.time, "sleep", lambda *_: None)
    # 5 次滚动后还在长高(第 6~9 次才到底): 固定 5 次的旧实现会在这里停下
    page = _FakePage([20000, 40000, 60000, 80000, 100000, 120000, 140000, 140000, 140000])
    b = BrowserAutomation.__new__(BrowserAutomation)
    b.dry_run = False
    b._context = None
    used = b.scroll_to_bottom(page, times=5, delay=0.0)
    assert page.wheels > 5, "页面还在长高就必须继续滚动(旧实现只滚 5 次)"
    assert used >= 8, f"应滚到高度不再增长为止, 实际 {used} 次"


def test_scroll_to_bottom_stops_when_height_stable(monkeypatch):
    """高度稳定后立刻收敛, 不做无谓滚动(正常页面不应白白等几十次)。"""
    monkeypatch.setattr(browser_mod.time, "sleep", lambda *_: None)
    page = _FakePage([50000, 50000, 50000, 50000, 50000, 50000])
    b = BrowserAutomation.__new__(BrowserAutomation)
    b.dry_run = False
    b._context = None
    used = b.scroll_to_bottom(page, times=2, delay=0.0)
    assert 3 <= used <= 6, f"稳定后应及时停止, 实际 {used} 次"


def test_scroll_to_bottom_dry_run_is_noop():
    """dry-run 不碰浏览器, 返回 0。"""
    b = BrowserAutomation.__new__(BrowserAutomation)
    b.dry_run = True
    b._context = None
    assert b.scroll_to_bottom(_FakePage([1, 2]), times=5, delay=0.0) == 0


# ---------------------------------------------------------------- ② 是否到底
def test_page_bottom_state_detects_reached():
    """scrollY + 视口 >= 页高 → 到底。"""
    class P:
        def evaluate(self, _js):
            return [60000, 800, 60800]          # 距底 0
    assert _page_bottom_state(P())[0] is True


def test_page_bottom_state_detects_not_reached():
    """还有 2 万像素没滚完 → 未到底(要把这个信号打进日志)。"""
    class P:
        def evaluate(self, _js):
            return [40000, 800, 60800]          # 距底 20000
    at_bottom, view = _page_bottom_state(P())
    assert at_bottom is False
    assert "距底=20000px" in view


# ---------------------------------------------------------------- ③ 只取主列表
def _card(asin: str, idx: int) -> str:
    return (f'<div class="p13n-sc-uncoverable-faceout" id="gridItemRoot">'
            f'<a href="/dp/{asin}?ref=x"><span>#{idx} item</span></a></div>')


def test_main_list_scope_excludes_recommendation_modules():
    """主列表 50 张卡 + 轮播/推荐模块: 严格模式下只统计主列表, 推荐模块的 ASIN 不进清单。"""
    cards = "".join(_card(f"B{i:09d}", i + 1) for i in range(50))        # 50 个主列表卡
    recommend = ('<div class="a-carousel">相关类目推荐'
                 '<a href="/dp/B0RECOMEND1">rec1</a>'
                 '<a href="/dp/B0RECOMEND2">rec2</a></div>')
    html = f"<html><body><h1>Best Sellers in Kitchen</h1>{cards}{recommend}</body></html>"
    scope, cards_n, strict, cards_all = _main_list_scope(html)
    assert strict is True
    asins = list(dict.fromkeys(re.findall(r"/dp/([A-Z0-9]{10})", scope)))
    assert len(asins) == 50, "主列表 50 个 ASIN 必须全部取到"
    assert not any(a.startswith("B0RECOM") for a in asins), "推荐模块的 ASIN 不得混入"
    assert cards_n >= 50


def test_main_list_scope_picks_main_cluster_not_recommend_carousels():
    """复现 UK 实况(2026-09-15 第二次): 主列表 38 张卡 + 若干推荐轮播共用同一套 class。

    日志里"主列表卡=77 / 提取ASIN=38"就是这么来的 —— 轮播被一起框进来了。
    按"卡片簇"取最大簇后, 应只认主列表那 38 张, 轮播的 ASIN 不得混入。
    """
    main = "".join(_card(f"M{i:09d}", i + 1) for i in range(38))
    # 主列表与轮播之间隔一大段其它 HTML(超过 _CLUSTER_GAP_CHARS), 形成两个簇
    filler = "<div>" + ("x" * 70000) + "</div>"
    carousel = "".join(_card(f"R{i:09d}", i + 1) for i in range(39))
    html = (f"<html><body><h1>Best Sellers in Kitchen</h1>{main}"
            f"{filler}<h2>Related categories</h2>{carousel}</body></html>")
    scope, cards_n, strict, cards_all = _main_list_scope(html)
    assert strict is True
    assert cards_all == 77, "页面卡片标记总数应为 38+39=77(与实测一致)"
    assert cards_n == 38, f"主列表簇应为 38 张, 实际 {cards_n}"
    asins = list(dict.fromkeys(re.findall(r"/dp/([A-Z0-9]{10})", scope)))
    assert len(asins) == 38
    assert not any(a.startswith("R0") for a in asins), "轮播簇的 ASIN 不得混入主列表"


def test_main_list_scope_falls_back_when_container_missing():
    """结构漂移(认不出卡片容器)时回退全文, 且 strict=False 以便调用方告警。"""
    html = '<html><body><a href="/dp/B062592129">x</a></body></html>'
    scope, cards_n, strict, cards_all = _main_list_scope(html)
    assert strict is False and cards_n == 0 and cards_all == 0
    assert "/dp/B062592129" in scope          # 回退=保留全文


def test_mid_scroll_extraction_would_undershoot():
    """复现报障的本质: 只渲染了 32 张卡时, 无论怎么解析都只能得到 32 个 ASIN。

    这条固化"为什么必须滚动到底" —— 若哪天有人把滚动改回固定次数, 本测试与
    test_scroll_to_bottom_keeps_scrolling_while_page_grows 会一起拦下来。
    """
    partial = "".join(_card(f"B{i:09d}", i + 1) for i in range(32))      # 只渲染 32 个
    scope, cards_n, strict, cards_all = _main_list_scope(f"<html><body>{partial}</body></html>")
    assert strict is True
    assert len(set(re.findall(r"/dp/([A-Z0-9]{10})", scope))) == 32      # 与实测的 32 一致
