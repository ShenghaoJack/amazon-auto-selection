# 单元测试: 榜单收集(名次范围 + 翻页 + 主列表标记校准; 2026-09-15 用真实页面定论)
# ---------------------------------------------------------------------------
# 真页面实测(用户另存的 UK 榜单页 `Amazon.co.uk Best Sellers_ … Chopping Board Sets.html`,
# 以及随后用浏览器逐页 dump 名次徽章复核):
#   · **第 1 页就是名次 #1..#50(50 个商品)**, 第 2 页是 #51..#88(38 个) → 榜单共 88 个
#   · 但 50 个商品在 HTML 里被拆成**两簇**: #1..#30(31 个标记) + #31..#50(20 个标记),
#     两簇之间隔 86024 字符(懒加载分块) → 旧的"卡片簇"逻辑只取第一簇 = **只抓到 30 个**
#     (这才是"每个榜单只取到 30/32/38 个 ASIN"的真因; 上一版误判为"页面分页")
#   · 名次容器 zg-bdg-text = 51 次(50 个名次 + 1 个模板); 轮播/推荐用
#     `p13n-sc-uncoverable-faceout`(62 次), **不能**当主列表标记
#   · 全文 /dp/ = 64 个; 榜单范围内 = 50 个 → 必须只取带名次徽章的卡
# 因此: ①按**名次徽章**取卡范围(不漏第 31~50 名, 不混轮播) ②等懒加载稳定 ③翻页取第 51 名之后。
import re

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
import pytest

from steps.step00_bestsellers import (
    Step00Bestsellers,
    _canonical_page_url,
    _card_marker_positions,
    _main_list_scope,
    _next_page_url,
    _page_incomplete,
    _set_list_viewport,
)

LIST_URL = "https://www.amazon.co.uk/gp/bestsellers/kitchen/3187097031"
NEXT_URL = ("https://www.amazon.co.uk/Best-Sellers-Home-Kitchen-Chopping-Board-Sets"
            "/zgbs/kitchen/3187097031/ref=zg_bs_pg_2_kitchen?_encoding=UTF8&pg=2")
# 生产代码会把分页链接**规范化**成只带 `pg` 的最小可用地址(见 _canonical_page_url),
# 因此夹具(点击/goto 的落点)也用它, 与真实行为一致。
NEXT_URL_CANON = _canonical_page_url(NEXT_URL)


def _rank_card(asin: str, rank: int) -> str:
    """真实结构的榜单卡: 含名次容器 zg-bdg-text 与卡根 id(gridItemRoot)。"""
    return (f'<div id="gridItemRoot" class="zg-grid-general-faceout">'
            f'<div class="zg-bdg-text">#{rank}</div>'
            f'<a href="/dp/{asin}?ref=zg_bs"><span>item {rank}</span></a></div>')


def _carousel_card(asin: str) -> str:
    """轮播卡: 用的是 p13n-sc-uncoverable-faceout(实测被误当主列表标记的那个 class)。"""
    return (f'<div class="p13n-sc-uncoverable-faceout">'
            f'<a href="/dp/{asin}?ref=carousel"><span>rec</span></a></div>')


def _page(rank_asins: list[tuple[str, int]], recs: list[str], next_href: str = "") -> str:
    cards = "".join(_rank_card(a, r) for a, r in rank_asins)
    carousel = "".join(_carousel_card(a) for a in recs)
    nav = ""
    if next_href:
        nav = (f'<nav aria-label="pagination"><ul class="a-pagination">'
               f'<li class="a-last"><a href="{next_href}">Next page</a></li></ul></nav>')
    return (f'<html><body><h1>Best Sellers in Kitchen</h1>'
            f'<div id="zg-right-col">{cards}</div>'
            f'<div class="a-carousel">相关类目{carousel}</div>{nav}</body></html>')


# ---------------------------------------------------------------- ① 主列表标记校准
def test_main_list_uses_rank_container_not_carousel_class():
    """主列表标记必须用名次容器(gridItemRoot/zg-grid-general-faceout), 不是轮播那个 class。

    实测: 轮播的 `p13n-sc-uncoverable-faceout` 62 次 > 榜单卡 51 次, 用它当标记会把轮播
    切进主列表 —— 这正是"主列表卡=77 / 提取ASIN=38"自相矛盾的根因。
    """
    pos = _card_marker_positions(_page([(f"B{i:09d}", i) for i in range(1, 51)], ["X" * 10]))
    assert len(pos) == 50, f"应只数榜单卡 50 个, 实际 {len(pos)}"


