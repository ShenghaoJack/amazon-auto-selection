# -*- coding: utf-8 -*-
"""步骤16-18 P0 修复的回归测试(2026-10-14 真数据冷启动验证后补)。

每条用例都对应一个**已实测复现**的问题(验证报告见
`_session_backup/verify1618/实测对照表.md`):

  · 16 `_xc_screen` 用 glob+sorted[-1] 选错文件、sheet 缺失静默回退 active
  · 16 票数闸门"全部被挡→退回全量并集"时不计账、且不设 ATTR_MAX 上限
  · 16 同义归并把 `runs` 一律写成 1(稳定性契约失效)
  · 16 N 轮抽取全失败时静默退回内置默认属性集(实测密钥缺失即触发)
  · 17 `单遍原始.xlsx` 把 `<属性>_依据` 当交付列写出去
  · gapfill: `不适用` 被当作缺值 → 视觉覆盖已有结论
  · 18 `extra_archive_files` 漏声明 → 校验/冲突类产物不归档
  · orchestrator `reset_steps_from` 漏删 `extra_archive_files`
  · main `_SafeStream` / `harden_streams` 不覆盖 logging handler → `⏸` 吞日志
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_mod                                                    # noqa: E402
from core.config import Config                                             # noqa: E402
from core.paths import Paths                                               # noqa: E402
from core.errors import StepFailedError                                    # noqa: E402
from steps.base import StepContext                                         # noqa: E402
from steps.step16_attribute_annotation import (                            # noqa: E402
    ATTR_ROUND_MAX,
    DEFAULT_TEXT_ATTRS,
    Step16AttributeAnnotation,
)
from steps.step17_attr_annotation import _deliverable_attr_names           # noqa: E402
from steps.step18_standardization import Step18Standardization             # noqa: E402
from tools.attr_stability import gapfill                                   # noqa: E402


# --------------------------------------------------------------------- 公共
class _FakeLLM:
    def __init__(self, payloads=None):
        self.payloads = list(payloads or [])
        self.prompts: list[str] = []

    def chat_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.payloads.pop(0) if self.payloads else {}


def _ctx(tmp_dir, extra=None) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": str(tmp_dir), "DRY_RUN": True, "MARKET": "amazon.com"}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _step16(ctx) -> Step16AttributeAnnotation:
    st = Step16AttributeAnnotation()
    st.bind_cat(ctx)
    return st


def _mk_xlsx(path: Path, sheet: str, header: list[str], rows: list[list]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ------------------------------------------------- 16: 过检表定位(实测选错过)
def test_xc_screen_prefers_exact_deliverable(tmp_path):
    """目录里混着 .bak / ~$ / 排序更靠后的其它 xlsx 时, 必须选中精确交付表。"""
    ctx = _ctx(tmp_path)
    d18 = Path(ctx.paths.cat_root) / "18_标准化"
    hdr = ["是否无线", "供电方式"]
    _mk_xlsx(d18 / "t-属性标注-标准化交付.xlsx", "属性标注", hdr, [["是", "电池供电"]])
    _mk_xlsx(d18 / "t-属性标注-标准化交付.xlsx.bak-before-x.xlsx", "属性标注", hdr, [["x", "y"]])
    _mk_xlsx(d18 / "ZZ-测试-遗留项ASIN清单.xlsx", "汇总", ["ASIN", "问题"], [["B0X", "合成"]])
    _mk_xlsx(d18 / "~$t-属性标注-标准化交付.xlsx", "属性标注", hdr, [["x", "y"]])

    items = [{"说明": "无线与有线互斥",
              "左": {"属性": "是否无线", "含": ["是"]},
              "右": {"属性": "供电方式", "不得含": ["有线供电"]}}]
    _passed, _suspect, basis = _step16(ctx)._xc_screen(ctx, items)
    assert "t-属性标注-标准化交付.xlsx" in basis, basis
    assert "ZZ-测试" not in basis and ".bak" not in basis and "~$" not in basis


def test_xc_screen_rejects_file_without_deliverable_sheet(tmp_path):
    """只有"看着像"的 xlsx(缺『属性标注』sheet) → 明确判"未过检", 不得回退 active sheet。"""
    ctx = _ctx(tmp_path)
    d18 = Path(ctx.paths.cat_root) / "18_标准化"
    _mk_xlsx(d18 / "t-属性标注-标准化交付-遗留项清单.xlsx", "汇总", ["ASIN"], [["B0X"]])
    items = [{"说明": "x", "左": {"属性": "是否无线", "含": ["是"]},
              "右": {"属性": "供电方式", "不得含": ["有线供电"]}}]
    passed, _suspect, basis = _step16(ctx)._xc_screen(ctx, items)
    assert passed == []
    assert "不是标准化交付表" in basis, basis


def test_xc_screen_no_deliverable_at_all(tmp_path):
    """目录里没有任何交付表候选(如只有 `遗留项ASIN清单.xlsx`) → 全部不自动采纳。"""
    ctx = _ctx(tmp_path)
    d18 = Path(ctx.paths.cat_root) / "18_标准化"
    _mk_xlsx(d18 / "遗留项ASIN清单.xlsx", "汇总", ["ASIN"], [["B0X"]])
    items = [{"说明": "x", "左": {"属性": "是否无线", "含": ["是"]},
              "右": {"属性": "供电方式", "不得含": ["有线供电"]}}]
    passed, _suspect, basis = _step16(ctx)._xc_screen(ctx, items)
    assert passed == [] and "过检" in basis, basis


# ------------------------------------------------- 16: 闸门绕过记账 + ATTR_MAX 截断
def test_gate_bypass_is_recorded_and_capped(tmp_path, monkeypatch):
    """run() 端到端: 闸门把全部属性判偶然 → spec 记 gate_bypassed + 属性数被 ATTR_MAX 截断。

    只 mock **底层抽取**(`_extract_attrs`), 让真实的并集/池合并/票数闸门/归并都跑起来。
    """
    ctx = _ctx(tmp_path, {"ATTR_SAMPLE_MODE": "uniform", "ATTR_SPEC_RUNS": 1, "ATTR_SPEC_MIN_RUNS": 5, "ATTR_MAX": ATTR_ROUND_MAX})
    st = _step16(ctx)
    ctx.llm = lambda: _FakeLLM([{}])
    names = ["属性%02d" % i for i in range(ATTR_ROUND_MAX + 8)]
    monkeypatch.setattr(st, "_extract_attrs", lambda c, df: (list(names), {n: n for n in names}))
    monkeypatch.setattr(st, "_read_split", lambda c: None)
    monkeypatch.setattr(st, "_summarize_hints", lambda c, attrs: None)
    monkeypatch.setattr(st, "_propose_cross_checks", lambda c, attrs: None)
    st.run(ctx)
    spec = json.loads((Path(ctx.paths.cat_root) / "t-attribute-spec.json").read_text(encoding="utf-8-sig"))
    assert len(spec["attributes"]) == ATTR_ROUND_MAX, "spec 属性数必须受 ATTR_MAX 配置约束"
    assert spec.get("gate_bypassed", {}).get("bypassed") is True, "闸门没生效必须留痕"
    assert spec["gate_bypassed"]["dropped_by_cap"], "应记录被截断的属性名"
    assert spec["gate_bypassed"]["flaky"] > 0


def test_cap_attrs_keeps_previous_spec_attrs():
    """截断时**人工确认过的上一份属性优先保留**。"""
    prev = ["保留A", "保留B"]
    attrs = [{"name": n, "runs": 1} for n in (["新%02d" % i for i in range(ATTR_ROUND_MAX + 5)] + prev)]
    kept, dropped = Step16AttributeAnnotation._cap_attrs(attrs, prev, ATTR_ROUND_MAX)
    kept_names = {a["name"] for a in kept}
    assert len(kept) == ATTR_ROUND_MAX
    assert set(prev) <= kept_names, "上一份 spec(人工确认)的属性必须留下"
    assert set(dropped) and not (set(dropped) & set(prev))


# ------------------------------------------------- 16: runs 契约(同义归并保留票数)
def test_dedupe_merge_preserves_runs(tmp_path):
    ctx = _ctx(tmp_path)
    st = _step16(ctx)
    ctx.llm = lambda: _FakeLLM([{"attributes": [
        {"name": "主体材质", "aliases": ["材质"], "hint": "h"},
        {"name": "颜色", "aliases": [], "hint": "h"},
    ]}])
    attrs = [{"name": "材质", "hint": "", "runs": 2},
             {"name": "主体材质", "hint": "", "runs": 3},
             {"name": "颜色", "hint": "", "runs": 1}]
    out = st._dedupe_merge(ctx, attrs)
    got = {a["name"]: a["runs"] for a in out}
    assert got["主体材质"] == 3, "归并后应取被归并名字里的最大票数"
    assert got["颜色"] == 1
    assert out[0]["aliases"] == ["材质"]


# ------------------------------------------------- 16: 抽取全失败不得静默退默认
def test_all_extractions_failed_raises(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"ATTR_SAMPLE_MODE": "uniform"})
    st = _step16(ctx)
    monkeypatch.setattr(st, "_extract_attrs", lambda c, df: ([], {}))
    monkeypatch.setattr(st, "_read_split", lambda c: None)
    with pytest.raises(StepFailedError) as ei:
        st._extract_spec_attrs(ctx, None)
    assert "抽取" in str(ei.value) and "LLM_API_KEY" in str(ei.value)


def test_all_extractions_failed_fallback_only_when_allowed(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"ATTR_SAMPLE_MODE": "uniform", "ATTR_SPEC_ALLOW_DEFAULT_ON_FAIL": "true"})
    st = _step16(ctx)
    monkeypatch.setattr(st, "_extract_attrs", lambda c, df: ([], {}))
    monkeypatch.setattr(st, "_read_split", lambda c: None)
    ctx.llm = lambda: _FakeLLM([{}])
    out = st._extract_spec_attrs(ctx, None)
    assert {a["name"] for a in out} == set(DEFAULT_TEXT_ATTRS), "显式开关打开时才允许回退"


# ------------------------------------------------- 17: 交付列过滤 _依据
def test_deliverable_attr_names_filters_basis_and_source():
    deliverable = [{"xl_row": 2, "attributes": {"主体材质": "塑料", "主体材质_依据": "原文…",
                                                "主体材质_来源": "文本", "颜色": "黑"}}]
    assert _deliverable_attr_names(deliverable) == ["主体材质", "颜色"]


# ------------------------------------------------- gapfill: 不适用 + 不覆盖
def _cell(values):
    return {"kind": "value", "values": [{"value": v, "run": "a%d" % (i + 1)} for i, v in enumerate(values)]}


def test_find_gaps_excludes_not_applicable():
    cells = {"5|attr.吸力": _cell(["不适用", "不适用"]),
             "6|attr.吸力": _cell(["未明确", "未明确"])}
    plan = gapfill.find_gaps(cells)
    rows = {it["xl_row"] for it in plan.items}
    assert rows == {6}, "两遍都判『不适用』的格不该再问图片(不适用是结论, 不是缺值)"
    assert plan.n_gaps == 1


def test_merge_candidates_does_not_overwrite_existing_value():
    cells = {"5|attr.吸力": _cell(["不适用", "不适用"]),
             "6|attr.吸力": _cell(["未明确", "未明确"])}
    gap_results = {"5": {"追问": {"吸力": "246MPH"}, "来源": {"吸力": "图片"}},
                   "6": {"追问": {"吸力": "62m/s"}, "来源": {"吸力": "图片"}}}
    out = gapfill.merge_candidates(cells, gap_results)
    merged_rows = [str(x["行号"]) for x in out["按属性"].get("吸力", [])]
    assert "5" not in merged_rows, "已有『不适用』结论的格不得被视觉覆盖(实测被写成 246MPH)"
    assert "6" in merged_rows
    assert out["跳过已有值"] >= 1


# ------------------------------------------------- 18: 归档声明覆盖实测产物
def test_step18_extra_archive_files_cover_observed_outputs(tmp_path):
    ctx = _ctx(tmp_path)
    st = Step18Standardization()
    st.bind_cat(ctx)
    extra = set(st.extra_archive_files)
    for name in ("18_标准化/交付值校验.json", "18_标准化/规则执行与输出校验.json",
                 "18_标准化/跨属性冲突.json",
                 "18_标准化/t-数值异常校验.json", "18_标准化/t-数值异常重跑指令.md"):
        assert name in extra, "实测写出但未声明的产物必须补进 extra_archive_files: %s" % name
    # The new panel treats Step17's reports as read-only inputs. This list also
    # drives reset deletion, so keeping the old report here would delete upstream.
    assert "18_标准化/词表聚类报告.json" not in extra
    assert "18_标准化/词表聚类历史.json" not in extra
    assert "18_标准化/待确认清单.json" not in extra


# ------------------------------------------------- reset 删 extra
def test_reset_steps_from_deletes_extra_archive_files(tmp_path):
    """`reset_steps_from` 必须删掉 extra_archive_files(实测漏删导致陈旧产物跨轮残留)。"""
    ctx = _ctx(tmp_path)
    d18 = Path(ctx.paths.cat_root) / "18_标准化"
    d18.mkdir(parents=True, exist_ok=True)
    decoy = d18 / "交付值校验.json"
    decoy.write_text("{}", encoding="utf-8")
    from core.orchestrator import PipelineRunner
    runner = PipelineRunner(ctx.config)
    runner.reset_steps_from(17)
    assert not decoy.exists(), "extra_archive_files 未随 reset 删除 → 陈旧产物跨轮残留"


# ------------------------------------------------- main: 日志流加固
class _Cp936Stream:
    """模拟 Windows 默认 cp936 流: 写非 GBK 字符抛 UnicodeEncodeError。"""

    encoding = "gbk"

    def __init__(self):
        self.buf = []

    def write(self, s):
        s.encode(self.encoding)          # 不可表示 → UnicodeEncodeError
        self.buf.append(s)
        return len(s)

    def flush(self):
        pass


def test_safe_stream_survives_unicode_encode_error():
    raw = _Cp936Stream()
    safe = main_mod._SafeStream(raw)
    n = safe.write("⏸ 步骤 17 暂停(等待人工确认)")
    assert n > 0, "写入必须返回长度(调用方按长度推进)"
    assert raw.buf and "?" in raw.buf[-1], "不可表示字符应降级为 replace, 而不是整行丢失"


def test_harden_streams_repoints_logging_handler(monkeypatch):
    raw_out, raw_err = _Cp936Stream(), _Cp936Stream()
    lg = logging.getLogger("pipeline")
    old_handlers = list(lg.handlers)
    h_out = logging.StreamHandler(raw_out)
    h_err = logging.StreamHandler(raw_err)
    lg.handlers = [h_out, h_err]
    try:
        monkeypatch.setattr(sys, "stdout", raw_out)
        monkeypatch.setattr(sys, "stderr", raw_err)
        main_mod.harden_streams()
        assert isinstance(h_out.stream, main_mod._SafeStream) and h_out.stream is sys.stdout, \
            "stdout handler 必须重指到加固流(否则编码/断管保护对它无效)"
        assert isinstance(h_err.stream, main_mod._SafeStream) and h_err.stream is sys.stderr, \
            "stderr handler 必须重指到 stderr 加固流(不得串到 stdout)"
    finally:
        lg.handlers = old_handlers
        monkeypatch.undo()
