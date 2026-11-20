# -*- coding: utf-8 -*-
"""卖家精灵页面"点得准"三件套(2026-09-16): 可见候选 + 停鼠标 + 命中判定点击。

**背景(用户报障)**: "在步骤11的查流量的时候, 在文本框中粘贴asin后, 鼠标会移到别的地方,
出现下拉框挡住了页面, 导致按钮找不到"。**截图证据**: `step11-g1-site.png`(选完站点)页面正常,
而 `step11-g1-date.png`(选完月份)里**顶栏「产品」超级菜单展开盖住整页**、并且页面**被滚动了**
(滚动条 + 右下"回到顶部"按钮出现, 工具行滚出视野); `step11-g1-filled.png` 菜单仍盖着 →
底下的"立即查询"按钮被挡。**根因(两处叠加)**:
  ① `_month_fallback.select_recent30()` 用 `page.locator("[class*=date] input, …").first.click()`
     **盲取第一个匹配** —— 没有校验可见性(对比 `_find_asin_input` 是逐候选要求 editable+visible),
     命中的是**视野外**的元素 → Playwright 先把它滚进视野(页面因此被滚动), 鼠标随之移动/悬停,
     而顶栏是 sticky 的、**悬停即展开超级菜单** → 菜单压住整页;
  ② 点按钮走 `core/browser.py::click()`:`locator(sel).first` + 真鼠标点, **被遮住时会先等满 30 秒**
     才退回 DOM click —— 用户看到的"按钮找不到"其实是白等 30 秒/组。
**修法(只给步骤 11/12/13 用, 不动 `core/browser.py` 的全局 click)**: 三个函数 ——
  * `first_visible()`: 只接受**可见**(需要时还要可编辑)且**不在顶栏/导航内**的候选;
  * `park_mouse()`: 把鼠标停到内容区空白处(远离 sticky 顶栏)并按 Esc 关掉遗留下拉/菜单;
  * `click_verified()`: 可见候选 → 停鼠标 → 用 `document.elementFromPoint` 判定"点上去的是不是
    它" → 不是就再退一次遮挡重判(最多 3 次) → 通过后真点(默认 6 秒短超时), 仍失败则 DOM click 兜底。
"""
from __future__ import annotations

import time

# 顶栏/导航容器: 落在其中的元素一律不当候选(sticky 顶栏被 hover 会展开超级菜单盖住页面)
_HEADER_SELECTOR = "header, nav, [class*=header], [class*=navbar], [class*=topbar], [class*=menu-bar]"

# 弹层容器: 目标若在其中(下拉选项/弹出菜单项), **不能**停鼠标+Esc —— 那会把刚打开的下拉关掉。
# 这类元素只做"可见性 + 命中判定 + 真点", 失败就原地重试。
_POPUP_SELECTOR = (".el-select-dropdown, .el-dropdown-menu, .el-popover, .el-autocomplete-suggestion, "
                   "[class*=dropdown-menu], [class*=popover], [class*=popup]")

_PARK_X = 6          # 停鼠标的 x(靠左, 通常无 hover 交互)
_PARK_MARGIN = 60    # 停鼠标的 y = 视口高 - 本值(避开顶栏, 也避开底部悬浮按钮)

_EDITABLE_JS = (
    "e => {const t = e.tagName;"
    " if (t === 'TEXTAREA') return !e.disabled && !e.readOnly;"
    " if (t === 'INPUT') return !e.disabled && !e.readOnly;"
    " return !!e.isContentEditable;}"
)

_HIT_JS = """e => {
    const r = e.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (r.width <= 0 || r.height <= 0) return false;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
    const top = document.elementFromPoint(x, y);
    return !!top && (top === e || e.contains(top) || top.contains(e));
}"""


def _spec_list(spec) -> list:
    """把候选规格统一成选择器列表: 字符串 / 字符串列表。"""
    if isinstance(spec, str):
        return [spec]
    try:
        return [str(s) for s in spec]
    except TypeError:
        return []


def _in_header(el) -> bool:
    try:
        return bool(el.evaluate(f"e => !!(e.closest && e.closest('{_HEADER_SELECTOR}'))"))
    except Exception:  # noqa: BLE001
        return False


def _in_popup(el) -> bool:
    """目标是否位于**弹层**内(下拉选项/弹出菜单项): 这类元素不能"停鼠标 + Esc"(会关掉它所在的下拉)。"""
    try:
        return bool(el.evaluate(f"e => !!(e.closest && e.closest('{_POPUP_SELECTOR}'))"))
    except Exception:  # noqa: BLE001
        return False


