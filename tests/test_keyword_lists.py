# 单元测试: 关键词清单三态(自动 / 人工清单 / 人工停止, 2026-09-29)
# ---------------------------------------------------------------------------
# 需求(用户原话的三情况):
#   ① 关键词都达标, 但有的不是我想要的 -> 人工删除 + 选别的;
#   ② 关键词都不达标 -> 人工选不达标的/手动添加, 或者就停止下一步;
#   ③ 都达标且都合适 -> 自动执行, 不做任何操作。
# 于是: 自动态行为与旧版**完全一致**; 人工态用清单词(不看 FR-07 判定)且门槛按
# **最终词数**判; 停止态直接中止步骤9。文件语义见 core/keyword_lists.py。
import os
from pathlib import Path

import pytest

from core.config import Config
from core.errors import StepFailedError
from core.keyword_lists import (
    MODE_AUTO,
    MODE_MANUAL,
    MODE_STOP,
    core_manual_name,
    longtail_manual_name,
    manual_first,
    read_core_sections,
    read_keywords_file,
    read_sections,
    read_words,
    resolve,
    set_list,
    set_stop,
    status,
    stop_name,
    used_head,
    used_name,
    write_core_sections,
    write_used,
    write_words,
)
from core.paths import Paths
from steps.base import StepContext
from steps.step06_trend import Step06Trend
from steps.step08_keyword_report import Step08KeywordReport
from steps.step09_search_collect import Step09SearchCollect

CAT = "Kw Cat-US-202609"


def _mk(ws_tmp, cat: str = CAT) -> Path:
    d = Path(ws_tmp) / cat
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_ctx(tmp_dir: str, **extra) -> StepContext:
    data = {"CAT": CAT, "DATA_ROOT": tmp_dir, "DRY_RUN": True}
    data.update(extra)
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _write_kw_file(d: Path, cat: str = CAT, core=(), tail=()) -> Path:
    p = d / f"{cat}-keywords.txt"
    p.write_text("[核心大词]\n" + "\n".join(core) + "\n[长尾词]\n" + "\n".join(tail) + "\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------- 读写基础
def test_read_words_normalizes_and_skips_sections(tmp_path):
    p = Path(tmp_path) / "w.txt"
    p.write_text("# 注释\n[核心大词]\nDog Dryer\n dog dryer \nHair Dryer\n\n", encoding="utf-8")
    assert read_words(p) == ["dog dryer", "hair dryer"]


def test_read_words_missing_file_is_empty(tmp_path):
    assert read_words(Path(tmp_path) / "nope.txt") == []


def test_write_words_empty_removes_file(tmp_path):
    p = Path(tmp_path) / "w.txt"
    write_words(p, ["a", "A", " b "])
    assert p.read_text(encoding="utf-8") == "a\nb\n"
    assert write_words(p, []) == []
    assert not p.exists()


def test_read_keywords_file_sections(tmp_path):
    d = _mk(tmp_path)
    _write_kw_file(d, core=["dog dryer", "pet dryer"], tail=["dog dryer for home"])
    core, tail = read_keywords_file(d / f"{CAT}-keywords.txt")
    assert core == ["dog dryer", "pet dryer"]
    assert tail == ["dog dryer for home"]


def test_read_sections_keeps_section_names(tmp_path):
    p = Path(tmp_path) / "e.txt"
    p.write_text("[场景.长尾词]\ndog dryer for home\n[人群.长尾词]\ndog dryer for pro\n",
                 encoding="utf-8")
    sec = read_sections(p)
    assert sec["[场景.长尾词]"] == ["dog dryer for home"]
    assert sec["[人群.长尾词]"] == ["dog dryer for pro"]


# ---------------------------------------------------------------- resolve 三态
def test_resolve_defaults_to_auto(tmp_path):
    d = _mk(tmp_path)
    plan = resolve(d, CAT, auto_core=["a", "b", "c"], auto_longtail=["l1", "l2"],
                   screen_verdict={"a": "达标", "b": "达标"}, gate=2)
    assert plan.mode == MODE_AUTO
    assert plan.core == ["a", "b", "c"]
    assert plan.longtail == ["l1", "l2"]
    assert plan.qualified == 2
    assert plan.no_verdict == 1
    assert plan.gate_ok is True


def test_resolve_auto_gate_fails_below_threshold(tmp_path):
    d = _mk(tmp_path)
    plan = resolve(d, CAT, auto_core=["a"], auto_longtail=[], screen_verdict={"a": "达标"}, gate=2)
    assert plan.gate_ok is False
    assert "❌停止" in plan.gate_text()


def test_resolve_manual_replaces_auto_and_counts_verdicts(tmp_path):
    """情况一/二: 人工清单里可含不达标词或手填新词 —— 一律照搜, 不再看 FR-07 判定。"""
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["a", "manual new", "b"])
    plan = resolve(d, CAT, auto_core=["a", "b", "c"], auto_longtail=[],
                   screen_verdict={"a": "达标", "b": "不达标", "c": "达标"}, gate=2)
    assert plan.mode == MODE_MANUAL
    assert plan.core == ["a", "manual new", "b"]          # c 被人工删掉了
    assert plan.qualified == 1                            # 只有 a 达标
    assert plan.no_verdict == 1                           # manual new 无判定
    assert plan.gate_ok is True                            # 3 个 >= 门槛 2