def test_main_list_scope_excludes_carousel_and_returns_50_of_page1():
    """第 1 页: 榜单 50 个商品 + 轮播 6 个 → 只取 50 个, 轮播 ASIN 不混入。"""
    recs = ["R0CAROUSEL", "R1CAROUSEL", "R2CAROUSEL", "R3CAROUSEL", "R4CAROUSEL", "R5CAROUSEL"]
    html = _page([(f"B{i:09d}", i) for i in range(1, 51)], recs)
    scope, cards, strict, cards_all = _main_list_scope(html)
    asins = list(dict.fromkeys(re.findall(r"/dp/([A-Z0-9]{10})", scope)))
    assert strict is True
    assert len(asins) == 50
    assert not any(a.startswith("R") for a in asins)
    assert cards_all == 50, "轮播卡不在标记集合里(标记=名次容器)"


def test_main_list_scope_merges_split_clusters_returns_all_50():
    """**本次报障的真因(回归钉子)**: 真实第 1 页的 50 个商品被拆成两簇。

    实测形状: #1~#30(31 个标记) 与 #31~#50(20 个标记), 中间隔 **86024 字符**的懒加载区段
    —— 超过 `_CLUSTER_GAP_CHARS`(60000) 于是被判成"两个簇", `_pick_main_cluster` 只取第一个
    → 50 个商品只抓到 30 个。按**名次徽章**取卡范围后必须拿全 50 个(轮播仍不得混入)。
    """
    part1 = "".join(_rank_card(f"B{i:09d}", i) for i in range(1, 31))
    part2 = "".join(_rank_card(f"B{i:09d}", i) for i in range(31, 51))
    filler = "<div class='lazy-chunk'>" + ("z" * 86024) + "</div>"
    recs = "".join(_carousel_card(a) for a in ("R0CAROUSEL", "R1CAROUSEL"))
    html = (f"<html><body><h1>Best Sellers in Kitchen</h1>"
            f"<div id='zg-right-col'>{part1}</div>{filler}"
            f"<div id='zg-right-col-b'>{part2}</div>"
            f"<div class='a-carousel'>相关类目{recs}</div></body></html>")
    scope, cards, strict, cards_all = _main_list_scope(html)
    asins = list(dict.fromkeys(re.findall(r"/dp/([A-Z0-9]{10})", scope)))
    assert cards == 50, f"两簇都要算进主列表, 实际卡片数={cards}"
    assert len(asins) == 50, f"应取到 #1~#50 全部 50 个, 实际 {len(asins)}"
    assert not any(a.startswith("R") for a in asins), "轮播 ASIN 不得混入"


def test_main_list_scope_excludes_larger_recommend_cluster():
    """推荐簇**比主列表还多**时也不能选错: 主列表 38 张 + 轮播 39 张(同一套卡片 class)。

    这条守的是"按最靠前的成规模簇挑, 而不是按卡片最多挑"的规则 —— 实测形状正是
    轮播 39 > 主列表 38, 按数量挑就会挑到轮播。
    """
    main = "".join(f'<div class="p13n-sc-uncoverable-faceout" id="gridItemRoot">'
                   f'<span class="zg-bdg-text">#{i}</span>'
                   f'<a href="/dp/M{i:09d}">x</a></div>' for i in range(1, 39))
    filler = "<div>" + ("y" * 70000) + "</div>"
    recs = "".join(f'<div class="p13n-sc-uncoverable-faceout">'
                   f'<a href="/dp/R{i:09d}">y</a></div>' for i in range(1, 40))
    html = f"<html><body><h1>Best Sellers</h1>{main}{filler}<h2>Related</h2>{recs}</body></html>"
    scope, cards, strict, cards_all = _main_list_scope(html)
    asins = list(dict.fromkeys(re.findall(r"/dp/([A-Z0-9]{10})", scope)))
    assert len(asins) == 38, f"应取主列表 38 个, 实际 {len(asins)}"
    assert not any(a.startswith("R") for a in asins)