def first_visible(page, spec, *, editable: bool = False, skip_header: bool = True,
                  max_scan: int = 12):
    """按顺序返回第一个**可见**(可选: 可编辑)且**不在顶栏内**的候选元素; 没有则 None。

    spec: 选择器字符串(可逗号分隔多个) 或 选择器列表(按优先级依次找)。
    editable=True 时还要求是"可输入且非只读"的元素(如 ASIN 输入框);
    对 el-select 这类**只读**输入框必须用缺省(editable=False), 否则会被跳过。
    """
    for sel in _spec_list(spec):
        try:
            loc = page.locator(sel)
            count = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(min(int(count or 0), max_scan)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                if editable and not el.evaluate(_EDITABLE_JS):
                    continue
                if skip_header and _in_header(el):
                    continue
            except Exception:  # noqa: BLE001
                continue
            return el
    return None


def park_mouse(page, *, escape: bool = True) -> None:
    """把鼠标停到**内容区空白处**(远离 sticky 顶栏)并按 Esc —— 关掉被 hover 打开的菜单/下拉。

    2026-09-16: 这正是用户看到的"鼠标会移到别的地方, 出现下拉框挡住了页面"的对症处理 ——
    自动化点完控件后鼠标常停在顶栏附近(或页面滚动后鼠标相对位置落到顶栏上), 顶栏菜单随即展开。
    """
    try:
        size = page.viewport_size or {}
        h = int(size.get("height") or 800)
        page.mouse.move(_PARK_X, max(120, h - _PARK_MARGIN))
    except Exception:  # noqa: BLE001
        pass
    if escape:
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass


def _hit_ok(el) -> bool:
    """目标中心点最上层元素是不是它自己(或它的子/祖先) —— 被遮罩盖住时返回 False。"""
    try:
        return bool(el.evaluate(_HIT_JS))
    except Exception:  # noqa: BLE001
        return False


def click_verified(page, spec, what: str = "", *, timeout: int = 6000, tries: int = 3,
                   log=None, dom_fallback: bool = True, skip_header: bool = True) -> bool:
    """点一个"确定点得到"的元素。返回是否成功(便于调用方决定是否重试)。

    流程: 可见候选 → 滚进视野 → (退遮挡 + 命中判定) × 最多 tries 次 → 真点(短超时)
          → 全失败则 `el.click()` DOM 兜底(带日志)。
    **退遮挡 = `park_mouse()`(鼠标挪到内容区 + Esc)**, 但对**弹层内**的目标(下拉选项/弹出菜单项)
    自动跳过 —— 否则 Esc 会把刚打开的下拉关掉。`skip_header=False` 用于"选项本身在顶栏下拉里"的场景。
    `what` 只用于日志, 如 "立即查询" / "导出"。
    """
    name = what or "目标控件"
    el = first_visible(page, spec, skip_header=skip_header)
    if el is None:
        if log:
            log(f"未找到可见的{name}(候选: {_spec_list(spec)}); 跳过点击")
        return False
    try:
        el.scroll_into_view_if_needed(timeout=timeout)
    except Exception:  # noqa: BLE001
        pass
    in_popup = _in_popup(el)
    for attempt in range(1, max(1, int(tries)) + 1):
        if not in_popup:
            park_mouse(page)                   # 页面级目标: 每次尝试前都退遮挡(顺带把鼠标挪离顶栏)
        try:
            if _hit_ok(el):
                el.click(timeout=timeout)
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    if dom_fallback:
        try:
            el.evaluate("e => e.click()")
            if log:
                log(f"{name}被遮挡/点不到, 已用 DOM click 兜底")
            return True
        except Exception:  # noqa: BLE001
            pass
    if log:
        log(f"{name}点击失败(候选可见但点不到, 已重试 {tries} 次)")
    return False


def click_by_text(page, text: str, what: str = "", *, timeout: int = 6000, log=None,
                  skip_header: bool = True) -> bool:
    """按文本点(等价旧写法 `page.get_by_text(text, exact=False).first.click()`)。"""
    return click_verified(page, f"text={text}", what=what or f"文本「{text}」",
                          timeout=timeout, log=log, skip_header=skip_header)
def dismiss_sellersprite_promotions(page, log=None) -> bool:
    """Close only a clearly identified live-course promotion, never workflow dialogs."""
    try:
        anchors = page.get_by_text('报名观看', exact=False)
        for i in range(min(anchors.count(), 5)):
            anchor = anchors.nth(i)
            if not anchor.is_visible():
                continue
            ancestors = anchor.locator('xpath=ancestor::*')
            for j in range(ancestors.count() - 1, -1, -1):
                container = ancestors.nth(j)
                text = container.inner_text(timeout=1000)
                if len(text) > 1200 or '报名观看' not in text:
                    continue
                if not any(term in text for term in ('直播亮点', '实战课堂', '直播课程')):
                    continue
                controls = container.locator(
                    '[class*="close" i], [aria-label*="关闭"], [aria-label*="close" i], '
                    '[title*="关闭"], [title*="close" i], img[src*="close" i], '
                    'button:has-text("×"), button:has-text("✕")')
                for k in range(min(controls.count(), 8)):
                    control = controls.nth(k)
                    if not control.is_visible():
                        continue
                    control.click(timeout=1500)
                    if not anchor.is_visible():
                        if log:
                            log('已关闭卖家精灵直播课程推广弹窗')
                        return True
    except Exception:
        # A failed promotional dismissal must not be mistaken for success.
        return False
    return False