def test_resolve_manual_below_gate_blocks(tmp_path):
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["only one"])
    plan = resolve(d, CAT, auto_core=["a", "b", "c"], auto_longtail=[], gate=2)
    assert plan.gate_ok is False
    assert "人工清单生效" in plan.gate_text()
    assert "❌停止" in plan.gate_text()


def test_resolve_manual_gate_is_satisfied_by_non_qualified_words(tmp_path):
    """情况二: 全都不达标时, 人工选/填的 2 个词就能过门槛(门槛按最终词数判)。"""
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["w1", "w2"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[],
                   screen_verdict={"w1": "不达标", "w2": "不达标"}, gate=2)
    assert plan.mode == MODE_MANUAL
    assert plan.qualified == 0
    assert plan.gate_ok is True


def test_resolve_stop_wins_over_manual(tmp_path):
    """情况二后半: 人工停止优先于人工清单(即使同时有清单也停)。"""
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["a", "b"])
    set_stop(d, CAT, True)
    plan = resolve(d, CAT, auto_core=["x", "y"], auto_longtail=[], gate=2)
    assert plan.mode == MODE_STOP
    assert plan.stopped is True
    assert "人工停止" in plan.gate_text()
    assert "❌" in plan.gate_text()


def test_resolve_non_us_auto_gate_not_applied(tmp_path):
    """非 US 站自动态保持既有行为: 门槛不适用, 有词即可(人工态仍按门槛判)。"""
    d = _mk(tmp_path)
    plan = resolve(d, CAT, auto_core=["only"], auto_longtail=[], gate=2, gate_applies=False)
    assert plan.mode == MODE_AUTO
    assert plan.gate_ok is True
    assert "不适用门槛" in plan.gate_text()
    write_words(d / core_manual_name(CAT), ["only"])
    plan2 = resolve(d, CAT, auto_core=["only"], auto_longtail=[], gate=2, gate_applies=False)
    assert plan2.mode == MODE_MANUAL
    assert plan2.gate_ok is False            # 人工态一律按门槛判(口径统一, 面板可下调门槛)


def test_source_text_reports_both_sources(tmp_path):
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["a", "new"])
    plan = resolve(d, CAT, auto_core=["a"], auto_longtail=[], screen_verdict={"a": "达标"}, gate=2)
    txt = plan.source_text()
    assert "来源=人工清单" in txt and "达标 1" in txt and "无判定 1" in txt


# ---------------------------------------------------------------- 长尾词独立生效
def test_longtail_manual_independent_of_core(tmp_path):
    d = _mk(tmp_path)
    write_words(d / longtail_manual_name(CAT), ["dog dryer for home"])
    plan = resolve(d, CAT, auto_core=["a", "b"], auto_longtail=["auto tail"], gate=2)
    assert plan.mode == MODE_AUTO                 # 只有长尾清单 -> 核心仍是自动态
    assert plan.longtail == ["dog dryer for home"]
    assert plan.longtail_manual_active is True
    assert "人工清单" in plan.longtail_text()