# ---------------------------------------------------------------- ② 下一页链接
def test_next_page_url_detected():
    """`<li class="a-last"><a href=…>Next page` → 取出链接并反转义 &amp;。"""
    html = (f'<nav aria-label="pagination"><ul class="a-pagination">'
            f'<li class="a-selected"><a href="…pg=1">1</a></li>'
            f'<li class="a-last"><a href="{NEXT_URL.replace("&", "&amp;")}">'
            f'Next page<span>→</span></a></li></ul></nav>')
    got = _next_page_url(html)
    assert "pg=2" in got
    assert "&amp;" not in got, "HTML 实体必须反转义(否则 URL 会带 &amp; 打不开)"


def test_next_page_url_handles_relative_href():
    """**实测踩到的坑**: 真机上拿到的 href 可能是相对路径, 直接 goto 会报
    `Cannot navigate to invalid URL` —— 必须用 base_url 补成绝对地址。"""
    html = ('<ul class="a-pagination"><li class="a-last">'
            '<a href="/Best-Sellers-Home-Kitchen-Chopping-Board-Sets/zgbs/kitchen'
            '/3187097031/ref=zg_bs_pg_2_kitchen?pg=2">Next page</a></li></ul>')
    got = _next_page_url(html)                       # 不给 base: 相对路径 → 拒绝(返回空)
    assert got == "", "相对路径且无 base 时必须拒绝, 不能把半截 URL 丢给 goto"
    got = _next_page_url(html, base_url="https://www.amazon.co.uk/gp/bestsellers/kitchen/3187097031")
    assert got.startswith("https://www.amazon.co.uk/")
    assert got.endswith("pg=2")


def test_next_page_url_rejects_junk():
    """空 href / javascript: / 锚点 / 相对无 base → 一律返回空(宁可不翻页也不报错)。"""
    for href in ("", "#", "javascript:void(0)", "/zgbs/kitchen/3187097031", "   "):
        html = f'<li class="a-last"><a href="{href}">Next page</a></li>'
        assert _next_page_url(html) == "", f"href={href!r} 应被拒绝"


def test_next_page_url_strips_whitespace_in_href():
    """href 跨行带空白时要清掉(否则 goto 同样报 invalid URL)。"""
    html = ('<li class="a-last"><a href="https://www.amazon.co.uk/x'
            '/zgbs/kitchen/1?pg=2\n  ">Next page</a></li>')
    got = _next_page_url(html)
    assert got == "https://www.amazon.co.uk/x/zgbs/kitchen/1?pg=2"


def test_next_page_url_empty_when_no_pagination():
    """没有分页控件(单页榜单)时返回空串, 翻页循环不会启动。"""
    assert _next_page_url(_page([("B000C91625", 1)], [])) == ""


# ---------------------------------------------------------------- ③ 翻页收集(端到端)
class _FakePage:
    def __init__(self, pages: dict):
        self._pages = pages
        self.url = ""
        self.viewport = None
        self.evaluate = lambda js: [5000, 720, 5000]   # 到底
        self.mouse = self

    def set_viewport_size(self, size):
        self.viewport = size

    def wheel(self, x, y):
        return None

    def content(self):
        return self._pages[self.url]


class _FakeBrowser:
    def __init__(self, pages: dict, click_navigates: bool = True):
        self.pages = pages
        self.visited = []
        self.page = None
        self.click_navigates = click_navigates
        self.clicked = []

    def new_page(self):
        self.page = _FakePage(self.pages)
        return self.page

    def goto(self, page, url, timeout=60000):
        page.url = url
        self.visited.append(url)

    def click(self, page, selector, timeout=30000):
        """模拟点"下一页"控件: 默认会跳转(与真实一致), 也可置为"点了但没反应"。"""
        self.clicked.append(selector)
        if self.click_navigates:
            nxt = [u for u in self.pages if u != page.url]
            if nxt:
                page.url = nxt[0]
        return None

    def scroll_to_bottom(self, page, times=5, delay=1.0, **kw):
        return 3

    def screenshot(self, page, path):
        return None

    def close_page(self, page):
        return None


