# 单元测试: 卖家精灵目标月缺失 → 「最近30天」替代(steps/_month_fallback, 步骤11/12 用)
import shutil
import tempfile
from pathlib import Path

from steps._month_fallback import (
    RECENT_MATCHERS,
    display,
    is_recent30,
    rewrite_month_key,
)


def test_is_recent30():
    assert is_recent30("recent30")
    assert is_recent30("RECENT30")
    assert is_recent30("最近30天")
    assert is_recent30(" 最近 30 天 ")
    assert is_recent30("last30")
    assert not is_recent30("2026-08")
    assert not is_recent30("")
    assert not is_recent30(None)


def test_display():
    assert display("recent30") == "最近30天"
    assert display("最近30天") == "最近30天"
    assert display("2026-08") == "2026-08"
    assert display("") == ""


def test_matchers_cover_default_label():
    # 平台默认选项文本「最近30天」必须在匹配列表首位附近(首个命中即点)
    assert "最近30天" in RECENT_MATCHERS


def test_rewrite_month_key_preserves_comments():
    p = Path(__file__).resolve().parent / "_mfb_tmp_config.yaml"
    try:
        text = (
            "# 注释行应保留\n"
            "CAT: sim\n"
            'TRAFFIC_MONTH: "2026-08"   # 目标月\n'
            "COMPETITOR_MONTH: \"2026-08\"\n"
            "TREND_MONTH: \"202608\"\n"
        )
        p.write_text(text, encoding="utf-8")
        assert rewrite_month_key(p, "TRAFFIC_MONTH", "最近30天") is True
        out = p.read_text(encoding="utf-8")
        assert "# 注释行应保留" in out
        assert 'TRAFFIC_MONTH: "最近30天"' in out
        assert out.count("最近30天") == 1
        # COMPETITOR_MONTH 未动
        assert 'COMPETITOR_MONTH: "2026-08"' in out
        # 不存在的键返回 False
        assert rewrite_month_key(p, "NOT_EXIST_KEY", "x") is False
    finally:
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# select_recent30 的"点得准"回归(2026-09-16 用户报障)
#   现象: "在步骤11的查流量的时候, 在文本框中粘贴asin后, 鼠标会移到别的地方, 出现下拉框挡住了页面,
#          导致按钮找不到"。截图证据: step11-g1-site.png 正常 → step11-g1-date.png **顶栏「产品」菜单
#          展开盖住整页 + 页面被滚动** (选月份那一步造成的)。
#   根因: `page.locator(selector).first.click()` **盲取第一个匹配**、不校验可见性 → 命中的是视野外
#          元素, Playwright 先把它滚进视野(页面因此滚动), 鼠标随之悬停 sticky 顶栏 → 菜单展开。
#   修后: 只点**可见且不在顶栏内**的候选, 点前先 park_mouse(挪鼠标 + Esc)。
from steps._month_fallback import select_recent30  # noqa: E402


class _FakeItem:
    def __init__(self, text, *, visible=True):
        self.text = text
        self.visible = visible
        self.clicks = 0

    def is_visible(self):
        return self.visible

    def evaluate(self, script, arg=None):
        if "closest" in script and "[class*=header]" in script:
            return False                  # 选项不在顶栏内
        if "closest" in script and "[class*=dropdown-menu]" in script:
            return True                   # 选项在弹层内 → 不该被 park/Esc 关掉
        if "elementFromPoint" in script:
            return True                   # 命中判定: 点得到
        if script.strip().endswith("e.click()"):
            self.clicks += 1
            return None
        return None

    def click(self, timeout=None):
        self.clicks += 1

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def wait_for(self, state=None, timeout=None):
        """真实代码会先等选项可见(item.wait_for(state='visible'))。"""
        return None

    def input_value(self):
        return "最近30天"


class _FakeHiddenInput(_FakeItem):
    """模拟"第一个匹配但在视野外/顶栏里"的候选 —— 修前会点它(并滚动页面)。"""

    def __init__(self):
        super().__init__("", visible=False)
        self.scrolled = 0

    def scroll_into_view_if_needed(self, timeout=None):
        self.scrolled += 1


class _FakeLoc:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]

    @property
    def first(self):
        return self.items[0] if self.items else None

    def filter(self, has_text=None):
        if has_text is None:
            return self
        return _FakeLoc([it for it in self.items if has_text in getattr(it, "text", "")])

    def wait_for(self, state=None, timeout=None):
        return None

    def all_inner_texts(self):
        return [it.text for it in self.items if it.visible]


class _FakeMouse:
    def __init__(self):
        self.moves = []

    def move(self, x, y):
        self.moves.append((x, y))


class _FakeKeyboard:
    def __init__(self):
        self.presses = []

    def press(self, key):
        self.presses.append(key)


class _FakePage:
    """最小假页面: 日期输入框候选 = [隐藏的(视野外), 可见的], 下拉项 = 「最近30天」。"""

    def __init__(self):
        self.hidden = _FakeHiddenInput()
        self.visible = _FakeItem("", visible=True)
        self.option = _FakeItem("最近30天", visible=True)
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.viewport_size = {"width": 1280, "height": 900}

    def locator(self, sel):
        if "date-select" in sel or "date" in sel:
            return _FakeLoc([self.hidden, self.visible])
        if "el-select-dropdown__item" in sel:
            return _FakeLoc([self.option])
        raise RuntimeError(f"未知选择器: {sel}")


def test_select_recent30_skips_hidden_candidate_and_parks_mouse():
    """必须点**可见**的那个日期框(不再盲取 .first), 且点前先挪鼠标/按 Esc 退遮挡。"""
    page = _FakePage()
    logs: list[str] = []
    val = select_recent30(page, "[class*=date-select] input", log=logs.append)
    assert val == "最近30天"
    assert page.hidden.clicks == 0, "隐藏/视野外的候选绝不能被点(修前就是它把页面滚了、把顶栏菜单带出来)"
    assert page.hidden.scrolled == 0, "也不该把它滚进视野"
    assert page.visible.clicks >= 1, "应点可见的那个日期框"
    assert page.option.clicks == 1, "应点中「最近30天」选项"
    assert page.mouse.moves, "点前必须挪鼠标(离开顶栏)"
    assert "Escape" in page.keyboard.presses
    assert any("最近30天" in m for m in logs)


def test_select_recent30_raises_when_no_visible_input():
    """没有可见候选时给出明确错误(而不是盲点一个看不见的元素)。"""

    class _NoVisible(_FakePage):
        def locator(self, sel):
            if "date" in sel:
                return _FakeLoc([self.hidden])       # 只有隐藏的
            return super().locator(sel)

    try:
        select_recent30(_NoVisible(), "[class*=date-select] input", log=lambda m: None)
    except RuntimeError as exc:
        assert "未找到可见的日期输入框" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应抛 RuntimeError")

def test_select_recent30_calls_wait_hook_after_both_clicks():
    page = _FakePage()
    calls = []
    assert select_recent30(page, "[class*=date-select] input", log=lambda _: None,
                           after_click=lambda: calls.append("wait")) == "最近30天"
    assert calls == ["wait", "wait"]