# ---------------------------------------------------------------- 留痕
def test_write_used_records_source_and_stats(tmp_path):
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["a", "new"])
    plan = resolve(d, CAT, auto_core=["a"], auto_longtail=["t1"],
                   screen_verdict={"a": "达标"}, gate=2)
    path = write_used(d, CAT, plan, site="美国")
    assert path.endswith(used_name(CAT))
    text = (d / used_name(CAT)).read_text(encoding="utf-8")
    assert "[核心大词]" in text and "[长尾词]" in text
    assert "来源=人工清单" in text and "门槛判定" in text
    assert "站点: 美国" in text
    assert used_head(d, CAT)[0].startswith("本轮实际生效关键词")


# ---------------------------------------------------------------- webui 写入接口
def test_set_list_manual_then_auto(tmp_path):
    d = _mk(tmp_path)
    out = set_list(d, CAT, "core", "manual", ["B", "a", "a"])
    assert out["ok"] and out["mode"] == MODE_MANUAL and out["saved"] == ["b", "a"]
    assert read_words(d / core_manual_name(CAT)) == ["b", "a"]
    out2 = set_list(d, CAT, "core", "auto")
    assert out2["ok"] and out2["mode"] == MODE_AUTO
    assert not (d / core_manual_name(CAT)).exists()


def test_set_list_rejects_over_limit(tmp_path):
    d = _mk(tmp_path)
    out = set_list(d, CAT, "core", "manual", [f"w{i}" for i in range(60)])
    assert out["ok"] is False and "最多 50 个" in out["error"]
    assert not (d / core_manual_name(CAT)).exists()


def test_set_list_rejects_bad_kind(tmp_path):
    assert set_list(_mk(tmp_path), CAT, "nope", "manual", ["a"])["ok"] is False


def test_set_stop_toggle(tmp_path):
    d = _mk(tmp_path)
    assert status(d, CAT)["stopped"] is False
    assert set_stop(d, CAT, True)["stopped"] is True
    assert (d / stop_name(CAT)).exists()
    assert status(d, CAT)["stopped"] is True
    assert set_stop(d, CAT, False)["stopped"] is False
    assert not (d / stop_name(CAT)).exists()


# ---------------------------------------------------------------- 步骤接线
def test_step09_collect_list_uses_manual_longtail(tmp_path):
    """人工长尾清单生效时: 抓取词直接用清单(跳过五维实体组装), 且不过 配件词 过滤。"""
    ctx = _make_ctx(tmp_path)
    s = Step09SearchCollect()
    out = s._build_collect_list(ctx, ["core1", "core2"], ["manual tail 1", "manual tail 2"],
                                manual_tail=True)
    assert out == ["core1", "core2", "manual tail 1", "manual tail 2"]


def test_step09_ensure_plan_blocks_on_stop(tmp_path):
    d = _mk(tmp_path)
    ctx = _make_ctx(tmp_path)
    set_stop(d, CAT, True)
    plan = resolve(Paths(ctx.config).cat_root, CAT, auto_core=["a", "b"], auto_longtail=[], gate=2)
    with pytest.raises(StepFailedError) as ei:
        Step09SearchCollect()._ensure_keyword_plan(ctx, CAT, plan, {})
    assert "人工停止" in str(ei.value)


def test_step09_ensure_plan_blocks_on_empty_core(tmp_path):
    d = _mk(tmp_path)
    ctx = _make_ctx(tmp_path)
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=2)
    with pytest.raises(StepFailedError) as ei:
        Step09SearchCollect()._ensure_keyword_plan(ctx, CAT, plan, {})
    assert "没有可用的核心词" in str(ei.value)


def test_step09_ensure_plan_blocks_on_gate_and_lists_other_candidates(tmp_path):
    d = _mk(tmp_path)
    ctx = _make_ctx(tmp_path)
    plan = resolve(d, CAT, auto_core=["a"], auto_longtail=[], gate=2)
    with pytest.raises(StepFailedError) as ei:
        Step09SearchCollect()._ensure_keyword_plan(ctx, CAT, plan, {"a": "达标", "b": "不达标"})
    msg = str(ei.value)
    assert "开品门槛未过" in msg and "b(不达标)" in msg


def test_step09_ensure_plan_passes_with_manual_list(tmp_path):
    d = _mk(tmp_path)
    ctx = _make_ctx(tmp_path)
    write_words(d / core_manual_name(CAT), ["a", "b"])
    plan = resolve(d, CAT, auto_core=["a"], auto_longtail=[], gate=2)
    Step09SearchCollect()._ensure_keyword_plan(ctx, CAT, plan, {})       # 不抛即通过


