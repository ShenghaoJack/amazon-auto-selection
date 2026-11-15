# -*- coding: utf-8 -*-
"""「仅注入词」独立保存 —— 行为测试（2026-10-12 用户口径）。

用户三问，逐条对应：
  ① **「保存并应用」会不会干扰仅注入词？** -> A 组（前端提交面）+ C 组（落盘互不覆盖）
  ② **输入格式对不对？** -> C 组（分隔符/大小写/重复/空白 的归一）
  ③ **输出格式对不对？** -> C 组（分区头逐字、与核心去重、两空删文件、API 返回结构）

⚠ 为什么必须新开一个文件：原有 `test_inject_only_manual_entry_is_wired_end_to_end` 只断言
   `'list: "inject"' in html` —— **改前改后都成立**（改前在 `saveKwMode` 里、改后在
   `saveKwInject` 里），所以它**测不出**"保存并应用是否还在碰仅注入词"。A 组把这一点钉死。

另：B 组是**今天顺手修掉的一个真 bug** 的回归 —— `manual_first()` 原来对 core 用 `read_words()`
（不分分区），只带 `[仅注入]` 的文件会让**步骤6 趋势**把仅注入词当成核心词，而**步骤9** 走
`resolve()` 用的是自动核心词 -> **两步的词不一致**。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.keyword_lists import (  # noqa: E402
    MODE_AUTO, MODE_MANUAL, MODE_STOP, MAX_INJECT, NOINJECT_SECTION, core_manual_name,
    manual_first, read_core_sections, read_core_sections3, resolve, set_list, set_stop,
    stop_name, write_core_sections, write_core_sections3,
)

CAT = "Inject Only Cat-US-202610"
HTML = (ROOT / "webui" / "index.html").read_text(encoding="utf-8", errors="replace")


def _src(fn: str) -> str:
    """取 index.html 里某个函数的函数体（到**最近**的下一个顶层 function 为止）。

    ⚠ 必须取两个候选的**最小值**：只找 `\\nfunction ` 会漏掉 `\\nasync function `，
      于是边界跑到**更后面**的函数去 —— 函数体被多框进来一大段，
      断言就会在错误的文本上做判断（我第一版就栽在这，`saveKwMode` 被算进了
      `saveKwInject` 的体内，测试假失败）。
    """
    i = HTML.find(fn)
    if i < 0:
        return ""
    start = i + len(fn)
    cands = [k for k in (HTML.find("\nfunction ", start),
                         HTML.find("\nasync function ", start)) if k > 0]
    return HTML[i:min(cands)] if cands else HTML[i:start + 3000]


def _no_comments(s: str) -> str:
    """去掉 JS 的 `//` 行注释与 `/* */` 块注释。

    ⚠ **必须去注释**再做"不许出现 X"这类断言 —— 否则**解释性注释里提到 X** 就会假失败。
      （我今天在这上面栽了两次：一次是关卡里的 `SS_ASIN_PREFIX`，一次就是这里。）
      正向断言（必须含 X）用不用都行；**反向断言一律先去掉注释**。
    """
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def _d(ws_tmp) -> Path:
    d = Path(ws_tmp) / "data" / CAT
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================ A. 「保存并应用」不碰仅注入词
class TestNoInterferenceFromSaveAndApply:
    def test_save_kw_mode_does_not_submit_inject(self):
        """★★ 核心回归：「保存并应用」的提交里**不能**再有 `list: "inject"`。

        改之前它在三个分支里都调 `saveInject()`，于是"改了仅注入词、顺手点保存并应用"
        -> 仅注入的改动**不会被保存**，而 `__kwDirty` 是共享标志 -> "未保存"提示被清掉
        -> **静默丢失**。这条一旦被改回去，这里立刻红。
        """
        body = _src("async function saveKwMode()")
        assert body, "找不到 saveKwMode 函数体"
        assert 'list: "inject"' not in body, "「保存并应用」又去提交仅注入词了"
        assert "saveInject" not in body, "旧的 saveInject 内联函数不应再出现在 saveKwMode"

    def test_independent_save_function_exists_and_submits_inject(self):
        """仅注入词有自己的保存函数，且**只**提交 inject。"""
        body = _src("async function saveKwInject()")
        assert body, "找不到 saveKwInject"
        assert 'list: "inject"' in body
        assert 'list: "core"' not in body and 'list: "longtail"' not in body, \
            "独立保存不该顺带提交核心词/长尾词"

    def test_independent_button_exists_and_is_wired(self):
        assert 'id="btnKwInjectSave"' in HTML, "面板上没有「保存仅注入词」按钮"
        assert '$("btnKwInjectSave").onclick = () => saveKwInject();' in HTML, "按钮没接线"

    def test_unsaved_inject_is_warned_not_silent(self):
        """⚠ `__kwDirty` 是共享标志 -> 必须有**显式告警**，不能静默丢改动。"""
        body = _src("async function saveKwMode()")
        assert "injChanged" in body, "缺少「仅注入词未保存」的比对"
        assert "未保存" in body, "缺少告警文案"

    def test_guard_is_not_vacuous(self):
        """★★ **反向验证：证明上面那条守护不是空壳**（未过检 ≠ 通过）。

        做法：把 `list: "inject"` **塞回** `saveKwMode` 的体内造一个"突变体"，用**与正式断言
        完全相同的判据**去判 —— 必须判为 False。否则说明判据（或 `_src` 的边界）有问题，
        正式断言只是"恰好通过"，等于没测。
        """
        guard = lambda b: ('list: "inject"' not in b)          # noqa: E731 — 与正式断言同判据
        body = _src("async function saveKwMode()")
        assert guard(body) is True, "正式体里出现了 inject"

        anchor = "const mode = currentKwMode();"
        assert anchor in body, "锚点变了，这条突变测试需要跟着改"
        mutated = body.replace(anchor, anchor + '\n  void { list: "inject" };')
        assert mutated != body, "突变没生效"
        assert guard(mutated) is False, "★ 突变体也通过了 —— 守护是空壳！"

    def test_panel_stays_in_same_module_but_says_save_is_separate(self):
        """用户口径：**仍在同一个模块**里，但保存分开 —— 界面上要说清楚。"""
        assert "仅注入词" in HTML and "btnKwInjectSave" in HTML
        assert "保存是分开的" in HTML or "保存仅注入词" in HTML


# ============================================================ B. manual_first 分区感知（今天的 bug）
class TestManualFirstIsSectionAware:
    def test_inject_only_file_does_not_leak_into_core(self, ws_tmp):
        """★ `[仅注入]`-only 文件 -> `manual_first(kind="core")` 必须回落到**自动**词。

        修复前它用 `read_words()`（不分分区）-> 把仅注入词当成人工核心词 -> 与步骤9 不一致。
        """
        d = _d(ws_tmp)
        write_core_sections(d / core_manual_name(CAT), [], ["只有注入", "仅此一词"])
        core, inject = read_core_sections(d / core_manual_name(CAT))
        assert core == [] and inject == ["只有注入", "仅此一词"]        # 文件本身没错
        words, src = manual_first(d, CAT, "core", ["auto1", "auto2"])
        assert words == ["auto1", "auto2"], "仅注入词泄漏进核心词了"
        assert src == "自动"

    def test_core_and_trend_agree(self, ws_tmp):
        """★ 步骤9(`resolve`) 与 步骤6(`manual_first`) 必须拿到**同一批核心词**。"""
        d = _d(ws_tmp)
        write_core_sections(d / core_manual_name(CAT), [], ["注入一", "注入二"])
        plan = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[])
        words, _ = manual_first(d, CAT, "core", ["auto1", "auto2"])
        assert list(plan.core) == list(words) == ["auto1", "auto2"]
        assert plan.inject_only == ["注入一", "注入二"]
        assert plan.inject_words == ["auto1", "auto2", "注入一", "注入二"]

    def test_legacy_plain_list_still_counts_as_core(self, ws_tmp):
        """向后兼容：无分区头的旧纯词表 -> 全部算核心大词（不能被这次修复改坏）。"""
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        p.write_text("old one\nold two\n", encoding="utf-8")
        words, src = manual_first(d, CAT, "core", ["auto1"])
        assert words == ["old one", "old two"] and src == "人工清单"

    def test_manual_core_still_wins(self, ws_tmp):
        """有 `[核心大词]` 时，人工清单**仍然**优先（修复不能把这条弄丢）。"""
        d = _d(ws_tmp)
        write_core_sections(d / core_manual_name(CAT), ["人工核心"], ["注入"])
        words, src = manual_first(d, CAT, "core", ["auto1"])
        assert words == ["人工核心"] and src == "人工清单"


# ============================================================ C. 输入 / 输出格式
class TestInputOutputFormat:
    # ---- 输入 ----
    def test_input_normalized_lowercase_dedup_strip(self, ws_tmp):
        """输入：大写 / 前后空白 / 重复 / 空串 -> 小写、去重、保序、去空。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "inject", "manual",
                 ["  Cutting Board ", "CUTTING BOARD", "cutting board", "", "  ", "Steel"])
        _core, inject = read_core_sections(d / core_manual_name(CAT))
        assert inject == ["cutting board", "steel"]

    def test_input_separators_split_in_frontend(self):
        """输入：面板的手填框按**逗号/换行/中文逗号**切分（在前端切，后端收数组）。"""
        body = _src("function addKwWords")
        assert body, "找不到 addKwWords"
        assert "split(" in body, "手填输入没有切分逻辑"
        for sep in (",", "，", "\\n"):
            assert sep in body, "缺少分隔符 %r 的处理" % sep

    # ---- 输出 ----
    def test_output_file_has_both_headers_in_order(self, ws_tmp):
        """输出：文件里 `[核心大词]` 在前、`[仅注入]` 在后，逐字匹配。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "core", "manual", ["c1", "c2"])
        set_list(d, CAT, "inject", "manual", ["i1", "i2"])
        txt = (d / core_manual_name(CAT)).read_text(encoding="utf-8")
        assert txt == "[核心大词]\nc1\nc2\n[仅注入]\ni1\ni2\n", repr(txt)
        assert txt.index("[核心大词]") < txt.index("[仅注入]")

    def test_output_drops_inject_duplicating_core(self, ws_tmp):
        """输出：与核心词重复的仅注入词被剔除（避免同一词被算两次）。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "core", "manual", ["dup"])
        r = set_list(d, CAT, "inject", "manual", ["dup", "unique"])
        assert r["inject_saved"] == ["unique"]
        _core, inject = read_core_sections(d / core_manual_name(CAT))
        assert inject == ["unique"]

    def test_output_omits_inject_header_when_empty(self, ws_tmp):
        """输出：仅注入为空时**不写** `[仅注入]` 头（不留空壳分区）。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "core", "manual", ["c1"])
        txt = (d / core_manual_name(CAT)).read_text(encoding="utf-8")
        assert txt == "[核心大词]\nc1\n" and "[仅注入]" not in txt

    def test_both_empty_deletes_file(self, ws_tmp):
        """输出：两个分区都空 -> 删文件（回自动态）。"""
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        set_list(d, CAT, "core", "manual", ["c1"])
        set_list(d, CAT, "inject", "manual", ["i1"])
        assert p.is_file()
        set_list(d, CAT, "inject", "auto")          # 清仅注入 -> 核心还在 -> 文件留
        assert p.is_file()
        set_list(d, CAT, "core", "auto")            # 再清核心 -> 两空 -> 删
        assert not p.is_file(), "两分区都空时应删除文件（回自动态）"

    # ---- API 返回结构 ----
    def test_api_response_shape(self, ws_tmp):
        """输出：保存接口返回 `ok/mode/file/saved/core_saved/inject_saved`。"""
        d = _d(ws_tmp)
        r = set_list(d, CAT, "inject", "manual", ["x1", "x2"])
        assert r["ok"] is True
        assert r["mode"] == "manual"
        assert r["file"] == core_manual_name(CAT)
        assert r["saved"] == ["x1", "x2"]           # saved = 本次保存的那一份
        assert r["inject_saved"] == ["x1", "x2"]
        assert r["core_saved"] == []                # 核心仍为空（走自动），没被顺手写脏

    def test_over_limit_returns_error_not_partial_write(self, ws_tmp):
        """输入超限 -> 明确报错、**不写半份**（误粘贴保护）。"""
        d = _d(ws_tmp)
        r = set_list(d, CAT, "inject", "manual",
                     ["w%d" % i for i in range(MAX_INJECT + 1)])
        assert r["ok"] is False and "最多" in r["error"]
        assert not (d / core_manual_name(CAT)).is_file(), "报错时不该留下文件"


# ============================================================ D. 端到端：两个按钮互不覆盖
class TestEndToEndMutualPreservation:
    def test_apply_then_inject_then_apply(self, ws_tmp):
        """跑一遍真实次序：保存并应用 -> 保存仅注入 -> 再保存并应用，两边都不丢。"""
        d = _d(ws_tmp)
        # ① 保存并应用 = core + longtail(+stop off)，**不碰** inject
        set_list(d, CAT, "core", "manual", ["c1", "c2"])
        set_list(d, CAT, "longtail", "manual", ["t1"])
        # ② 保存仅注入
        set_list(d, CAT, "inject", "manual", ["i1", "i2"])
        # ③ 再点一次保存并应用（切自动口径：清核心+长尾）
        set_list(d, CAT, "core", "auto")
        set_list(d, CAT, "longtail", "auto")
        core, inject = read_core_sections(d / core_manual_name(CAT))
        assert core == [], "切回自动应清掉核心分区"
        assert inject == ["i1", "i2"], "仅注入词被「保存并应用」弄丢了！"

    def test_inject_kept_when_stop_flag_used(self, ws_tmp):
        """停止态也不影响仅注入词（停止只管步骤9 抓取）。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "inject", "manual", ["i1"])
        (d / ("filter-%s-keyword-stop.txt" % CAT)).write_text("", encoding="utf-8")
        core, inject = read_core_sections(d / core_manual_name(CAT))
        assert inject == ["i1"]

    def test_pool_excludes_words_already_core(self):
        """★ 仅注入词池**刻意**滤掉"已是核心大词"的词 —— 这是设计，不是 bug。

        理由两条：① 核心大词**本来就在** `inject_words` 里（= 核心 + 仅注入，会去重）→ 冗余；
                 ② 自动态下核心词**照样被步骤9 抓取** → 加进仅注入**达不到**"只判定不抓"。
        """
        assert "const injected = new Set(" in HTML
        assert "filter(c => !injected.has(c.word))" in HTML, "池子没做核心词过滤"

    def test_empty_pool_message_is_not_misleading(self):
        """★ 池子为空时的文案**不能**写"已全部注入"（误导）。

        真实意思是"候选都已经是核心大词了"；而且必须给出**正确出路**：
        想"只判定不抓"应从核心词清单移除并保存，然后加到仅注入。
        """
        body = _no_comments(_src("function renderKwInjectPool()"))
        assert "候选词都已经是" in body or "核心大词" in body
        assert "已全部注入" not in body, "又写回误导文案了"
        assert "核心大词清单中移除并保存" in body, "没有告诉用户正确出路"

    def test_manual_entry_blocks_core_words(self):
        """★ 手填框也必须挡住"已是核心大词"的词 —— 否则保存时会被**静默剔除**。"""
        body = _src("function addKwWords(kind)")
        assert 'kind === "inject"' in body, "手填没针对 inject 做检查"
        assert "coreSet" in body and "dup" in body, "没做核心词去重检查"
        assert "已是核心大词" in body, "没有说明原因"

    def test_manual_entry_guard_is_not_vacuous(self):
        """突变验证：去掉 inject 判定，判据必须为 False。"""
        guard = lambda b: ('kind === "inject"' in b and "coreSet" in b   # noqa: E731
                           and "dup" in b)
        body = _src("function addKwWords(kind)")
        assert guard(body) is True
        mutated = body.replace('kind === "inject"', 'kind === "XXXX"')
        assert mutated != body and guard(mutated) is False

    def test_step9_words_exclude_inject_even_after_saves(self, ws_tmp):
        """★ 最终语义：无论怎么存，仅注入词都**不进步骤9 的抓取词**。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "core", "manual", ["core one"])
        set_list(d, CAT, "inject", "manual", ["inject only"])
        plan = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[])
        assert plan.core == ["core one"]                       # 步骤9 抓的
        assert plan.inject_only == ["inject only"]             # 只判定、不抓
        assert "inject only" not in plan.core
        assert plan.inject_words == ["core one", "inject only"]  # 步骤10 判定的全部


# ============================================================ E. 注入词不受三态影响（用户口径）
class TestInjectUnaffectedByThreeStateMode:
    """用户口径（2026-10-12）：**注入词不受「自动 / 人工清单 / 人工停止」选择的影响。**

    三态只决定「核心大词从哪来」（自动口径 vs 人工清单）与「步骤9 抓不抓」，
    与 `[仅注入]` 分区无关 —— 它在三种态下都必须照常进步骤10 的判定词。
    """

    @staticmethod
    def _prep(ws_tmp) -> Path:
        d = _d(ws_tmp)
        set_list(d, CAT, "inject", "manual", ["注入甲", "注入乙"])
        return d

    def test_auto_mode(self, ws_tmp):
        """ ① 自动态（无人工清单）：仅注入词照常生效。"""
        d = self._prep(ws_tmp)
        plan = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[])
        assert plan.mode == MODE_AUTO
        assert plan.inject_only == ["注入甲", "注入乙"]
        assert plan.inject_words == ["auto1", "auto2", "注入甲", "注入乙"]

    def test_manual_mode(self, ws_tmp):
        """ ② 人工清单态：核心词换成人工的，**仅注入词不动**。"""
        d = self._prep(ws_tmp)
        set_list(d, CAT, "core", "manual", ["人工核心"])
        plan = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[])
        assert plan.mode == MODE_MANUAL
        assert plan.core == ["人工核心"]
        assert plan.inject_only == ["注入甲", "注入乙"], "人工清单击穿了仅注入分区"
        assert plan.inject_words == ["人工核心", "注入甲", "注入乙"]

    def test_stopped_mode(self, ws_tmp):
        """ ③ 人工停止态：步骤9 不抓，但**仅注入词仍然在**（步骤10 判定照旧）。"""
        d = self._prep(ws_tmp)
        set_stop(d, CAT, True)
        assert (d / stop_name(CAT)).is_file()
        plan = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[])
        assert plan.mode == MODE_STOP and plan.stopped is True
        assert plan.inject_only == ["注入甲", "注入乙"], "人工停止把仅注入词弄丢了"
        assert plan.inject_words == ["auto1", "注入甲", "注入乙"]

    def test_mode_switches_never_touch_inject_section(self, ws_tmp):
        """★ 三态来回切（自动↔人工↔停止），`[仅注入]` 分区逐字不变。"""
        d = self._prep(ws_tmp)
        p = d / core_manual_name(CAT)
        steps = [
            ("core manual", lambda: set_list(d, CAT, "core", "manual", ["c1", "c2"])),
            ("core auto", lambda: set_list(d, CAT, "core", "auto")),
            ("stop on", lambda: set_stop(d, CAT, True)),
            ("stop off", lambda: set_stop(d, CAT, False)),
            ("core manual again", lambda: set_list(d, CAT, "core", "manual", ["c3"])),
            ("core auto again", lambda: set_list(d, CAT, "core", "auto")),
        ]
        for name, act in steps:
            act()
            _core, inject = read_core_sections(p)
            assert inject == ["注入甲", "注入乙"], "第 %s 步之后仅注入词被改了" % name

    def test_gate_and_scraping_not_affected_by_inject_in_any_mode(self, ws_tmp):
        """仅注入词在三种态下都：**不进门槛判定**、**不被步骤9 抓取**。"""
        d = self._prep(ws_tmp)
        # 自动态：核心只有 1 个自动词，门槛 2 -> 不过；注入词再多也不该让它过
        set_list(d, CAT, "core", "auto")
        plan = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[], gate=2, gate_applies=True)
        assert plan.gate_ok is False, "仅注入词不该把门槛撑过去"
        assert "注入甲" not in plan.core, "仅注入词不该出现在步骤9 抓取的核心词里"
        # 人工态：同理
        set_list(d, CAT, "core", "manual", ["m1"])
        plan2 = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[], gate=2, gate_applies=True)
        assert plan2.gate_ok is False
        assert "注入甲" not in plan2.core
        # 停止态：同样
        set_stop(d, CAT, True)
        plan3 = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[], gate=2, gate_applies=True)
        assert "注入甲" not in plan3.core

    def test_step10_reads_inject_words_without_mode_check(self):
        """源码级护栏：`Plan.inject_words` 里**不能有**任何三态判断。

        一旦有人写成 `if self.mode == MODE_MANUAL: ...`，注入词就会受三态影响 —— 与用户口径冲突。
        """
        import inspect

        from core.keyword_lists import Plan
        src = inspect.getsource(Plan.inject_words.fget)
        for token in ("self.mode", "is_manual", "self.stopped", "MODE_MANUAL", "MODE_STOP"):
            assert token not in src, "`inject_words` 里出现了三态判断: %s" % token


# ============================================================ F. 核心大词预填 -> 保存后永久转人工
class TestCorePrefillThenPermanentManual:
    """用户口径 A（2026-10-12）：「不要自动注入，所有核心大词让人工选」。

    做法：**自动态下把自动词预填进编辑区**（方便你在此基础上删/加）→ 一动就切「人工清单」
    → 保存后**从此以你的清单为准、不再自动**。
    ⚠ 关键约束：**预填只是界面行为，绝不自动写文件** —— 否则存量 14 个类目会被静默改成人工。
    """

    # ---------------- 前端：预填 ----------------
    def test_prefill_falls_back_to_auto_words(self):
        body = _src("async function loadKeywordPanel(force)")
        assert body, "找不到 loadKeywordPanel"
        m = re.search(r"kwEdit\.core\s*=\s*(.+?);", body, re.S)
        assert m, "找不到 kwEdit.core 的赋值"
        expr = m.group(1)
        assert "_coreManual.length" in expr and "effective" in expr, expr
        # 三元第一支必须是**人工**（人工优先），否则手工清单会被自动词盖掉
        assert expr.index("_coreManual.length") < expr.index("effective"), expr

    def test_prefill_flag_marks_auto_state(self):
        body = _src("async function loadKeywordPanel(force)")
        assert "__kwCorePrefilled" in body
        assert "_coreManual.length" in body and "effective" in body

    def test_prefill_never_writes_the_file(self):
        """★ 预填**不能**调保存接口 —— 否则打开面板就把存量类目变成人工清单（静默改行为）。"""
        body = _src("async function loadKeywordPanel(force)")
        assert "keyword-lists/save" not in body
        assert "kw-core-words" not in body

    # ---------------- 前端：一动就切「人工清单」 ----------------
    def test_editing_prefilled_core_switches_to_manual(self):
        """核心词一改就切人工 —— 不切的话，改完点保存会被**当作自动口径清掉**（改了等于没改）。"""
        body = _src("function markKwDirty(kind)")
        assert body, "找不到 markKwDirty"
        assert 'kind === "core"' in body
        assert "__kwCorePrefilled" in body
        assert 'currentKwMode() === "auto"' in body
        assert 'kwModeRadio("manual")' in body

    def test_all_edit_paths_pass_kind(self):
        """★ 所有编辑入口都必须把 `kind` 传下去，否则上面那条自动切人工**永远不触发**。"""
        assert "markKwDirty();" not in HTML, "还有没传 kind 的 markKwDirty() 调用"
        assert HTML.count("markKwDirty(kind);") >= 4, "编辑入口少于 4 个（toggle/添加/填充/清空）"

    def test_switch_guard_is_not_vacuous(self):
        """突变验证：把"核心词"判定换成别的词，判据必须为 False（证明它真的在检查）。"""
        guard = lambda b: ('kind === "core"' in b and "__kwCorePrefilled" in b   # noqa: E731
                           and 'kwModeRadio("manual")' in b)
        body = _src("function markKwDirty(kind)")
        assert guard(body) is True
        mutated = body.replace('kind === "core"', 'kind === "XXXX"')
        assert mutated != body and guard(mutated) is False

    # ---------------- 前端：提示条 ----------------
    def test_hint_element_exists_and_is_conditional(self):
        assert 'id="kwCorePrefillHint"' in HTML, "缺少预填提示条"
        body = _src("function renderKeywordPanel()")
        assert body, "找不到 renderKeywordPanel"
        assert "kwCorePrefillHint" in body and "__kwCorePrefilled" in body, \
            "提示条没跟预填状态联动"
        assert "display = \"none\"" in body or "display='none'" in body, \
            "非预填态应隐藏提示条"

    # ---------------- 后端：保存后永久转人工 ----------------
    def test_saved_manual_list_replaces_auto_for_good(self, ws_tmp):
        """★ 保存人工清单后，核心词**只**来自清单 —— 自动词不再混进来。"""
        d = _d(ws_tmp)
        p1 = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[])
        assert p1.mode == MODE_AUTO and p1.core == ["auto1", "auto2"]
        set_list(d, CAT, "core", "manual", ["人工一", "人工二"])
        p2 = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[])
        assert p2.mode == MODE_MANUAL
        assert p2.core == ["人工一", "人工二"], "自动词又混进来了"
        assert "auto1" not in p2.core

    def test_untouched_category_keeps_auto_behaviour(self, ws_tmp):
        """★ 没保存过的类目（存量 14 个）行为**完全不变** —— 这是选 A 而不是 B 的意义。"""
        d = _d(ws_tmp)
        assert not (d / core_manual_name(CAT)).is_file()
        p = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[])
        assert p.mode == MODE_AUTO and p.core == ["auto1"]

    def test_prefill_then_save_keeps_inject_untouched(self, ws_tmp):
        """预填 -> 只改核心词 -> 保存：**仅注入词不受影响**。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "inject", "manual", ["注入甲"])
        set_list(d, CAT, "core", "manual", ["人工核心"])
        _core, inject = read_core_sections(d / core_manual_name(CAT))
        assert inject == ["注入甲"]
        plan = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[])
        assert plan.core == ["人工核心"] and plan.inject_words == ["人工核心", "注入甲"]