def _ctx(tmp_dir: str) -> StepContext:
    cfg = Config({"CAT": "t", "DATA_ROOT": tmp_dir, "DRY_RUN": True})
    paths = Paths(cfg)
    import os
    os.makedirs(paths.cat_root, exist_ok=True)
    os.makedirs(paths.screenshot_dir(), exist_ok=True)
    return StepContext(cfg, paths)


def test_scrape_list_follows_pagination_and_merges_all_pages(ws_tmp, monkeypatch):
    """第 1 页 #1~#50 + 第 2 页 #51~#88(带 Next page)→ 合并去重得到完整 88 个。

    这是本次报障的复现与验收: 不修范围只会有 30 个; 修好范围 + 翻页必须 88 个。
    """
    page1 = _page([(f"B{i:09d}", i) for i in range(1, 51)], ["R0CAROUSEL"],
                  next_href=NEXT_URL)
    page2 = _page([(f"B{i:09d}", i) for i in range(51, 89)], [])
    browser = _FakeBrowser({LIST_URL: page1, NEXT_URL_CANON: page2})
    step = Step00Bestsellers()
    step._cat = "t"
    logs: list[str] = []
    monkeypatch.setattr(step, "log", lambda c, m: logs.append(str(m)))

    asins = step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs")
    assert asins is not None
    assert len(asins) == 88, f"应合并两页得到 88 个, 实际 {len(asins)}: {sorted(asins)}"
    assert len(set(asins)) == 88, "跨页不得重复"
    assert not any(a.startswith("R") for a in asins), "轮播 ASIN 不得混入"
    # 点击控件能跳转时应走点击路径(不强求 goto); 每页都要读到内容
    assert browser.clicked, f"应点击下一页控件, 实际 clicked={browser.clicked}"
    assert any("翻页收集完成" in m for m in logs), "应打印翻页汇总"


def test_scrape_list_single_page_when_no_pagination(ws_tmp, monkeypatch):
    """没有分页控件时只抓一页(不能因为循环而重复请求同一 URL)。"""
    only = _page([(f"B{i:09d}", i) for i in range(1, 13)], [])
    browser = _FakeBrowser({LIST_URL: only})
    step = Step00Bestsellers()
    step._cat = "t"
    monkeypatch.setattr(step, "log", lambda c, m: None)
    asins = step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs")
    assert asins is not None and len(asins) == 12
    assert browser.visited == [LIST_URL]


@pytest.mark.parametrize("kind", ["bs", "nr"])
def test_verification_page_with_recommendations_is_never_list_data(ws_tmp, monkeypatch, kind):
    from steps.step00_bestsellers import ListVerificationRequired
    challenge = '<html>Check the box to continue <label>I am human</label><a href="/dp/B000C91625">recommended</a></html>'
    browser = _FakeBrowser({LIST_URL: challenge})
    step = Step00Bestsellers()
    monkeypatch.setattr(step, "log", lambda c, m: None)
    with pytest.raises(ListVerificationRequired):
        step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind=kind, attempts=1)


def test_verification_on_second_page_does_not_return_partial_list(ws_tmp, monkeypatch):
    from steps.step00_bestsellers import ListVerificationRequired
    page1 = _page([(f"B{i:09d}", i) for i in range(1, 51)], [], next_href=NEXT_URL)
    blocked = '<html><label>I am human</label><a href="/dp/B000C91625">recommended</a></html>'
    browser = _FakeBrowser({LIST_URL: page1, NEXT_URL_CANON: blocked})
    step = Step00Bestsellers()
    monkeypatch.setattr(step, "log", lambda c, m: None)
    with pytest.raises(ListVerificationRequired):
        step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs", attempts=1)