def test_step06_manual_first_wins(tmp_path):
    d = _mk(tmp_path)
    write_words(d / core_manual_name(CAT), ["manual word"])
    words, src = manual_first(d, CAT, "core", ["auto1", "auto2"])
    assert words == ["manual word"] and src == "人工清单"
    (d / core_manual_name(CAT)).unlink()          # 删掉清单 => 回自动口径
    words2, src2 = manual_first(d, CAT, "core", ["auto1"])
    assert words2 == ["auto1"] and src2 == "自动"


def test_step06_auto_core_words_reads_section(tmp_path):
    ctx = _make_ctx(tmp_path)
    d = Path(ctx.paths.cat_root)
    _write_kw_file(d, core=["dog dryer", "pet dryer"], tail=["t"])
    assert Step06Trend()._auto_core_words(ctx, CAT) == ["dog dryer", "pet dryer"]


def test_step08_load_inputs_uses_manual_core_and_gate(tmp_path):
    """报告口径必须与步骤9 一致: 人工清单生效时报告也用清单词, 门槛按词数判。"""
    ctx = _make_ctx(tmp_path, SITE="美国", MARKET="amazon.com")
    d = Path(ctx.paths.cat_root)
    _write_kw_file(d, core=["a", "b", "c"], tail=["t1"])
    (d / f"{CAT}-screen-results.json").write_text(
        '[{"keyword":"a","verdict":"达标"},{"keyword":"b","verdict":"不达标"}]', encoding="utf-8")
    write_words(d / core_manual_name(CAT), ["a", "b"])
    inputs = Step08KeywordReport()._load_inputs(ctx)
    assert inputs["core_words"] == ["a", "b"]
    plan = inputs["plan"]
    assert plan.mode == MODE_MANUAL
    assert plan.gate_ok is True
    assert inputs["non_us"] is False


def test_step08_load_inputs_auto_matches_old_behaviour(tmp_path):
    ctx = _make_ctx(tmp_path, SITE="美国", MARKET="amazon.com")
    d = Path(ctx.paths.cat_root)
    _write_kw_file(d, core=["a", "b", "c"], tail=[])
    (d / f"{CAT}-screen-results.json").write_text(
        '[{"keyword":"a","verdict":"达标"},{"keyword":"c","verdict":"达标"}]', encoding="utf-8")
    inputs = Step08KeywordReport()._load_inputs(ctx)
    assert inputs["core_words"] == ["a", "c"]        # 自动态 = FR-07 达标词(与旧版一致)
    assert inputs["plan"].mode == MODE_AUTO
    assert inputs["plan"].gate_ok is True


def test_step08_marks_manual_words_not_as_data_missing(tmp_path):
    """人工清单新增的词没跑过 FR-04/FR-05/FR-07 → 报告要如实标注。

    2026-09-29 用户问"加了新词报告是不是也要变"时补的口径: 判-定列写"人工新增(未判定)"
    而不是"数据不足"(后者会被看成筛选数据缺失的 bug), 长尾词 sheet 的说明列同理。
    """
    from steps.step08_keyword_report import _verdict_text

    assert _verdict_text("manual w", {}, {"manual w"}) == "人工新增(未判定)"
    assert _verdict_text("auto w", {"auto w": {"verdict": "达标"}}, set()) == "达标"
    assert _verdict_text("unknown", {}, set()) == "数据不足"

    df = Step08KeywordReport()._build_longtail_sheet(
        ["auto w", "manual w"], manual_tail={"manual w"}, auto_tail={"auto w"})
    notes = dict(zip(df["长尾词"], df["说明"]))
    assert notes["auto w"] == "来自 FR-04 关键词文件"
    assert "人工清单新增" in notes["manual w"]