# ============================================================ G. 长尾词与核心大词同口径
class TestTailPrefillMirrorsCore:
    """用户口径（2026-10-12）：**长尾词的自动词也同步核心大词的显示** ——
    自动态下同样预填出来、看得见，一动就转人工。"""

    def test_tail_prefill_uses_crawl_not_pool(self):
        """★★ 长尾词预填必须用 `longtail.crawl`（步骤9 真在抓的 ≤5 条），**不是** `auto` 候选池。

        实测（2026-10-12）：`auto` 是 20-25 条的候选池，而 `crawl` 是"实体 × 基座"**组装**出来的
        5 条 —— 两者**不是包含关系**（Dog Dryer 的 5 条一条都不在池子里）。
        拿池子预填，用户不改直接保存 → 转人工 → 抓取量从 5 变 25（页数/耗时/成本暴涨）。
        """
        body = _src("async function loadKeywordPanel(force)")
        assert "(d.longtail || {}).crawl" in body, "长尾词预填没用 crawl"
        assert "_tailCrawl" in body
        # 不该再拿 effective/auto 去预填长尾
        seg = body[body.find("kwEdit.longtail"):body.find("kwEdit.longtail") + 300]
        assert "effective" not in seg, "长尾词预填又用回 effective（候选池）了"
        assert "(d.longtail || {}).auto" not in seg, "长尾词预填不该用候选池 auto"

    def test_tail_prefill_falls_back_to_manual_first(self):
        body = _src("async function loadKeywordPanel(force)")
        assert "__kwTailPrefilled" in body, "长尾词没有预填标记"
        m = re.search(r"kwEdit\.longtail\s*=\s*(.+?);", body, re.S)
        assert m, "找不到 kwEdit.longtail 赋值"
        expr = m.group(1)
        assert "_tailManual.length" in expr and "_tailCrawl" in expr, expr
        assert expr.index("_tailManual.length") < expr.index("_tailCrawl"), \
            "人工必须优先（顺序反了会把手写清单盖掉）"

    def test_tail_prefill_only_when_no_manual_list(self):
        """先取人工、再判"有没有人工" —— 顺序不能反（反了会把人工清单判成"没有"）。"""
        body = _src("async function loadKeywordPanel(force)")
        i_manual = body.find("const _tailManual = ((d.longtail || {}).manual || []).slice();")
        i_pref = body.find("__kwTailPrefilled")
        assert i_manual >= 0, "找不到「先取人工长尾」那行"
        assert i_pref > i_manual, "预填判断应在「先取人工」之后"

    def test_tail_prefill_never_writes_the_file(self):
        body = _src("async function loadKeywordPanel(force)")
        assert "keyword-lists/save" not in body

    def test_tail_edit_switches_to_manual(self):
        """长尾词一动也要切人工（否则改了会被当作自动口径清掉）。"""
        body = _src("function markKwDirty(kind)")
        assert 'kind === "longtail"' in body and "__kwTailPrefilled" in body
        assert 'kind === "core"' in body and "__kwCorePrefilled" in body, "核心那条不能被改坏"

    def test_tail_hint_warns_manual_crawls_everything(self):
        """★★ 长尾词预填的**关键警告**：转人工后**清单里全部**都会被抓取。

        这不是文案问题 —— 自动只抓 5 条，转人工后条数变多则页数/耗时/成本上升。
        有人删掉这句警告就等着踩坑。
        """
        body = _src("function renderKeywordPanel()")
        i = body.find("kwTailPrefillHint")
        seg = body[i:i + 1100]
        assert "实际在抓" in seg, "必须说明预填的是「实际在抓」的那几条"
        assert "候选池" in seg, "必须说明下方词池才是候选池"
        assert "全部都会被步骤9 抓取" in seg, "必须写明「转人工后全部都抓」"

    def test_server_exposes_crawl_field_separate_from_pool(self):
        """★ 后端必须**单独**给出 `longtail.crawl`，且**不能**拿 `auto`/`effective` 顶替。

        行为面另由 `scripts/_plan_pilot/_probe_crawl.py` 实测过：crawl=5、auto=25。
        """
        src = (ROOT / "webui" / "server.py").read_text(encoding="utf-8", errors="replace")
        assert '"crawl": _auto_crawl_longtail(' in src, "没接上 crawl 字段"
        assert "def _auto_crawl_longtail(" in src
        # 必须复用步骤9 的函数（自己复刻必然漂移）
        for fn in ("crawl_words", "parse_entity_artifact", "_accessory_terms_from",
                   "_matches_accessory", "_top_related_longtail"):
            assert fn in src, "没复用步骤9 的 %s" % fn

    def test_tail_hint_exists_and_is_conditional(self):
        assert 'id="kwTailPrefillHint"' in HTML, "缺少长尾词预填提示条"
        body = _src("function renderKeywordPanel()")
        assert "kwTailPrefillHint" in body and "__kwTailPrefilled" in body

    def test_tail_saved_manual_replaces_auto(self, ws_tmp):
        """保存人工长尾清单后，长尾词**只**来自清单。"""
        d = _d(ws_tmp)
        p1 = resolve(d, CAT, auto_core=["a"], auto_longtail=["t1", "t2"])
        assert p1.longtail == ["t1", "t2"]
        set_list(d, CAT, "longtail", "manual", ["人工长尾"])
        p2 = resolve(d, CAT, auto_core=["a"], auto_longtail=["t1", "t2"])
        assert p2.longtail == ["人工长尾"], "自动长尾词又混进来了"

    def test_tail_change_does_not_touch_core_or_inject(self, ws_tmp):
        """只动长尾词：核心大词与仅注入词都不受影响。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "inject", "manual", ["注入甲"])
        set_list(d, CAT, "core", "manual", ["人工核心"])
        set_list(d, CAT, "longtail", "manual", ["人工长尾"])
        core, inject = read_core_sections(d / core_manual_name(CAT))
        assert core == ["人工核心"] and inject == ["注入甲"]
        plan = resolve(d, CAT, auto_core=["auto1"], auto_longtail=[])
        assert plan.longtail == ["人工长尾"]
        assert plan.inject_words == ["人工核心", "注入甲"]


# ============================================================ H. 「不注入」：从注入里扣掉某个核心词
class TestNoInjectSubtracts:
    """用户口径 A（2026-10-12）：面板上把一个达标核心词从注入里删掉 = 记进 `[不注入]`。

    语义：该词**仍然**是核心大词、**仍然**被步骤9 抓取，只是**不参与步骤10 的判定**。
    为什么必须有这个减法清单：`inject_words = 核心 + 仅注入` 是**只加不减**的，
    光把词从「仅注入」删掉、或把它加进去再删，都**不会**让核心词退出注入。
    """

    def test_write_and_read_three_sections(self, ws_tmp):
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        c, i, x = write_core_sections3(p, ["c1", "c2", "c3"], ["手加"], ["c2"])
        assert (c, i, x) == (["c1", "c2", "c3"], ["手加"], ["c2"])
        assert read_core_sections3(p) == (["c1", "c2", "c3"], ["手加"], ["c2"])
        txt = p.read_text(encoding="utf-8")
        assert NOINJECT_SECTION in txt and txt.index("[仅注入]") < txt.index(NOINJECT_SECTION)

    def test_legacy_two_section_file_reads_noinject_empty(self, ws_tmp):
        """向后兼容：只有两分区的旧文件 -> 不注入为空（行为完全不变）。"""
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        write_core_sections(p, ["c1"], ["i1"])
        assert read_core_sections3(p) == (["c1"], ["i1"], [])

    def test_old_signature_still_two_tuple(self, ws_tmp):
        """★ `write_core_sections`/`read_core_sections` 的**旧签名不变** —— 别打断既有调用方。"""
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        got = write_core_sections(p, ["c1"], ["i1"])
        assert isinstance(got, tuple) and len(got) == 2, "旧签名被改成 3 元组了"
        assert len(read_core_sections(p)) == 2

    def test_inject_words_subtracts_but_core_keeps_it(self, ws_tmp):
        """★ 核心：扣掉之后 —— `inject_words` 没有它，但 `core` **仍然有它**（照样被抓）。"""
        d = _d(ws_tmp)
        write_core_sections3(d / core_manual_name(CAT), ["c1", "c2", "c3"], [], ["c2"])
        plan = resolve(d, CAT, auto_core=["a1"], auto_longtail=[])
        assert plan.core == ["c1", "c2", "c3"], "不该把 c2 从核心词里删掉"
        assert plan.inject_words == ["c1", "c3"], "c2 应已被扣出注入"
        assert plan.no_inject == ["c2"]
        assert "不注入" in plan.inject_text()

    def test_auto_core_word_can_be_subtracted(self, ws_tmp):
        """★ 自动态也扣得掉（这正是"达标核心词删不掉"的解法）。"""
        d = _d(ws_tmp)
        write_core_sections3(d / core_manual_name(CAT), [], [], ["auto2"])
        plan = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[])
        assert plan.mode == MODE_AUTO
        assert plan.core == ["auto1", "auto2"]
        assert plan.inject_words == ["auto1"], "自动核心词没被扣掉"

    def test_noinject_wins_over_inject_only(self, ws_tmp):
        """一个词同时出现在两个分区 -> **不注入优先**（不能既"只注入"又"不注入"）。"""
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        p.write_text("[核心大词]\nc1\n[仅注入]\ndup\n[不注入]\ndup\n", encoding="utf-8")
        plan = resolve(d, CAT, auto_core=[], auto_longtail=[])
        assert plan.inject_words == ["c1"]
        # 写盘时也按"不注入优先"剔重
        _c, i, x = write_core_sections3(p, ["c1"], ["dup"], ["dup"])
        assert i == [] and x == ["dup"]

    def test_set_list_noinject_keeps_other_sections(self, ws_tmp):
        """保存「不注入」不动核心/仅注入；清空它也不动。"""
        d = _d(ws_tmp)
        set_list(d, CAT, "core", "manual", ["c1", "c2"])
        set_list(d, CAT, "inject", "manual", ["i1"])
        r = set_list(d, CAT, "noinject", "manual", ["c2"])
        assert r["noinject_saved"] == ["c2"]
        assert r["core_saved"] == ["c1", "c2"] and r["inject_saved"] == ["i1"]
        r2 = set_list(d, CAT, "noinject", "auto")
        assert r2["noinject_saved"] == []
        c, i, x = read_core_sections3(d / core_manual_name(CAT))
        assert (c, i, x) == (["c1", "c2"], ["i1"], [])

    def test_file_deleted_only_when_all_three_empty(self, ws_tmp):
        """三个分区都空才删文件（只有「不注入」也算有内容 -> 文件要留）。"""
        d = _d(ws_tmp)
        p = d / core_manual_name(CAT)
        set_list(d, CAT, "noinject", "manual", ["c1"])
        assert p.is_file(), "只有「不注入」时不该删文件"
        set_list(d, CAT, "noinject", "auto")
        assert not p.is_file()

    def test_noinject_rejected_over_limit(self, ws_tmp):
        from core.keyword_lists import MAX_NOINJECT
        d = _d(ws_tmp)
        r = set_list(d, CAT, "noinject", "manual",
                     ["w%d" % i for i in range(MAX_NOINJECT + 1)])
        assert r["ok"] is False and "最多" in r["error"]

    def test_inject_words_still_mode_independent_with_noinject(self, ws_tmp):
        """★ 加了减法之后，「不受三态影响」这条**仍然**成立。

        ⚠ 注意：**停止态的核词按设计回落自动口径**（step10 文档原话：「人工停止标记只影响
          步骤9(不抓取), 此处核心词仍回落自动口径, 不会变空」）—— 所以三态下 `plan.core`
          **本来就不同**（这不是 bug）。这里验的是：**不注入的减法在三态下都照常生效**。
        """
        d = _d(ws_tmp)
        write_core_sections3(d / core_manual_name(CAT), ["mc"], ["i1"], ["mc"])
        for stopped in (False, True):
            set_stop(d, CAT, stopped)
            # auto_core 里也放 mc，这样两种模式下 core 都含 mc，减法可对比
            plan = resolve(d, CAT, auto_core=["mc", "a1"], auto_longtail=[])
            assert "mc" not in plan.inject_words, "停止=%s 时「不注入」没生效" % stopped
            assert "i1" in plan.inject_words, "停止=%s 时仅注入词丢了" % stopped
        set_stop(d, CAT, False)
        # 人工态：核心用人工清单（含 mc） -> 同样被扣
        plan2 = resolve(d, CAT, auto_core=["a1"], auto_longtail=[])
        assert plan2.mode == MODE_MANUAL
        assert plan2.core == ["mc"] and plan2.inject_words == ["i1"]


# ============================================================ I. 面板接线（注入块＝合并清单 + 来源标记 + 删除路由）
class TestInjectPanelWiring:
    """用户要求：「注入词可以自动填达标的核心大词，但需要和核心词一样**标出来**，
    不合适的可以**删除和添加**」—— 面板把**合并后的实际注入词**显示出来，
    每个词标来源（`核心` / `手加`），删除按来源路由到 `[不注入]` / `[仅注入]`。
    """

    def test_html_has_excluded_line(self):
        assert 'id="kwInjectExcluded"' in HTML, "没有『已从注入中排除』那一行"

    def test_frontend_merges_same_formula_as_backend(self):
        """★ 前端合并口径必须与后端 `Plan.inject_words` **一致**：核心 − 不注入 + 仅注入。"""
        body = _src("function injectMerged()")
        assert body, "找不到 injectMerged"
        assert "kwEdit.noinject" in body, "没做减法"
        assert "core || {}).effective" in body or "effective" in body, "没取生效核心词"
        assert "kwEdit.inject" in body, "没加手加词"

    def test_merged_matches_backend_semantics_in_python(self, ws_tmp):
        """★ 用**同一组输入**在 Python 侧重算一遍，确认两边口径一致（防漂移）。"""
        d = _d(ws_tmp)
        write_core_sections3(d / core_manual_name(CAT), ["mc"], ["hand"], ["mc"])
        plan = resolve(d, CAT, auto_core=["a1", "a2"], auto_longtail=[])
        # 前端公式： (core.effective ∪ kwEdit.core) − noinject + inject
        merged = [w for w in list(plan.core) if w not in set(plan.no_inject)]
        merged += [w for w in plan.inject_only if w not in merged and w not in set(plan.no_inject)]
        assert merged == list(plan.inject_words), (merged, plan.inject_words)

    def test_delete_routes_by_source(self):
        """★ 删除按来源路由：`核心` → 记进 `[不注入]`；`手加` → 从 `[仅注入]` 移除。"""
        body = _src("function renderInjectChips(box)")
        assert body, "找不到 renderInjectChips"
        assert 'excludeInject(' in body and 'toggleKwWord("inject"' in body
        assert 'x.dataset.src === "core"' in body, "没按来源分流"
        ex = _src("function excludeInject(word)")
        assert "kwEdit.noinject" in ex, "排除没写进 noinject"

    def test_source_labels_rendered(self):
        """两个来源都要**标出来**（用户明确要求"和核心词一样标出来"）。"""
        body = _src("function renderInjectChips(box)")
        assert "核心" in body and "手加" in body, "没打来源标记"

    def test_save_saves_both_sections(self):
        """★ 「保存仅注入词」必须**同时**保存 `[不注入]` —— 否则删了不生效（改了等于没改）。"""
        body = _src("async function saveKwInject()")
        assert 'list: "inject"' in body and 'list: "noinject"' in body
        assert body.index('list: "inject"') < body.index('list: "noinject"')

    def test_clear_resets_both(self):
        """「清空仅注入」要**整体复位**（含不注入），否则留下看不见的减法。"""
        body = _src("function clearKwEdit(kind)")
        assert 'kind === "inject"' in body and "kwEdit.noinject = []" in body

    def test_unsaved_warning_covers_noinject(self):
        """`saveKwMode` 的"未保存"告警必须把 `[不注入]` 也算进去。"""
        body = _src("async function saveKwMode()")
        assert "excluded" in body, "injChanged 没比 excluded"
        assert "kwEdit.noinject" in body

    def test_server_exposes_the_three_blocks(self):
        src = (ROOT / "webui" / "server.py").read_text(encoding="utf-8", errors="replace")
        for k in ('"excluded": list(plan.no_inject)', '"effective_inject": list(plan.inject_words)',
                  '"core_part"', '"excluded_section"', "MAX_NOINJECT", "NOINJECT_SECTION"):
            assert k in src, "服务端缺 %s" % k