def test_headed_verification_waits_for_actual_list(ws_tmp, monkeypatch):
    from steps import step00_bestsellers as mod
    challenge = '<html>Check the box to continue <label>I am human</label></html>'
    good = _page([(f"B{i:09d}", i) for i in range(1, 4)], [])
    browser = _FakeBrowser({LIST_URL: challenge})
    step = Step00Bestsellers()
    monkeypatch.setattr(step, "log", lambda c, m: None)
    monkeypatch.setattr(mod.time, "sleep", lambda _: browser.pages.update({LIST_URL: good}))
    assert len(step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs", manual_challenge=True)) == 3


def test_navigation_during_headed_verification_keeps_page_open(ws_tmp, monkeypatch):
    from steps import step00_bestsellers as mod
    challenge = '<html>Check the box to continue <label>I am human</label></html>'
    good = _page([(f"B{i:09d}", i) for i in range(1, 4)], [])
    class NavigatingPage(_FakePage):
        def __init__(self):
            super().__init__({LIST_URL: challenge})
            self.reads = 0
            self.gotos = 0
        def content(self):
            self.reads += 1
            if self.reads in (2, 3):
                raise RuntimeError("Page.content: Unable to retrieve content because the page is navigating and changing the content.")
            return challenge if self.reads == 1 else good
        def wait_for_load_state(self, *args, **kwargs):
            pass
    page = NavigatingPage()
    browser = _FakeBrowserSequence(page)
    step = Step00Bestsellers()
    monkeypatch.setattr(step, "log", lambda c, m: None)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    assert len(step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs", manual_challenge=True)) == 3
    assert browser.visited == [LIST_URL]


def test_page_content_does_not_swallow_non_navigation_errors(monkeypatch):
    from steps.step00_bestsellers import _page_content
    class Closed:
        def content(self):
            raise RuntimeError("Target page has been closed")
    with pytest.raises(RuntimeError, match="closed"):
        _page_content(Closed())


def test_run_reopens_headed_after_closing_headless_and_saves_only_success(ws_tmp, monkeypatch):
    import contextlib
    import json
    from pathlib import Path
    from steps.step00_bestsellers import ListVerificationRequired
    ctx = _ctx(ws_tmp)
    ctx.config.data["LIST_URLS"] = LIST_URL
    modes, opened, seen = [], [], []
    @contextlib.contextmanager
    def browser(_site, headed=False):
        assert not opened
        opened.append(headed)
        modes.append(headed)
        try:
            yield object()
        finally:
            opened.pop()
    monkeypatch.setattr(ctx, "browser", browser)
    step = Step00Bestsellers()
    step._cat = "t"
    monkeypatch.setattr(step, "log", lambda c, m: None)
    monkeypatch.setattr(step, "ensure_amazon_login", lambda c: None)
    monkeypatch.setattr(step, "ensure_sellersprite_login", lambda c: None)
    def scrape(_ctx, _browser, url, kind="bs", manual_challenge=False):
        seen.append((kind, manual_challenge))
        if len(seen) == 1:
            raise ListVerificationRequired("blocked")
        return ["B000C91625"] if kind == "bs" else ["B04A8EAA04"]
    monkeypatch.setattr(step, "_scrape_list", scrape)
    step.run(ctx)
    result = json.loads(Path(ctx.resolve_artifact("t-bestseller-newrelease.json")).read_text(encoding="utf-8"))
    assert modes == [False, True, False]
    assert seen == [("bs", False), ("bs", True), ("nr", False)]
    assert len(result["processed_urls"]) == 2
    assert result["bs"] == ["B000C91625"] and result["nr"] == ["B04A8EAA04"]


def test_headed_verification_timeout_keeps_failed_url_out_of_checkpoint(ws_tmp, monkeypatch):
    import contextlib
    from pathlib import Path
    from steps.step00_bestsellers import ListVerificationRequired
    ctx = _ctx(ws_tmp)
    ctx.config.data["LIST_URLS"] = LIST_URL
    @contextlib.contextmanager
    def browser(_site, headed=False):
        yield object()
    monkeypatch.setattr(ctx, "browser", browser)
    step = Step00Bestsellers()
    step._cat = "t"
    monkeypatch.setattr(step, "log", lambda c, m: None)
    monkeypatch.setattr(step, "ensure_amazon_login", lambda c: None)
    monkeypatch.setattr(step, "ensure_sellersprite_login", lambda c: None)
    def blocked(*args, **kwargs):
        raise ListVerificationRequired("验证未完成")
    monkeypatch.setattr(step, "_scrape_list", blocked)
    with pytest.raises(ListVerificationRequired):
        step.run(ctx)
    assert not Path(ctx.resolve_artifact("t-bestseller-newrelease.json")).exists()


def test_verification_reuses_sellersprite_cdp_browser(ws_tmp, monkeypatch):
    import contextlib
    from steps.step00_bestsellers import ListVerificationRequired
    ctx = _ctx(ws_tmp)
    ctx.config.data.update({"LIST_URLS": LIST_URL, "SS_CDP_URL": "http://127.0.0.1:9222"})
    opened = []
    @contextlib.contextmanager
    def browser(site, headed=False):
        opened.append((site, headed))
        yield object()
    monkeypatch.setattr(ctx, "browser", browser)
    step = Step00Bestsellers()
    step._cat = "t"
    monkeypatch.setattr(step, "log", lambda c, m: None)
    monkeypatch.setattr(step, "ensure_amazon_login", lambda c: None)
    monkeypatch.setattr(step, "ensure_sellersprite_login", lambda c: None)
    calls = []
    def scrape(_ctx, _browser, url, kind="bs", manual_challenge=False):
        calls.append((kind, manual_challenge))
        if len(calls) == 1:
            raise ListVerificationRequired("blocked")
        return ["B000C91625"]
    monkeypatch.setattr(step, "_scrape_list", scrape)
    step.run(ctx)
    assert opened == [("amazon", False), ("sellersprite", True), ("amazon", False)]


def test_scrape_list_stops_when_page2_has_no_new_asins(ws_tmp, monkeypatch):
    """**实测踩到的假象**: 点了"下一页"但页面根本没跳(第 2 页返回同一批 ASIN)。

    上一版会把同一页重复计入合并 → 打印"共 2 页, 去重后 60 个"(实为 30 个),
    看起来翻页成功、其实一直是第 1 页。现要求: 没有**新** ASIN 就停手并如实记录。
    """
    same = _page([(f"B{i:09d}", i) for i in range(1, 51)], ["R0CAROUSEL"],
                 next_href=NEXT_URL)
    # 第 2 页"打开"了但内容与第 1 页完全相同(点击没生效/分页是懒加载)
    browser = _FakeBrowser({LIST_URL: same, NEXT_URL_CANON: same}, click_navigates=False)
    step = Step00Bestsellers()
    step._cat = "t"
    logs: list[str] = []
    monkeypatch.setattr(step, "log", lambda c, m: logs.append(str(m)))

    asins = step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs")
    assert asins is not None
    assert len(asins) == 50, f"不得把同一页重复计入, 实际 {len(asins)}"
    assert any("没有新增 ASIN" in m for m in logs), "应明确记录'没有新增 ASIN'"
    assert not any("共 2 页" in m for m in logs), "未真正翻页时不得宣称共 2 页"


def test_scrape_list_click_not_navigating_falls_back_to_goto(ws_tmp, monkeypatch):
    """点击控件没跳转时, 必须回退到直接导航(而不是默默停在第 1 页)。"""
    p1 = _page([(f"B{i:09d}", i) for i in range(1, 51)], [], next_href=NEXT_URL)
    p2 = _page([(f"B{i:09d}", i) for i in range(51, 89)], [])
    browser = _FakeBrowser({LIST_URL: p1, NEXT_URL_CANON: p2}, click_navigates=False)
    step = Step00Bestsellers()
    step._cat = "t"
    logs: list[str] = []
    monkeypatch.setattr(step, "log", lambda c, m: logs.append(str(m)))

    asins = step._scrape_list(_ctx(ws_tmp), browser, LIST_URL, kind="bs")
    assert asins is not None and len(asins) == 88, f"应回退导航拿到 88 个, 实际 {len(asins)}"
    assert browser.clicked, "应先尝试点击控件"
    assert NEXT_URL_CANON in browser.visited, "点击未跳转后应回退到 goto"
    assert any("URL 未变化" in m for m in logs), "应记录'点击后 URL 未变化'"
    assert any("翻页收集完成: 共 2 页" in m for m in logs)


# ------------------------------------------------- ④ 视口 / 名次缺号自愈(2026-09-15 实测)
def test_set_list_viewport_uses_tall_viewport_and_tolerates_failure():
    """抓榜单前必须把视口放大(实测 720 高只渲染 38 个名次, 2600 高渲染全 50 个)。"""
    page = _FakePage({})
    assert _set_list_viewport(page) is True
    assert page.viewport["height"] >= 2000, f"视口必须够高, 实际 {page.viewport}"

    class _NoViewport:
        def set_viewport_size(self, size):
            raise RuntimeError("CDP 复用窗口不支持")

    assert _set_list_viewport(_NoViewport()) is False, "视口设置失败应返回 False 而不是抛异常"


def test_page_incomplete_rules():
    """渲染不全的判据(实测形状为主): 第 1 页有下一页就必须到 #50; 后续页不满 50 个算假尾页。"""
    r1_38 = list(range(1, 39))            # 实测: 视口矮时第 1 页只渲染 #1~#38
    r1_50 = list(range(1, 51))
    r2_38 = list(range(51, 89))           # 实测: 视口矮时第 2 页 #51~#88(假尾页)
    r2_50 = list(range(51, 101))
    r2_13 = list(range(51, 64))           # 真的尾页: 类目只有 63 个商品

    assert _page_incomplete(r1_38, 1, has_next=True) is True
    assert _page_incomplete(r1_50, 1, has_next=True) is False
    assert _page_incomplete(r1_38, 1, has_next=False) is False, "没有下一页时 38 个就是全部"
    assert _page_incomplete(r2_38, 2, has_next=False) is True, "假尾页: 应再试一次补齐"
    assert _page_incomplete(r2_50, 2, has_next=False) is False
    assert _page_incomplete(r2_13, 2, has_next=False) is True, "真尾页也会试一次(只多一次请求)"
    assert _page_incomplete([], 1, has_next=True) is False, "无名次(非榜单页)→ 不判断"


class _FakePageSequence(_FakePage):
    """同一 URL 的**多次加载**返回不同内容(模拟"重载后渲染补齐")。"""

    def __init__(self, seq: list[str]):
        super().__init__({})
        self._seq = list(seq)
        self.gotos = 0            # goto 次数: 0 = 首次打开, 1 = 重载后的内容

    def content(self):
        return self._seq[min(self.gotos, len(self._seq) - 1)]


class _FakeBrowserSequence(_FakeBrowser):
    def __init__(self, page):
        super().__init__({})
        self._page = page

    def new_page(self):
        return self._page

    def goto(self, page, url, timeout=60000):
        page.url = url
        page.gotos += 1
        self.visited.append(url)


def test_read_list_page_heals_incomplete_rank_render(ws_tmp, monkeypatch):
    """**渲染不全自愈**: 第 1 页只渲染 #1~#38 时, 应放大视口重载补齐到 #1~#50。

    实测: 视口 1280x720 时 Amazon 只渲染 38 个名次(有"下一页"却停在 #38, 说明本页该有
    50 个), 放大到 2600 后渲染全 50 个 —— 不补这一步就会永久少 #39~#50(12 个商品)。
    """
    short = _page([(f"B{i:09d}", i) for i in range(1, 39)], [], next_href=NEXT_URL)
    full = _page([(f"B{i:09d}", i) for i in range(1, 51)], [])
    page = _FakePageSequence([short, full])
    browser = _FakeBrowserSequence(page)
    step = Step00Bestsellers()
    step._cat = "t"
    logs: list[str] = []
    monkeypatch.setattr(step, "log", lambda c, m: logs.append(str(m)))

    out = step._read_list_page(_ctx(ws_tmp), browser, page, LIST_URL, "bs")
    asins = out[-1]
    assert len(asins) == 50, f"重载后应拿全 50 个, 实际 {len(asins)}"
    assert page.viewport["height"] >= 2600, f"重载应放大视口, 实际 {page.viewport}"
    assert any("渲染不全" in m for m in logs), "应记录'判定渲染不全'"
    assert any("重载后名次" in m for m in logs), "应记录重载后的名次范围"


def test_read_list_page_keeps_result_when_no_next_page(ws_tmp, monkeypatch):
    """榜单本来就只有一页(无名次缺号判据)时, **不得**触发重载(避免多余请求)。"""
    only = _page([(f"B{i:09d}", i) for i in range(1, 39)], [])
    page = _FakePageSequence([only])
    browser = _FakeBrowserSequence(page)
    step = Step00Bestsellers()
    step._cat = "t"
    logs: list[str] = []
    monkeypatch.setattr(step, "log", lambda c, m: logs.append(str(m)))

    out = step._read_list_page(_ctx(ws_tmp), browser, page, LIST_URL, "bs")
    assert len(out[-1]) == 38
    assert page.gotos == 0, "无下一页时不应重载"
    assert not any("渲染不全" in m for m in logs)


