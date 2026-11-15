# -*- coding: utf-8 -*-
"""step16 属性提取的**样本列**测试（2026-10-12）。

背景：提示词一直写着"从 标题/五点/**详细参数** 判断"，但 `_build_attr_sample` **只喂了标题+五点**
—— 空头承诺，于是每行 hint 的"判定依据"只能写"从标题/五点"。拆分表里 `详细参数` 这一列
**本来就有数据**，这次补上，并把"喂哪些列"收敛成**唯一真值** `ATTR_SAMPLE_COLS`，
提示词措辞由它拼出 —— 两边再也不会漂移。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from steps.step16_attribute_annotation import (  # noqa: E402
    ATTR_SAMPLE_COLS, ATTR_SAMPLE_FIELD_MAX, ATTR_SAMPLE_MAX_CHARS, ATTR_SAMPLE_MAX_ROWS,
    ATTR_SPEC_MIN_RUNS_RATIO,
    ATTR_SPEC_RUNS_DEFAULT, Step16AttributeAnnotation, sample_cols_text,
    spec_min_runs_default,
)

SRC = (ROOT / "steps" / "step16_attribute_annotation.py").read_text(encoding="utf-8",
                                                                   errors="replace")
_build = Step16AttributeAnnotation._build_attr_sample


def _df(**cols):
    return pd.DataFrame(cols)


# ---------------------------------------------------------------- 列名对应表
class TestSampleCols:
    def test_includes_detail_params(self):
        """★ 必须含 `详细参数` —— 它就是"hint 只会写标题/五点"的根因。"""
        names = [lbl for lbl, _c in ATTR_SAMPLE_COLS]
        assert names == ["标题", "五点", "详细参数", "SKU"], names

    def test_sku_is_a_variant_attribute_source(self):
        """★ `SKU` 列装的是**亚马逊变体属性**（`Size: … | Color: …`），不是卖家编码。

        它是 hint「典型取值」最权威的来源；有人以为是内部编码而删掉它就会退化。
        """
        d = {lbl: set(c) for lbl, c in ATTR_SAMPLE_COLS}
        assert "SKU" in d, "SKU 列被删了"
        assert "SKU" in d["SKU"], "候选列名必须对应拆分表里的真实列名 `SKU`"

    def test_candidate_column_names_match_the_split_table(self):
        """列名（含候选写法）都必须是拆分表里**真实存在**的那几个。"""
        d = {lbl: set(c) for lbl, c in ATTR_SAMPLE_COLS}
        assert "标题" in d["标题"] and "title" in d["标题"]
        assert "五点描述" in d["五点"]
        assert "详细参数" in d["详细参数"], "详细参数是拆分表第 23/24 列的真实列名"

    def test_real_split_table_has_those_columns(self):
        """有真实拆分表时，直接核对列名存在（这才是"对应表中的列名"的硬证据）。"""
        for cat in ("Automatic Cat Feeders-US-202608", "Cutting Boards-US-202608"):
            p = ROOT / "data" / cat / "14_流量拆分" / "流量拆分数据-需人工补充.xlsx"
            if not p.is_file():
                pytest.skip("无拆分表样本")
            cols = set(pd.read_excel(p, nrows=1).columns)
            for lbl, cands in ATTR_SAMPLE_COLS:
                assert any(c in cols for c in cands), \
                    "拆分表里没有 %s 的任一候选列 %s" % (lbl, cands)

    def test_sample_cols_text_is_derived(self):
        assert sample_cols_text() == "标题/五点/详细参数/SKU"


# ---------------------------------------------------------------- 样本构建
class TestBuildSample:
    def test_all_three_labels_present(self):
        df = _df(**{"标题": ["T1"], "五点描述": ["B1"], "详细参数": ["Brand:X | Shape:Y"],
                    "SKU": ["Size: 1-Piece | Color: Two-Tone"]})
        s = _build(None, df)
        assert "标题: T1" in s
        assert "五点: B1" in s
        assert "详细参数: Brand:X | Shape:Y" in s
        assert "SKU: Size: 1-Piece | Color: Two-Tone" in s

    def test_missing_column_is_skipped_not_crashed(self):
        """只有标题也能跑（旧数据目录/列名变了都不该炸）。"""
        s = _build(None, _df(**{"标题": ["只有标题"]}))
        assert "标题: 只有标题" in s and "详细参数" not in s

    def test_empty_values_skipped(self):
        df = _df(**{"标题": ["T"], "五点描述": [None], "详细参数": [float("nan")]})
        s = _build(None, df)
        assert "五点" not in s and "详细参数" not in s, "空值/nan 不该进样本"

    def test_no_usable_columns_returns_empty(self):
        assert _build(None, _df(**{"无关列": ["x"]})) == ""

    def test_list_bullets_joined(self):
        """`五点描述` 是 list 时全部条目都要进样本（2026-11-11：原来只取前两条，后几条卖点丢失）。"""
        s = _build(None, _df(**{"标题": ["T"], "五点描述": [["a", "b", "c"]],
                               "详细参数": ["P"]}))
        assert "五点: a | b | c" in s

    def test_long_bullets_not_cut_at_400(self):
        """★ 五点中位 ~1264 字：第 3~5 条卖点必须进样本，不能再截到 400 字。"""
        bullets = "x" * 1500 + " TAIL_MARKER"
        s = _build(None, _df(**{"标题": ["T"], "五点描述": [bullets]}))
        assert "TAIL_MARKER" in s
        assert len(s) <= ATTR_SAMPLE_FIELD_MAX["五点"] + 50

    def test_row_sampling_and_char_cap(self):
        n = ATTR_SAMPLE_MAX_ROWS * 3
        df = _df(**{"标题": ["T%d" % i for i in range(n)],
                    "五点描述": ["B%d" % i for i in range(n)],
                    "详细参数": ["P%d" % i for i in range(n)]})
        s = _build(None, df)
        assert len(s) <= ATTR_SAMPLE_MAX_CHARS
        assert s.count("\n标题: ") + (1 if s.startswith("标题: ") else 0) <= ATTR_SAMPLE_MAX_ROWS

    def test_char_cap_cuts_on_row_boundary(self):
        """超总长时按整行丢弃，最后一行必须完整（不出现半截行）。"""
        n = ATTR_SAMPLE_MAX_ROWS
        df = _df(**{"标题": ["T%d" % i for i in range(n)],
                    "五点描述": ["B" * 1990 + "END%d" % i for i in range(n)],
                    "详细参数": ["P" * 790 + "PEND" for _ in range(n)]})
        s = _build(None, df)
        assert len(s) <= ATTR_SAMPLE_MAX_CHARS
        assert s.endswith("PEND"), "最后一行被切成了半截"


# ---------------------------------------------------------------- 提示词与输入同源（防漂移）
class TestPromptMatchesInput:
    """★ 核心不变量：提示词里"从 X/Y/Z 判断"的那串，必须**就是**实际喂进去的列。"""

    def test_prompt_has_no_hardcoded_column_list(self):
        """提示词里不该再出现硬编码的列名串（应全部由 `sample_cols_text()` 拼出）。

        ⚠ 必须**先去 `#` 注释** —— 否则**解释性注释里提到那句历史措辞**就会假阳性
          （我第一版就栽在这，和 JS 侧 `_no_comments()` 是同一个教训）。
        """
        code = re.sub(r"(?m)#[^\n]*", "", SRC)
        hard = re.findall(r"从 [^\n\"]{0,20}标题/五点[^\n\"]{0,20}", code)
        assert not hard, "提示词里还有硬编码的列名串: %s" % hard[:3]

    def test_prompt_uses_the_derived_text(self):
        assert SRC.count("sample_cols_text()") >= 3, \
            "两处提示词 + 助手函数本身都应引用它"

    def test_every_sample_col_is_promised_to_the_model(self):
        """反向：喂进去的每一列，提示词都得告诉模型"可以从这里找依据"。"""
        txt = sample_cols_text()
        for lbl, _c in ATTR_SAMPLE_COLS:
            assert lbl in txt, "%s 没进提示词措辞" % lbl


# ============================================================ 抽取次数 / 票数门槛（两键一体）
class TestSpecRunsAndThreshold:
    """用户口径（2026-10-12）：3 遍不够准 → **5 遍**；而且**两键必须一起调**。

    `runs` = "N 遍里被提到几次"（票数），所以 `ATTR_SPEC_RUNS` 与 `ATTR_SPEC_MIN_RUNS`
    是**一个比例的两半**：只调 N 不调门槛 = **悄悄放开闸门**（属性集变大、偶然属性被自动采纳）。
    设计上让门槛**默认由 N 推导**（≈60%），显式覆盖且比例 <50% 时打 CRIT 告警。
    """

    def test_runs_fixed_at_three(self):
        """2026-11-11 用户口径：抽取遍数**写死为 3**，配置不可修改。"""
        assert ATTR_SPEC_RUNS_DEFAULT == 3

    def test_threshold_derives_to_three_for_five(self):
        assert spec_min_runs_default(5) == 3

    def test_derivation_reproduces_the_old_default(self):
        """★ 向后兼容：N=3 推出的正是**旧默认 2**（67%）—— 老类目口径不变。"""
        assert spec_min_runs_default(3) == 2

    def test_ratio_stays_reasonable(self):
        """任何 N 下门槛比例都不低于 50%（防"只调一个键"放开闸门）。"""
        for n in range(2, 21):
            k = spec_min_runs_default(n)
            assert k >= 2, "N=%d 门槛 %d 太低" % (n, k)
            assert k / n >= 0.5, "N=%d 门槛 %d 比例只有 %.0f%%" % (n, k, 100.0 * k / n)

    def test_ratio_constant_is_60_percent(self):
        assert abs(ATTR_SPEC_MIN_RUNS_RATIO - 0.6) < 1e-9

    def test_source_has_no_hardcoded_old_defaults(self):
        """★ 源码里不能再有硬编码的旧默认（`ATTR_SPEC_RUNS", 3` / `ATTR_SPEC_MIN_RUNS", 2`）。

        2026-10-12 之前 `_pool_merge` 的**日志**里就硬编码过一个 `2` ——
        日志报的门槛和真正生效的门槛不一致，比不报还坏。
        """
        code = re.sub(r"(?m)#[^\n]*", "", SRC)
        assert '"ATTR_SPEC_RUNS", 3' not in code, "还有硬编码的 ATTR_SPEC_RUNS=3"
        assert '"ATTR_SPEC_MIN_RUNS", 2' not in code, "还有硬编码的 ATTR_SPEC_MIN_RUNS=2"
        from steps.step16_attribute_annotation import spec_extraction_settings
        # 遍数写死：配置里的 ATTR_SPEC_RUNS / ATTR_SAMPLE_MODE 一律忽略
        for mode in ("uniform", "price_band"):
            info = spec_extraction_settings({"ATTR_SAMPLE_MODE": mode, "ATTR_SPEC_RUNS": 8})
            assert info["mode"] == "price_band"
            assert info["runs"] == 3 and info["runs_overridden"] is False

    def test_mismatch_warns_loudly(self):
        """两键比例过松时必须**告警**（不静默）——这是"必须一起调"的兜底。"""
        assert "票数门槛偏松" in SRC and "两键应一起调" in SRC


# ============================================================ 面板上的「抽取遍数」
class TestRunsPanelField:
    """用户口径（2026-10-12）：界面上**只给一个输入框（抽取遍数）**，票数门槛自动跟着变
    —— 物理上不可能"只调一个"。口径数值**只从后端拿**（单一真值），前端不自己算比例。
    """

    @staticmethod
    def _info(ws_tmp, extra: str) -> dict:
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_runs_probe",
                                                      ROOT / "webui" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cat = "RunsCat-US-202610"
        d = pathlib.Path(ws_tmp) / "data" / cat
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(
            'CAT: "%s"\nDATA_ROOT: "%s"\nSITE: "美国"\nMARKET: "amazon.com"\n%s'
            % (cat, (pathlib.Path(ws_tmp) / "data").as_posix(), extra), encoding="utf-8")
        return mod.Handler.__new__(mod.Handler)._spec_runs_info(str(d / "config.yaml"))

    def test_defaults(self, ws_tmp):
        info = self._info(ws_tmp, "")
        assert info["runs"] == info["default_runs"] == 3 and info["min_runs"] == 3
        assert info["runs_overridden"] is False and info["too_loose"] is False
        assert abs(info["ratio"] - 1.0) < 1e-9

    def test_config_runs_is_ignored(self, ws_tmp):
        """★ 2026-11-11：遍数写死为 3，配置里写 ATTR_SPEC_RUNS 也不生效。"""
        info = self._info(ws_tmp, "ATTR_SPEC_RUNS: 8\n")
        assert info["runs"] == 3 and info["runs_overridden"] is False
        assert info["min_runs"] == 3 and info["min_runs_overridden"] is False

    def test_threshold_override_still_read_and_capped(self, ws_tmp):
        """票数门槛 ATTR_SPEC_MIN_RUNS 仍可配置，但不超过遍数 3。"""
        info = self._info(ws_tmp, "ATTR_SPEC_MIN_RUNS: 2\n")
        assert info["min_runs"] == 2 and info["min_runs_overridden"] is True
        info = self._info(ws_tmp, "ATTR_SPEC_MIN_RUNS: 9\n")
        assert info["min_runs"] == 3

    # ---------------- 前端：只有一个输入框 ----------------
    def test_ui_has_the_runs_input_and_is_wired(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        assert 'id="attrSpecRuns"' in html, "面板上没有「抽取遍数」输入框"
        # 2026-10-12：改成两项**共用一个**保存按钮（原来是各自一个）
        assert '$("btnSaveAttrTuning").onclick = () => saveAttrTuning();' in html, "没接线"
        assert "ATTR_SPEC_RUNS" in html

    def test_ui_does_not_expose_the_threshold_as_an_input(self):
        """★ 关键：门槛**不能**是第二个输入框 —— 否则"两键一起调"的保护就没了。

        它只能以**只读文本**出现（`attrSpecRunsHint`）。
        """
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        import re as _re
        inputs = _re.findall(r'<input[^>]*id="([^"]+)"', html)
        assert "attrSpecRuns" in inputs
        for bad in ("attrSpecMinRuns", "ATTR_SPEC_MIN_RUNS_input", "attrMinRuns"):
            assert bad not in inputs, "门槛被做成了可编辑输入框: %s" % bad
        assert "attrSpecRunsHint" in html, "门槛的只读显示没了"

    def test_ui_shows_the_loose_warning(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        i = html.find("function renderAttrSpecRuns")
        assert i > 0
        seg = html[i:i + 1400]
        assert "too_loose" in seg and "门槛偏松" in seg, "界面没有把'门槛偏松'显示出来"
        assert "min_runs" in seg, "界面没显示推导出的门槛"


# ============================================================ 步骤17「一致率提示门槛」
class TestStabMinRateIsHintNotGate:
    """2026-10-12 核实：`ATTR_STAB_MIN_RATE` **只提示、不阻断**。

    之前 step17 文件头（"低于则阻断"）与 Web 提示词都写错了，**两处都改**；
    这里把它们钉住，防止"阻断"这个错说法再回来（人会因此不敢调）。
    """

    @staticmethod
    def _info(ws_tmp, extra: str) -> dict:
        import importlib.util
        spec = importlib.util.spec_from_file_location("srv_rate_probe",
                                                      ROOT / "webui" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cat = "RateCat-US-202610"
        d = pathlib.Path(ws_tmp) / "data" / cat
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(
            'CAT: "%s"\nDATA_ROOT: "%s"\nSITE: "美国"\nMARKET: "amazon.com"\n%s'
            % (cat, (pathlib.Path(ws_tmp) / "data").as_posix(), extra), encoding="utf-8")
        return mod.Handler.__new__(mod.Handler)._stab_min_rate_info(str(d / "config.yaml"))

    def test_fixed_zero_and_not_blocking(self, ws_tmp):
        """2026-11-11 用户口径：一致率提示门槛**写死为 0**，不阻断。"""
        info = self._info(ws_tmp, "")
        assert info["value"] == 0.0 and info["off"] is True and info["overridden"] is False
        assert info["blocking"] is False, "代码事实是**不阻断**，这里必须为 False"

    def test_config_value_is_ignored(self, ws_tmp):
        info = self._info(ws_tmp, "ATTR_STAB_MIN_RATE: 92.5\n")
        assert info["value"] == 0.0 and info["overridden"] is False
        src = (ROOT / "steps" / "step17_attr_annotation.py").read_text(encoding="utf-8")
        assert "floor = 0.0" in src

    def test_server_rejects_saving_fixed_keys(self):
        src = (ROOT / "webui" / "server.py").read_text(encoding="utf-8")
        assert '{"ATTR_SPEC_RUNS", "ATTR_STAB_MIN_RATE"} & set(values)' in src

    def test_source_does_not_claim_blocking(self):
        """★ step17 文件头**不能**再说"低于则阻断"（与代码不符，会误导人）。

        ⚠ 正则里刻意**不写全那句话** —— 否则我这个断言本身出现在源码里的可能性、
          以及"注释里引用旧措辞做说明"都会被算成命中（我今天在这上面栽过好几次）。
        """
        src = (ROOT / "steps" / "step17_attr_annotation.py").read_text(encoding="utf-8",
                                                                      errors="replace")
        assert "低于" + "则阻断" not in src, "文件头又写回旧措辞了"
        assert "只提示、不阻断" in src, "缺少'只提示不阻断'的说明"

    def test_ui_says_hint_not_gate(self):
        """★ Web 提示词也不能说"硬门槛/低于此值阻断"。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        assert "硬" + "门槛" not in html, "界面又写'硬门槛'了"
        # 找**提示词那一行**（不是 M 标签那一行）——按"交付值一致率"定位，别用第一个匹配
        i = html.find("交付值一致率")
        assert i > 0, "找不到一致率提示词那一行"
        seg = html[i:i + 220]
        assert "只提示" in seg and "不阻断" in seg, seg

    def test_ui_has_the_control_and_is_wired(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        assert 'id="attrMinRate"' in html
        # 2026-10-12：与「抽取遍数」共用一个保存按钮
        assert 'id="btnSaveAttrTuning"' in html
        assert '$("btnSaveAttrTuning").onclick = () => saveAttrTuning();' in html
        assert "ATTR_STAB_MIN_RATE" in html, "保存时没写这个键"


# ============================================================ 重置步骤16 必须清干净 + 面板要刷新
class TestResetStep16:
    """2026-10-12 用户报障的两个现象，其实是**两个独立的漏洞**：

    ① **候选属性还在**：`reset_steps_from()` 只删 `step.artifacts`（= spec 一个文件）+
       `step.archive_dirs`；而 step16 原来 **`archive_dirs` 是空的** →
       `16_产品属性标注/属性集待确认清单.json`（**候选属性的数据源**）**留在原地** ✗
    ② **表不自动刷新**：前端 watcher 原来**只盯步骤 4/7**、且**只在"变成 DONE"时**刷 →
       步骤16 不在列表里；而**重置是 DONE→非DONE（方向相反）** → 永远不触发 ✗
    """

    def test_step16_declares_its_directory_for_reset(self):
        """★ 修法①：step16 必须声明 `archive_dirs`，否则重置只删 spec、目录残留。"""
        from steps.step16_attribute_annotation import Step16AttributeAnnotation as S
        assert S.archive_dirs == ["16_产品属性标注"], S.archive_dirs
        assert any("attribute-spec" in a for a in S.__dict__.get("_x", []) or []) or True

    def test_pending_list_lives_in_that_dir(self):
        """候选属性的数据源就在这个目录里 —— 两者必须绑定（改了目录名这条会红）。"""
        src = (ROOT / "steps" / "step16_attribute_annotation.py").read_text(
            encoding="utf-8", errors="replace")
        assert '"16_产品属性标注"' in src
        assert "属性集待确认清单.json" in src, "待确认清单的落盘名变了？"
        # 候选属性面板读的也是它
        srv = (ROOT / "webui" / "server.py").read_text(encoding="utf-8", errors="replace")
        assert "属性集待确认清单" in srv, "Web 读的不是这个文件？"

    def test_only_step16_owns_that_dir(self):
        """整目录随步骤删是安全的：**只有 step16** 引用这个目录名。"""
        hits = []
        for f in sorted((ROOT / "steps").glob("*.py")):
            if "16_产品属性标注" in f.read_text(encoding="utf-8", errors="replace"):
                hits.append(f.name)
        assert hits == ["step16_attribute_annotation.py"], hits

    def test_frontend_watcher_covers_step16_both_directions(self):
        """★ 修法②：watcher 要**盯步骤16**，并且**状态一变就刷**（完成/重置都要）。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        i = html.find("function watchKeywordRefresh")
        assert i > 0, "找不到 watcher"
        seg = html[i:i + 2200]
        assert "s.number === 16" in seg, "步骤16 没进监听列表"
        assert "attrChanged" in seg and "loadAttrSpec(true)" in seg, "没有刷新属性集面板"
        # 关键：不能只在"变成 DONE"时刷 —— 重置是反方向
        assert "prev === s.status" in seg, "没有做'状态一变就刷'的判断"
        assert 's.status === "DONE" && s.number === 16' not in seg, \
            "步骤16 又被限制成'只在完成时刷'了"

    def test_frontend_watcher_still_handles_4_and_7(self):
        """别为了修 16 把原来的 4/7 逻辑弄坏。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        i = html.find("function watchKeywordRefresh")
        seg = html[i:i + 2200]
        assert "s.number === 4 || s.number === 7" in seg
        assert "loadKeywordPanel(true)" in seg
        assert "__kwDirty" in seg, "关键词面板的'未保存就不覆盖'保护没了"


# ============================================================ 面板 vs 配置中心：只能有一个改的地方
class TestSinglePlaceToEdit:
    """用户口径（2026-10-12）：这两个旋钮既然已经在「步骤16 属性集提取与确认」面板里能改，
    **配置中心就不该再出现** —— 同一个键两处都能改就是"冲突 / 以谁为准"的隐患。
    """

    @staticmethod
    def _hidden(html: str) -> list[str]:
        import re
        m = re.search(r"const HIDDEN_KEYS = new Set\(\[(.*?)\]\);", html, re.S)
        return re.findall(r'"([A-Z_]+)"', m.group(1)) if m else []

    def test_both_knobs_are_hidden_from_the_config_form(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        hidden = self._hidden(html)
        assert "ATTR_SPEC_RUNS" in hidden, "抽取遍数还留在配置中心"
        assert "ATTR_STAB_MIN_RATE" in hidden, "一致率提示门槛还留在配置中心"
        # 门槛**故意**不给输入框（两键一体）→ 也要隐藏，免得有人手加进 config 后从表单改
        assert "ATTR_SPEC_MIN_RUNS" in hidden, "票数门槛可能被手加进 config 后从表单改"

    def test_panel_has_the_controls(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        assert 'id="attrSpecRuns"' in html and 'id="attrMinRate"' in html

    def test_two_inputs_share_one_save_button(self):
        """★ 用户口径（2026-10-12）：两项**共用一个保存按钮**（原来各有一个）。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        assert 'id="btnSaveAttrTuning"' in html, "没有合并后的保存按钮"
        assert '$("btnSaveAttrTuning").onclick = () => saveAttrTuning();' in html, "没接线"
        for old in ("btnSaveAttrRuns", "btnSaveAttrMinRate", "saveAttrRuns", "saveAttrMinRate"):
            assert old not in html, "旧按钮/旧函数残留: %s" % old

    def test_one_save_writes_only_changed_keys(self):
        """★ 只提交**改动过的**键（没改的不写盘，避免无谓动 config）；任一项非法则整体不存。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        i = html.find("async function saveAttrTuning")
        assert i > 0
        seg = html[i:i + 1800]
        assert "const changed = {}" in seg and "Object.keys(changed).length" in seg
        assert "changed.ATTR_SPEC_RUNS" in seg and "changed.ATTR_STAB_MIN_RATE" in seg
        # 校验必须在写盘之前（不做半份）
        assert seg.index("抽取遍数要 >= 1") < seg.index("/api/config/save")
        assert seg.index("0-100") < seg.index("/api/config/save")
        assert "values: changed" in seg, "保存时没把两项一起提交"

    def test_config_form_filter_honours_hidden_keys_in_groups_too(self):
        """★ 关键修复：`GROUPS` 的渲染原来**不看 HIDDEN_KEYS** → 登记进 GROUPS 的键藏不住。

        现在 `present` 必须同时过滤 HIDDEN_KEYS，否则"HIDDEN_KEYS 说话不算数"
        （我藏了它却还在 → 又是一次"改了等于没改"）。
        """
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        assert "in values && !HIDDEN_KEYS.has(k)" in html, \
            "GROUPS 渲染没过滤 HIDDEN_KEYS"

    def test_panel_save_is_independent_of_the_form(self):
        """面板保存走 `/api/config/save`（通用 patch），**与表单是否渲染无关** ——
        所以"从表单藏起来"不会把面板的保存能力一起藏掉。"""
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        i = html.find("async function saveAttrTuning")
        assert i > 0
        seg = html[i:i + 1800]
        assert "/api/config/save" in seg, "面板没走通用保存接口"
        assert "values: changed" in seg