def test_step08_manual_lists_reach_report_sheets(tmp_path):
    """人工清单要一路走到报告 sheet(长尾词保留说明 / 新增说明各不同)。"""
    ctx = _make_ctx(tmp_path, SITE="美国", MARKET="amazon.com")
    d = Path(ctx.paths.cat_root)
    _write_kw_file(d, core=["a", "b"], tail=["auto tail"])
    (d / f"{CAT}-screen-results.json").write_text(
        '[{"keyword":"a","verdict":"达标"},{"keyword":"b","verdict":"达标"}]', encoding="utf-8")
    write_words(d / longtail_manual_name(CAT), ["auto tail", "manual tail"])
    inputs = Step08KeywordReport()._load_inputs(ctx)
    plan = inputs["plan"]
    assert plan.manual_longtail == ["auto tail", "manual tail"]
    df = Step08KeywordReport()._build_longtail_sheet(
        inputs["longtail_words"], manual_tail=set(plan.manual_longtail),
        auto_tail=set(plan.auto_longtail))
    notes = dict(zip(df["长尾词"], df["说明"]))
    assert "人工清单保留" in notes["auto tail"]
    assert "人工清单新增" in notes["manual tail"]


def test_step08_run_writes_seven_sheets_end_to_end(tmp_path):
    """端到端: 步骤8 必须能把 7 个 sheet 全组装出来并写出 xlsx。

    2026-09-29 回归(实测 Dog Dryer 步骤8 秒失败): 给「方法说明」sheet 加"关键词清单"行时
    误写成 **3 元素**元组, 而该 sheet 只有 2 列 → `pd.DataFrame(rows, columns=["项目","说明"])`
    抛 `ValueError: 2 columns passed, passed data had 3 columns`, 步骤8 直接失败。
    **只有跑完整 run() 才覆盖得到** —— 单独调各 sheet 方法的单测发现不了, 所以在这里钉死。
    """
    import openpyxl

    ctx = _make_ctx(tmp_path, SITE="美国", MARKET="amazon.com")
    d = Path(ctx.paths.cat_root)
    _write_kw_file(d, core=["a", "b"], tail=["t1"])
    (d / f"{CAT}-screen-results.json").write_text(
        '[{"keyword":"a","verdict":"达标"},{"keyword":"b","verdict":"达标"}]', encoding="utf-8")
    step = Step08KeywordReport()
    step.validate_upstream(ctx)
    step.run(ctx)
    out = ctx.resolve_artifact(f"{CAT}-keyword-report.xlsx")
    assert os.path.exists(out), "报告必须写出"
    wb = openpyxl.load_workbook(out)
    assert len(wb.sheetnames) == 7, "7 个 sheet 缺一不可: %s" % wb.sheetnames


# ======================================================================
# 「仅注入」分区(2026-10-06 用户口径: "注入词只能和核心词同步吗, 不能添加吗")
#   仅注入词 = 只参与步骤10 判定、**不被步骤9 抓取**、不进门槛; 自动态下也生效。
# ======================================================================
def test_read_core_sections_legacy_file_has_no_inject(ws_tmp):
    """**向后兼容**: 没有分区头的旧文件(纯词表)全部算核心大词, 仅注入为空。"""
    d = _mk(ws_tmp)
    p = d / core_manual_name(CAT)
    p.write_text("a\nb\nc\n", encoding="utf-8")
    core, inject = read_core_sections(p)
    assert core == ["a", "b", "c"]
    assert inject == []


def test_read_core_sections_splits_two_sections(ws_tmp):
    """有 `[核心大词]` / `[仅注入]` 两分区时按分区读。"""
    d = _mk(ws_tmp)
    p = d / core_manual_name(CAT)
    p.write_text("[核心大词]\na\nb\n[仅注入]\n手填词\nc\n", encoding="utf-8")
    assert read_core_sections(p) == (["a", "b"], ["手填词", "c"])


def test_write_core_sections_dedup_and_delete(ws_tmp):
    """写回: 与核心重复的仅注入词被剔除; 两分区都空则删文件(回自动)。"""
    d = _mk(ws_tmp)
    p = d / core_manual_name(CAT)
    c, i = write_core_sections(p, ["A", "b"], ["B", " 手填 "])
    assert c == ["a", "b"] and i == ["手填"]
    assert p.read_text(encoding="utf-8") == "[核心大词]\na\nb\n[仅注入]\n手填\n"
    write_core_sections(p, [], [])
    assert not p.exists()


def test_resolve_inject_words_and_gate_ignores_them(ws_tmp):
    """仅注入词进 `plan.inject_words`, 但**不影响门槛**、模式仍可是自动。"""
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), [], ["only one", "only two"])
    plan = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[],
                   gate=2, gate_applies=True)
    assert plan.mode == MODE_AUTO                 # 只有仅注入词 -> 仍是自动态
    assert plan.core == ["auto1", "auto2"]        # 抓取/门槛只看核心大词
    assert plan.inject_only == ["only one", "only two"]
    assert plan.inject_words == ["auto1", "auto2", "only one", "only two"]
    assert plan.gate_ok is True                   # 门槛按 2 个自动核心词判, 与仅注入无关


