# -*- coding: utf-8 -*-
"""单元测试: steps/_browser_ux.py —— 卖家精灵页面"点得准"三件套(2026-09-16)。

背景(用户报障): 步骤11 选月份时 `page.locator("[class*=date] input, …").first.click()` **盲取第一个匹配**,
命中的是视野外元素 → Playwright 把它滚进视野(页面被滚动) + 鼠标悬停顶栏 → **顶栏超级菜单展开盖住整页**,
"立即查询/导出"按钮被挡; 而 `BrowserAutomation.click()` 被遮住时会先等满 **30 秒** 才退回 DOM click。
截图证据: `step11-g1-date.png` / `-filled.png`(顶栏「产品」菜单展开 + 页面已滚动)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from steps import _browser_ux as ux  # noqa: E402


# ------------------------------------------------------------------ 假页面
class FakeMouse:
    def __init__(self):
        self.moves = []

    def move(self, x, y):
        self.moves.append((x, y))


class FakeKeyboard:
    def __init__(self):
        self.presses = []

    def press(self, key):
        self.presses.append(key)


class FakeLocator:
    """一个假候选元素: 可见性/是否在顶栏/是否在弹层/命中判定/点击行为都可控。"""

    def __init__(self, *, visible=True, in_header=False, in_popup=False, hit=True,
                 click_raises=False, dom_click_ok=True, label="el"):
        self.visible = visible
        self.in_header = in_header
        self.in_popup = in_popup
        self.hit = hit
        self.click_raises = click_raises
        self.dom_click_ok = dom_click_ok
        self.label = label
        self.clicks = 0
        self.dom_clicks = 0
        self.scrolls = 0

    # --- Playwright Locator 接口(只实现被测代码用到的部分)
    def is_visible(self):
        return self.visible

    def evaluate(self, script, arg=None):
        if "closest" in script and "[class*=header]" in script:
            return self.in_header
        if "closest" in script and "[class*=dropdown-menu]" in script:
            return self.in_popup
        if "elementFromPoint" in script:
            return self.hit
        if "isContentEditable" in script:          # 可编辑判定
            return True
        if script.strip().endswith("e.click()"):
            self.dom_clicks += 1
            if not self.dom_click_ok:
                raise RuntimeError("dom click failed")
            return None
        return None

    def click(self, timeout=None):
        self.clicks += 1
        if self.click_raises:
            raise TimeoutError("element is not stable / intercepted")

    def scroll_into_view_if_needed(self, timeout=None):
        self.scrolls += 1


class FakeLocatorList:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]


class FakePage:
    def __init__(self, mapping):
        """mapping: {选择器: [FakeLocator, ...]}"""
        self.mapping = mapping
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.viewport_size = {"width": 1280, "height": 900}
        self.asked = []

    def locator(self, sel):
        self.asked.append(sel)
        if sel not in self.mapping:
            raise RuntimeError(f"unknown selector: {sel}")
        return FakeLocatorList(self.mapping[sel])


# ------------------------------------------------------------------ first_visible
def test_first_visible_skips_hidden_and_header_candidates():
    """盲取 .first 的问题: 第一个匹配可能是隐藏的或顶栏里的 —— 必须跳过。"""
    hidden = FakeLocator(visible=False, label="hidden")
    in_nav = FakeLocator(in_header=True, label="nav")
    good = FakeLocator(label="good")
    page = FakePage({"[class*=date] input": [hidden, in_nav, good]})
    assert ux.first_visible(page, "[class*=date] input") is good


def test_first_visible_returns_none_when_no_candidate():
    page = FakePage({"#x": [FakeLocator(visible=False)]})
    assert ux.first_visible(page, "#x") is None
    assert ux.first_visible(page, "#missing") is None, "选择器不认识时应返回 None 而不是抛错"


def test_first_visible_editable_requirement():
    """editable=True 用于 ASIN 输入框; 日期那种只读 el-select 输入框用缺省(不要求可编辑)。"""
    readonly = FakeLocator(label="readonly")
    readonly.evaluate = lambda script, arg=None: False if "isContentEditable" in script else (
        False if "closest" in script else True)
    page = FakePage({"input": [readonly]})
    assert ux.first_visible(page, "input", editable=True) is None, "只读元素不该当可编辑候选"
    assert ux.first_visible(page, "input") is readonly, "缺省不要求可编辑(反查页日期框就是只读的)"


# ------------------------------------------------------------------ park_mouse
def test_park_mouse_moves_away_from_header_and_presses_escape():
    """停鼠标必须落在**内容区**(远离 sticky 顶栏), 并关掉遗留下拉。"""
    page = FakePage({})
    ux.park_mouse(page)
    assert page.mouse.moves, "必须移动鼠标(否则会一直悬停在顶栏上)"
    x, y = page.mouse.moves[-1]
    assert y > 300, f"鼠标停在 y={y}, 离顶栏太近(顶栏悬停会展开超级菜单)"
    assert x < 40, "应停在左边缘空白处(避免触发别的 hover 交互)"
    assert page.keyboard.presses == ["Escape"], "应发一次 Esc 关闭遗留下拉/菜单"


# ------------------------------------------------------------------ click_verified
def test_click_verified_parks_mouse_then_clicks():
    good = FakeLocator()
    page = FakePage({"button": [good]})
    logs = []
    assert ux.click_verified(page, "button", what="立即查询", log=logs.append) is True
    assert good.clicks == 1 and good.dom_clicks == 0, "命中判定通过时应走真鼠标点击"
    assert good.scrolls == 1, "点前应先滚进视野"
    assert page.mouse.moves, "点前应停鼠标(把顶栏 hover 关掉)"


def test_click_verified_retries_then_falls_back_to_dom_click():
    """被遮罩挡住(命中判定为假)时: 先退遮挡重试, 仍不行才 DOM 兜底 —— 不再白等 30 秒。"""
    el = FakeLocator(hit=False)
    page = FakePage({"button": [el]})
    logs = []
    assert ux.click_verified(page, "button", what="导出", tries=2, log=logs.append) is True
    assert el.clicks == 0, "命中判定失败时不该发真鼠标点击"
    assert el.dom_clicks == 1, "应退回 DOM click"
    assert any("兜底" in m for m in logs), f"兜底要有日志: {logs}"
    assert len(page.mouse.moves) >= 2, "每次重试前都要再停一次鼠标(退遮挡)"


def test_click_verified_intercepted_click_falls_back():
    """真点击抛 Timeout(被遮住/不稳定) → 退遮挡重试 → 仍失败则 DOM click。"""
    el = FakeLocator(click_raises=True)
    page = FakePage({"button": [el]})
    assert ux.click_verified(page, "button", tries=2) is True
    assert el.clicks == 2, "两次尝试都应发真点击(每次前都退遮挡)"
    assert el.dom_clicks == 1


def test_click_verified_without_visible_candidate_is_safe():
    """没有可见候选 → 返回 False 且不抛错(调用方可据此重试/报错), 并留下日志。"""
    page = FakePage({"button": [FakeLocator(visible=False)]})
    logs = []
    assert ux.click_verified(page, "button", what="立即查询", log=logs.append) is False
    assert any("未找到可见的立即查询" in m for m in logs)


def test_click_by_text_uses_text_selector():
    el = FakeLocator()
    page = FakePage({"text=导出Excel": [el]})
    assert ux.click_by_text(page, "导出Excel", what="导出Excel", log=None) is True
    assert el.clicks == 1


def test_popup_option_is_clicked_without_park_or_escape():
    """弹层内的目标(下拉选项/弹出菜单项)**不能**停鼠标+Esc —— 那会关掉刚打开的下拉。"""
    opt = FakeLocator(in_popup=True)
    page = FakePage({"[class*=date] input": [opt]})
    assert ux.click_verified(page, "[class*=date] input", what="月份选项") is True
    assert opt.clicks == 1
    assert page.mouse.moves == [], "弹层内目标不该移动鼠标(悬停类菜单会被关掉)"
    assert page.keyboard.presses == [], "弹层内目标不该按 Esc(会关掉所在下拉)"


def test_page_level_target_parks_but_popup_target_does_not():
    """对照: 页面级按钮要退遮挡(停鼠标+Esc), 弹层内的不要。"""
    btn = FakeLocator()
    page = FakePage({"button": [btn]})
    ux.click_verified(page, "button", what="立即查询")
    assert page.mouse.moves and page.keyboard.presses == ["Escape"]
