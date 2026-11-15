"""Behavioral regressions for the six Step 16 findings (2026-10-19)."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pandas as pd
import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step16_attribute_annotation import Step16AttributeAnnotation, spec_extraction_settings
from tools.attr_stability import spec_io
from tools.cat_report.engine import choose_price_bands

CAT = "Review-US-202610"
RULE = {"说明": "电动产品应使用电池", "左": {"属性": "产品类型", "含": ["电动"]},
        "右": {"属性": "供电方式", "含": ["电池"]}}
ATTRS = [{"name": "产品类型", "hint": "电动/手动"},
         {"name": "供电方式", "hint": "电池/电源"}]


def setup(tmp_path, extra=None):
    cfg = Config({"CAT": CAT, "DATA_ROOT": str(tmp_path), "DRY_RUN": True, **(extra or {})})
    ctx = StepContext(cfg, Paths(cfg))
    ctx.paths.cat_root.mkdir(parents=True, exist_ok=True)
    st = Step16AttributeAnnotation()
    st.bind_cat(ctx)
    return ctx, st


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def delivery(ctx, header, rows):
    p = ctx.paths.cat_root / "18_标准化" / f"{CAT}-属性标注-标准化交付.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "属性标注"
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(p)
    wb.close()
    return p


@pytest.mark.parametrize("change", ["add_attribute", "edit_hint", "lock_rule", "edit_values"])
def test_cross_check_generation_cannot_overwrite_concurrent_save(tmp_path, change):
    ctx, st = setup(tmp_path)
    initial = spec_io.commit_attr_spec(ctx.paths.cat_root,
                                     {"status": "pending_confirm", "attributes": ATTRS})
    rp = Path(initial["rules_path"])
    after = {}

    class LLM:
        def chat_json(self, *a, **kw):
            if change in ("add_attribute", "edit_hint"):
                spec = spec_io.load_attr_spec(initial["path"])
                rev = spec_io.spec_revision(spec)
                if change == "add_attribute":
                    spec["attributes"].append({"name": "人工新增", "hint": "人工说明"})
                else:
                    spec["attributes"][0]["hint"] = "人工新说明"
                spec_io.commit_attr_spec(ctx.paths.cat_root, spec, expected_revision=rev)
            else:
                with spec_io.spec_lock(ctx.paths.cat_root):
                    rules = read(rp)
                    if change == "lock_rule":
                        rules["cross_checks"] = {"_锁定": True, "_来源": "人工", "互斥": []}
                    else:
                        rules["attributes"][0]["params"]["values"] = ["人工新值"]
                    spec_io.write_json_atomic(rp, rules)
            after["spec"] = Path(initial["path"]).read_bytes()
            after["rules"] = rp.read_bytes()
            return {"互斥": [RULE]}

    ctx._llm = LLM()
    st._xc_screen = lambda c, items: (items, {}, "isolated generation test")
    st._propose_cross_checks(ctx, ATTRS)
    assert Path(initial["path"]).read_bytes() == after["spec"]
    assert rp.read_bytes() == after["rules"]
    spec_io.assert_rule_alignment(read(initial["path"]), read(rp))


def test_unchanged_cross_check_generation_writes_atomically(tmp_path, monkeypatch):
    ctx, st = setup(tmp_path)
    initial = spec_io.commit_attr_spec(ctx.paths.cat_root,
                                     {"status": "pending_confirm", "attributes": ATTRS})
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: {"互斥": [RULE]})
    st._xc_screen = lambda c, items: (items, {}, "isolated generation test")
    st._propose_cross_checks(ctx, ATTRS)
    rp = Path(initial["rules_path"])
    assert read(rp)["cross_checks"]["互斥"] == [RULE]
    old = rp.read_bytes()
    monkeypatch.setattr(spec_io.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        st._propose_cross_checks(ctx, ATTRS)
    assert rp.read_bytes() == old


@pytest.mark.parametrize("header,rows,reason", [
    (["产品类型"], [["电动"]], "缺属性列"),
    (["产品类型", "供电方式"], [], "无数据行"),
    (["产品类型", "供电方式"], [["电动", "未明确"]], "无有效适用行"),
    (["产品类型", "供电方式"], [["手动", "电池"]], "无有效适用行"),
])
def test_unchecked_constraints_never_pass(tmp_path, header, rows, reason):
    ctx, st = setup(tmp_path)
    delivery(ctx, header, rows)
    passed, suspect, basis = st._xc_screen(ctx, [RULE])
    assert passed == [] and suspect == {}
    assert reason in basis


@pytest.mark.parametrize("value,accepted", [("电池", True), ("电源", False)])
def test_applicable_constraint_is_actually_checked(tmp_path, value, accepted):
    ctx, st = setup(tmp_path)
    delivery(ctx, ["产品类型", "供电方式"], [["电动", value]])
    passed, suspect, _ = st._xc_screen(ctx, [RULE])
    assert bool(passed) is accepted
    assert suspect == ({} if accepted else {id(RULE): 1})


def test_unchecked_constraint_is_labelled_in_suggestion_and_not_installed(tmp_path):
    ctx, st = setup(tmp_path)
    initial = spec_io.commit_attr_spec(ctx.paths.cat_root,
                                     {"status": "pending_confirm", "attributes": ATTRS})
    delivery(ctx, ["产品类型"], [["电动"]])
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: {"互斥": [RULE]})
    st._propose_cross_checks(ctx, ATTRS)
    assert "cross_checks" not in read(initial["rules_path"])
    suggestion = read(ctx.paths.cat_root / "16_产品属性标注" / "跨属性约束建议.json")
    assert suggestion["候选"][0]["校验"].startswith("未过检")


# 2026-11-11 用户口径：抽取遍数写死为 3、固定价格段模式；ATTR_SPEC_RUNS / ATTR_SAMPLE_MODE
# 配置一律忽略。票数门槛仍读 ATTR_SPEC_MIN_RUNS（或旧键 ATTR_BAND_MIN_RUNS），且不超过 3。
@pytest.mark.parametrize("config,expected", [
    ({}, ("price_band", 3, 3)),
    ({"ATTR_SPEC_RUNS": 7}, ("price_band", 3, 3)),
    ({"ATTR_SPEC_RUNS": 7, "ATTR_SPEC_MIN_RUNS": 7}, ("price_band", 3, 3)),
    ({"ATTR_BAND_ROUNDS": 3, "ATTR_BAND_MIN_RUNS": 3}, ("price_band", 3, 3)),
    ({"ATTR_SPEC_RUNS": 7, "ATTR_BAND_ROUNDS": 3, "ATTR_BAND_MIN_RUNS": 1}, ("price_band", 3, 1)),
    ({"ATTR_SAMPLE_MODE": "uniform", "ATTR_BAND_ROUNDS": 3}, ("price_band", 3, 3)),
])
def test_runs_fixed_at_three_regardless_of_config(config, expected):
    info = spec_extraction_settings(config)
    assert (info["mode"], info["runs"], info["min_runs"]) == expected
    assert info["runs_overridden"] is False


def test_real_price_path_runs_three_rounds_even_if_config_says_seven(tmp_path, monkeypatch):
    ctx, st = setup(tmp_path, {"ATTR_SPEC_RUNS": 7})
    ctx._llm = SimpleNamespace(config=ctx.config)
    monkeypatch.setattr("steps.step16_attribute_annotation.choose_price_bands",
                        lambda p: {"bands": [{"lo": 0, "hi": 20}, {"lo": 20, "hi": 100}]})
    monkeypatch.setattr("tools.attr_stability.global_merge.global_merge", lambda llm, names: {n: n for n in names})
    calls = []

    def extract(c, df, **_kw):
        calls.append(df["价格"].tolist())
        names = ["材质"] + (["稀有属性"] if len(calls) <= 1 else [])
        return names, {n: n + "(说明)" for n in names}

    st._extract_attrs = extract
    attrs = st._extract_spec_attrs(ctx, pd.DataFrame({"标题": ["t"] * 4, "价格": [10, 11, 30, 31]}))
    # 1 次单轮上限探测 + 两个价格段 × 3 轮（配置里的 7 被忽略）
    assert len(calls) == 1 + 2 * 3
    assert [a["name"] for a in attrs] == ["材质"]
    assert attrs[0]["runs"] == 6


def test_uniform_synonyms_vote_once_per_original_round_before_threshold(tmp_path):
    ctx, st = setup(tmp_path, {"ATTR_SAMPLE_MODE": "uniform", "ATTR_SPEC_RUNS": 5})
    batches = iter([["材质", "主体材质", "颜色"], ["主体材质", "颜色"],
                    ["材质", "颜色"], ["主体材质", "颜色"], ["颜色"]])

    def extract(*a):
        names = next(batches)
        return names, {n: n + "(说明)" for n in names}

    st._extract_attrs = extract
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: {"attributes": [
        {"name": "材质", "hint": "合并说明", "aliases": ["主体材质"]},
        {"name": "颜色", "hint": "颜色说明", "aliases": []}]})
    attrs = st._extract_spec_attrs(ctx, pd.DataFrame({"标题": ["t"]}))
    assert {a["name"]: a["runs"] for a in attrs} == {"材质": 4, "颜色": 5}


@pytest.mark.parametrize("groups", [
    [{"name": "材质", "aliases": []}],  # missing original name
    [{"name": "材质", "aliases": ["颜色"]}, {"name": "颜色", "aliases": []}],  # double vote
    [{"name": "虚构", "aliases": []}, {"name": "材质", "aliases": ["颜色"]}],
    [{"name": "材质", "aliases": "颜色"}],
    [{"name": "材质", "aliases": 3}],
])
def test_invalid_synonym_coverage_falls_back_without_losing_source_names(tmp_path, groups):
    ctx, st = setup(tmp_path)
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: {"attributes": groups})
    assert st._dedupe_merge(ctx, [{"name": "材质", "runs": 3}, {"name": "颜色", "runs": 2}],
                            extractions=[[{"name": "材质"}], [{"name": "颜色"}]]) == []


def test_identical_uniform_rerun_does_not_adopt_one_vote_noise(tmp_path, monkeypatch):
    ctx, st = setup(tmp_path, {"ATTR_SAMPLE_MODE": "uniform", "ATTR_SPEC_RUNS": 5})
    counter = [0]

    def extract(*a):
        names = ["材质"] + (["颜色"] if counter[0] % 5 == 0 else [])
        counter[0] += 1
        return names, {n: n + "(说明)" for n in names}

    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: {})
    st._extract_attrs = extract
    st._read_split = lambda c: pd.DataFrame({"标题": ["t"]})
    st._summarize_hints = lambda *a: None
    st._propose_cross_checks = lambda *a: None
    for _ in range(2):
        st.run(ctx)
        spec = read(ctx.resolve_artifact(st.artifacts[0]))
        assert {a["name"]: a["runs"] for a in spec["attributes"]} == {"材质": 5}
        assert not spec.get("gate_bypassed")


@pytest.mark.parametrize("prices", [[20.0], [20.0] * 4, [10.0, 20.0, 30.0, 40.0]])
def test_small_price_population_remains_covered_once(prices):
    info = choose_price_bands(prices)
    assert len(info["bands"]) == 1
    for price in prices:
        assert sum(b["lo"] <= price < b["hi"] for b in info["bands"]) == 1


def test_small_population_runs_price_path_without_type_error(tmp_path, monkeypatch):
    ctx, st = setup(tmp_path)
    ctx._llm = SimpleNamespace(config=ctx.config)
    st._extract_attrs = lambda c, df: (["材质"], {"材质": "材质(说明)"})
    attrs = st._extract_spec_attrs(ctx, pd.DataFrame({"标题": ["t"] * 4, "价格": [20.0] * 4}))
    assert attrs[0]["name"] == "材质" and attrs[0]["runs"] == 3


def test_stale_attributes_before_proposal_never_call_model(tmp_path):
    ctx, st = setup(tmp_path)
    spec_io.commit_attr_spec(ctx.paths.cat_root, {"status": "pending_confirm",
        "attributes": ATTRS + [{"name": "人工新增", "hint": "新说明"}]})
    def unexpected(*a, **kw):
        pytest.fail("stale attributes reached the model")
    ctx._llm = SimpleNamespace(chat_json=unexpected)
    st._propose_cross_checks(ctx, ATTRS)


def test_server_reports_the_same_effective_settings(tmp_path):
    from webui.server import Handler
    values = {"ATTR_BAND_ROUNDS": 3, "ATTR_BAND_MIN_RUNS": 3, "ATTR_SPEC_RUNS": 7}
    handler = Handler.__new__(Handler)
    handler._cat_cfg_values = lambda q: values
    assert handler._spec_runs_info(None) == spec_extraction_settings(values)


def test_raw_same_name_duplicates_cannot_vote_twice(tmp_path):
    ctx, st = setup(tmp_path)
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: {"attributes": [
        {"name": "材质", "aliases": []}, {"name": "颜色", "aliases": []}]})
    attrs = st._dedupe_merge(ctx, [{"name": "材质", "runs": 2}, {"name": "颜色", "runs": 1}],
        extractions=[[{"name": "材质"}, {"name": "材质"}], [{"name": "颜色"}]])
    assert {a["name"]: a["runs"] for a in attrs} == {"材质": 1, "颜色": 1}


def test_tuning_frontend_displays_the_effective_vote_unit():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run([node, str(Path(__file__).with_name("step16_tuning_frontend.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