def test_resolve_inject_words_dedup_with_core(ws_tmp):
    """核心大词与仅注入词重复时, `inject_words` 只出现一次(按核心位置)。"""
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), ["a", "b"], ["b", "c"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=1, gate_applies=True)
    assert plan.inject_words == ["a", "b", "c"]


def test_set_list_inject_keeps_core_section(ws_tmp):
    """保存仅注入清单**不动**核心分区; 清空仅注入也不动核心分区。"""
    d = _mk(ws_tmp)
    set_list(d, CAT, "core", "manual", ["c1", "c2"])
    r = set_list(d, CAT, "inject", "manual", ["x1", "x2"])
    assert r["ok"] and r["saved"] == ["x1", "x2"]
    assert read_core_sections(d / core_manual_name(CAT)) == (["c1", "c2"], ["x1", "x2"])
    # 清空仅注入 -> 文件保留(核心还在)
    r2 = set_list(d, CAT, "inject", "auto")
    assert r2["ok"] and r2["saved"] == []
    assert read_core_sections(d / core_manual_name(CAT)) == (["c1", "c2"], [])
    assert (d / core_manual_name(CAT)).exists()


def test_set_list_core_auto_keeps_inject_section(ws_tmp):
    """切回自动口径清掉核心大词时, **仅注入词必须保留**(它们是独立附加清单)。"""
    d = _mk(ws_tmp)
    set_list(d, CAT, "core", "manual", ["c1"])
    set_list(d, CAT, "inject", "manual", ["x1"])
    set_list(d, CAT, "core", "auto")
    assert read_core_sections(d / core_manual_name(CAT)) == ([], ["x1"])
    # 两个分区都空了才删文件
    set_list(d, CAT, "inject", "auto")
    assert not (d / core_manual_name(CAT)).exists()


def test_set_list_inject_limit(ws_tmp):
    """仅注入词也有数量上限(误粘贴保护)。"""
    d = _mk(ws_tmp)
    from core.keyword_lists import MAX_INJECT
    r = set_list(d, CAT, "inject", "manual", [f"w{i}" for i in range(MAX_INJECT + 1)])
    assert r["ok"] is False and "最多" in r["error"]


def test_status_reports_inject_limit(ws_tmp):
    d = _mk(ws_tmp)
    st = status(d, CAT)
    assert st["max_inject"] > 0 and st["inject_section"] == "[仅注入]"


def test_used_file_marks_inject_section(ws_tmp):
    """留痕文件也写 `[仅注入]` 分区, 便于事后核对注入口径。"""
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), ["a"], ["only"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=1, gate_applies=True)
    path = write_used(d, CAT, plan)
    text = Path(path).read_text(encoding="utf-8")
    assert "# 步骤10 注入词 2 个 = 核心大词 1 个 + 仅注入 1 个" in text
    assert "[仅注入]" in text


# ---------------------------------------------------------------- 步骤9/10 分工
def test_step09_crawl_excludes_inject_only(ws_tmp):
    """**核心断言**: 仅注入词只给步骤10 判定, 步骤9 的抓取词里**不能有**它们。"""
    from steps.step09_search_collect import Step09SearchCollect
    ctx = _make_ctx(ws_tmp)
    d = Path(ctx.paths.cat_root)
    write_core_sections(d / core_manual_name(CAT), ["core one"], ["inject only"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=1, gate_applies=True)
    assert plan.core == ["core one"]              # 抓取/门槛口径
    assert plan.inject_only == ["inject only"]
    s9 = Step09SearchCollect()
    words = s9._build_collect_list(ctx, list(plan.core), list(plan.longtail),
                                   manual_tail=plan.longtail_manual_active)
    assert "core one" in words
    assert "inject only" not in words


def test_step09_source_never_reads_inject_words():
    """源码级护栏: 步骤9 不得引用 `inject_words` / `inject_only`(只有步骤10 才注入)。"""
    import inspect
    from steps import step09_search_collect as _s9
    src = inspect.getsource(_s9)
    assert "inject_words" not in src
    assert "inject_only" not in src
