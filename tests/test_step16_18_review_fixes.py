"""输入更新、完整性闸门、模型输出及规则确认的端到端回归。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

from core.config import Config
from core.errors import StepDataError
from core.paths import Paths
from steps.base import StepContext
from steps.step17_attr_annotation import Step17AttrAnnotation, UPSTREAM_SPLIT
from steps.step18_standardization import Step18Standardization
from tools.attr_stability import AttrSpec, RuleSource
from tools.attr_stability.attr_runner import CallableProducer, run_round
from tools.attr_stability.converge import apply_classification
from tools.attr_stability.spec import resolve_rules_file


def _rules():
    return RuleSource(attrs={"材质": AttrSpec(name="材质", values=["竹制", "木制"])})


def _row(rid=2):
    return {
        "xl_row": rid,
        "category_check": {"判定": "属于本类目"},
        "attributes": {"材质": "竹制", "材质_来源": "标题", "材质_依据": "bamboo"},
        "尺寸_拆列": {"长": "", "宽": "", "高": ""},
        "尺寸_依据": "无",
        "suitable_scene_规范串": "",
        "词表外词": [],
        "pack_count_数值": None,
        "跨列纠错": [],
        "冲突": [],
    }


@pytest.mark.parametrize("field", ["材质", "材质_依据"])
def test_incomplete_runs_never_enter_delivery(field):
    row = _row()
    del row["attributes"][field]
    producer = SimpleNamespace(produce=lambda *args, **kwargs: {2: row})
    result = run_round(
        producer,
        _rules(),
        n_runs=2,
        max_retry=0,
        input_rows=[{"xl_row": 2}],
        strict_gate=False,
        require_complete=True,
        require_basis=True,
        log=lambda *_: None,
    )
    assert not result.deliverable
    assert result.rejected == ["a1", "a2"]
    gate = result.to_dict()["gates"]["a1"]
    assert gate["required_errors"] > 0 and not gate["delivery_accepted"]


def test_missing_source_is_filled_without_changing_value_or_evidence():
    row = _row()
    del row["attributes"]["材质_来源"]
    result = run_round(
        SimpleNamespace(produce=lambda *args, **kwargs: {2: copy.deepcopy(row)}),
        _rules(), n_runs=2, max_retry=0, input_rows=[{"xl_row": 2}],
        strict_gate=False, require_complete=True, require_basis=True,
        log=lambda *_: None,
    )
    assert result.passed == ["a1", "a2"]
    assert result.deliverable[0]["attributes"] == {
        "材质": "竹制", "材质_来源": "", "材质_依据": "bamboo"}


def test_incomplete_run_is_repaired_before_delivery():
    calls = []

    def produce(tag, retry_brief=None):
        row = _row()
        calls.append((tag, retry_brief))
        if retry_brief is None:
            del row["attributes"]["材质_依据"]
        return {2: row}

    result = run_round(
        SimpleNamespace(produce=produce),
        _rules(),
        n_runs=2,
        input_rows=[{"xl_row": 2}],
        strict_gate=False,
        require_complete=True,
        require_basis=True,
        log=lambda *_: None,
    )
    assert result.passed == ["a1", "a2"]
    assert len(calls) == 4
    assert result.deliverable[0]["attributes"]["材质_依据"] == "bamboo"


def test_soft_errors_remain_nonblocking():
    row = _row()
    row["新过程字段"] = "允许记录"
    row["attributes"]["材质"] = "竹木"
    result = run_round(
        SimpleNamespace(produce=lambda *a, **k: {2: row}),
        _rules(),
        n_runs=2,
        input_rows=[{"xl_row": 2}],
        strict_gate=False,
        require_complete=True,
        require_basis=True,
        log=lambda *_: None,
    )
    assert result.passed == ["a1", "a2"]
    assert result.deliverable


def test_source_key_is_required_but_empty_label_remains_compatible():
    row = _row()
    row["attributes"]["材质_来源"] = ""
    result = run_round(
        SimpleNamespace(produce=lambda *a, **k: {2: row}),
        _rules(),
        n_runs=2,
        strict_gate=False,
        require_complete=True,
        require_basis=True,
        log=lambda *_: None,
    )
    assert result.deliverable


@pytest.mark.parametrize("rid", [3, None])
def test_wrong_or_missing_input_row_is_blocked(rid):
    rows = {} if rid is None else {rid: _row(rid)}
    result = run_round(
        SimpleNamespace(produce=lambda *a, **k: rows),
        _rules(),
        n_runs=2,
        max_retry=0,
        input_rows=[{"xl_row": 2}],
        strict_gate=False,
        require_complete=True,
        log=lambda *_: None,
    )
    assert not result.deliverable


def test_retry_exception_cannot_restore_rejected_old_result():
    def produce(tag, retry_brief=None):
        if retry_brief:
            raise RuntimeError("retry failed")
        row = _row()
        del row["attributes"]["材质_依据"]
        return {2: row}

    result = run_round(
        SimpleNamespace(produce=produce),
        _rules(),
        n_runs=2,
        max_retry=1,
        strict_gate=False,
        require_complete=True,
        require_basis=True,
        log=lambda *_: None,
    )
    assert not result.passed and not result.deliverable


def _producer(tmp_path, calls, **changes):
    options = dict(
        rows=[{"xl_row": 2, "标题": "旧标题"}],
        out_dir=str(tmp_path),
        prefix="a",
        prompt_text="old prompt",
        workers=1,
        cache_context={"model": "model-a", "rules": {"材质": ["竹制"]}},
    )
    options.update(changes)
    return CallableProducer(
        fn=lambda p: (calls.append(p) or {"rows": [{"xl_row": 2}]}), **options
    )


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_text": "new prompt"},
        {"cache_context": {"model": "model-b"}},
        {"cache_context": {"rules": {"材质": ["木制"]}}},
        {"rows": [{"xl_row": 2, "标题": "新标题"}]},
        {"input_fields": ["xl_row", "标题"]},
    ],
)
def test_completed_cache_invalidates_on_judgment_changes(tmp_path, change):
    calls = []
    _producer(tmp_path, calls).produce("a1")
    _producer(tmp_path, calls).produce("a1")
    assert len(calls) == 1
    _producer(tmp_path, calls, **change).produce("a1")
    assert len(calls) == 2


def test_same_count_wrong_cached_ids_are_not_reused(tmp_path):
    calls = []
    producer = _producer(tmp_path, calls)
    (tmp_path / "a1.json").write_text(
        json.dumps({"input_key": producer._input_key(), "rows": [{"xl_row": 99}]}),
        encoding="utf-8",
    )
    assert set(producer.produce("a1")) == {2}
    assert len(calls) == 1


def test_partial_cache_is_invalid_after_prompt_change(tmp_path):
    calls = []
    old = _producer(tmp_path, calls)
    old._save_ckpt(
        old._ckpt_path("a1"), old._input_key(), {2: {"xl_row": 2, "old": True}}
    )
    new = _producer(tmp_path, calls, prompt_text="new prompt")
    result = new.produce("a1")
    assert len(calls) == 1 and "old" not in result[2]


def test_basis_switch_is_per_call_without_global_leak():
    row = _row()
    del row["attributes"]["材质_依据"]
    producer = SimpleNamespace(produce=lambda *a, **k: {2: row})
    relaxed = run_round(
        producer,
        _rules(),
        n_runs=2,
        max_retry=0,
        strict_gate=False,
        require_complete=True,
        require_basis=False,
        log=lambda *_: None,
    )
    required = run_round(
        producer,
        _rules(),
        n_runs=2,
        max_retry=0,
        strict_gate=False,
        require_complete=True,
        require_basis=True,
        log=lambda *_: None,
    )
    assert relaxed.deliverable and not required.deliverable


def _ctx(tmp_path, **values):
    return StepContext(
        Config(
            {
                "CAT": "Demo-US-202610",
                "DATA_ROOT": str(tmp_path),
                "DRY_RUN": True,
                **values,
            }
        ),
        Paths(Config({"CAT": "Demo-US-202610", "DATA_ROOT": str(tmp_path)})),
    )


def _split(ctx, title, sheet="AI已补充"):
    path = Path(ctx.resolve_artifact(UPSTREAM_SPLIT))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.active.title = sheet
    wb.active.append(["变体ASIN", "标题", "五点描述", "详细参数", "图片链接", "SKU"])
    wb.active.append(["B000C91625", title, "bamboo", "18x10x6cm",
                      "https://example.com/a.jpg", "SKU-1"])
    wb.save(path)
    wb.close()


def test_upstream_changes_refresh_input_and_cache_key(tmp_path):
    ctx = _ctx(tmp_path)
    step = Step17AttrAnnotation()
    out = ctx.resolve_artifact("17_属性标注")
    _split(ctx, "旧标题")
    rows = step._load_rows(ctx, out)
    key = _producer(tmp_path, [], rows=rows)._input_key()
    assert step._load_rows(ctx, out) == rows
    _split(ctx, "新标题")
    current = step._load_rows(ctx, out)
    assert current[0]["标题"] == "新标题"
    assert _producer(tmp_path, [], rows=current)._input_key() != key
    saved = json.loads((Path(out) / "input.json").read_text(encoding="utf-8"))
    assert saved["rows_sha256"] == step._rows_digest(current)


def test_legacy_snapshot_is_refreshed_unless_explicit_replay(tmp_path):
    ctx = _ctx(tmp_path)
    _split(ctx, "当前标题")
    out = Path(ctx.resolve_artifact("17_属性标注"))
    out.mkdir()
    snap = out / "input.json"
    old = {"rows": [{"xl_row": 2, "标题": "历史标题"}]}
    snap.write_text(json.dumps(old), encoding="utf-8")
    replay = _ctx(tmp_path, ATTR_STAB_INPUT_MODE="snapshot")
    assert Step17AttrAnnotation()._load_rows(replay, str(out))[0]["标题"] == "历史标题"
    assert Step17AttrAnnotation()._load_rows(ctx, str(out))[0]["标题"] == "当前标题"


def test_tampered_snapshot_is_rebuilt_from_upstream(tmp_path):
    ctx = _ctx(tmp_path)
    _split(ctx, "可信标题")
    step = Step17AttrAnnotation()
    out = ctx.resolve_artifact("17_属性标注")
    step._load_rows(ctx, out)
    snap = Path(out) / "input.json"
    data = json.loads(snap.read_text(encoding="utf-8"))
    data["rows"][0]["标题"] = "改坏的快照"
    snap.write_text(json.dumps(data), encoding="utf-8")
    assert step._load_rows(ctx, out)[0]["标题"] == "可信标题"


def test_wrong_sheet_never_falls_back_to_another_sheet(tmp_path):
    ctx = _ctx(tmp_path)
    _split(ctx, "wrong", sheet="其他页")
    with pytest.raises(StepDataError, match="工作表"):
        Step17AttrAnnotation()._load_rows(ctx, ctx.resolve_artifact("17_属性标注"))


def test_vision_fingerprint_tracks_urls_and_definitions():
    plan = SimpleNamespace(items=[{"xl_row": 2, "待补属性": ["材质"]}])
    rows = [{"xl_row": 2, "图片链接": "https://example.com/a.jpg"}]
    make = Step17AttrAnnotation._vision_input_key
    key = make(plan, "图片链接", rows=rows, context={"model": "a"})
    assert key != make(
        plan,
        "图片链接",
        rows=[{**rows[0], "图片链接": "b.jpg"}],
        context={"model": "a"},
    )
    assert key != make(plan, "图片链接", rows=rows, context={"model": "b"})


def test_only_valid_canonical_classification_is_returned():
    out = apply_classification(
        {},
        {
            "材质": {
                "raw": "竹制",
                "bad": "石头",
                "new": "新增: 合金",
                "typed": ["竹制"],
            },
            "陌生属性": {"raw": "竹制"},
        },
        {"材质": ["竹制"]},
    )
    assert out["合法映射"] == {"材质": {"raw": "竹制"}}
    assert len(out["非法输出"]) == 3
    assert out["新增建议"]["材质"] == {"合金": 1}


def _rule_file(root, name="demo.rules.json", confirmed=False):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(
        json.dumps(
            {
                "attributes": [
                    {
                        "name": "材质",
                        "rule": "enum",
                        "params": {"values": ["竹制", "木制"]},
                    }
                ],
                "vocab_status": "confirmed" if confirmed else "pending_confirm",
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("layout", ["flat", "categories", "shared"])
def test_rules_application_confirmation_and_web_share_one_path(tmp_path, layout):
    from webui.server import Handler

    ctx = _ctx(tmp_path)
    root = Path(ctx.paths.cat_root)
    directory = (
        root
        if layout == "flat"
        else (
            root / "categories"
            if layout == "categories"
            else tmp_path / "_catalog" / "Demo"
        )
    )
    source = _rule_file(directory)
    step = Step17AttrAnnotation()
    step._cat = ctx.config.cat
    before = source.read_bytes()
    assert Path(step._rules_json_path(ctx)) == source
    assert Handler._std_rules_path(ctx.config.cat, str(tmp_path)) == source
    assert step._set_vocab_status(ctx, "confirmed") == 1
    path = Path(step._rules_json_path(ctx))
    assert path == Handler._std_rules_path(ctx.config.cat, str(tmp_path))
    d, stem = step._resolve_rules(ctx)
    assert path == Path(d) / (stem + ".rules.json")
    assert step._vocab_status(ctx) == "confirmed"
    assert RuleSource.load(d, stem).attrs["材质"].values == ["竹制", "木制"]
    if layout == "shared":
        assert source.read_bytes() == before
        assert path.parent == root


def test_ambiguous_rules_are_rejected_not_guessed(tmp_path):
    ctx = _ctx(tmp_path)
    root = Path(ctx.paths.cat_root)
    _rule_file(root, "a.rules.json")
    _rule_file(root, "b.rules.json")
    with pytest.raises(StepDataError, match="歧义"):
        Step17AttrAnnotation()._vocab_status(ctx)
    with pytest.raises(ValueError, match="歧义"):
        resolve_rules_file(str(root), writable=True)


def test_out_of_vocabulary_value_is_reported_and_preserved_in_delivery(tmp_path):
    ctx = _ctx(tmp_path)
    _rule_file(Path(ctx.paths.cat_root))
    step = Step18Standardization()
    step._cat = ctx.config.cat
    res = SimpleNamespace(
        cells={},
        deliverable=[
            {"xl_row": 2, "attributes": {"材质": "石头", "材质_来源": "标题"}}
        ],
    )
    std = {}
    report = step._validate_delivery_values(_rules(), res, std)
    assert len(report["词表外保留"]) == 1
    assert report["词表外保留"][0]["原值"] == "石头"
    assert report["词表外保留"][0]["处理"] == "保留原值"
    assert (2, "材质") not in std
    out = Path(ctx.resolve_artifact("18_标准化"))
    out.mkdir()
    step._write_deliverables(
        ctx, str(out), [{"xl_row": 2, "标题": "测试"}], res, std, raw_too=False
    )
    wb = openpyxl.load_workbook(out / f"{step._cat}-属性标注-标准化交付.xlsx")
    try:
        assert wb["属性标注"].cell(2, 2).value == "石头"
    finally:
        wb.close()


def test_invalid_classification_is_not_used_for_standardization(tmp_path):
    ctx = _ctx(tmp_path)
    _rule_file(Path(ctx.paths.cat_root))
    step = Step17AttrAnnotation()
    step._cat = ctx.config.cat
    res = SimpleNamespace(
        cells={
            "2|attr.材质": {
                "kind": "value",
                "mode": "raw",
                "values": [{"value": "raw", "by": ["a1", "a2"]}],
            }
        },
        deliverable=[],
    )
    ctx._llm = SimpleNamespace(
        chat_json=lambda *a, **kw: {"归类": {"材质": {"raw": "石头"}}}
    )
    result = step._converge_standardize(ctx, _rules(), res)
    assert "石头" not in result.values()
    assert not list(Path(ctx.paths.cat_root).glob("*.learned-map.json"))
    unchanged = copy.deepcopy(res)
    ctx._llm = SimpleNamespace(
        chat_json=lambda *a, **kw: {"归类": {"材质": {"raw": "竹制"}}}
    )
    result = step._converge_standardize(ctx, _rules(), unchanged)
    assert result[(2, "材质")] == "竹制"


def test_delivery_guard_respects_declared_multi_separator():
    rules = _rules()
    rules.attrs["材质"].shape = "multi"
    res = SimpleNamespace(
        deliverable=[{"xl_row": 2, "attributes": {"材质": "竹制/木制"}}]
    )
    definitions = {
        "attributes": [{"name": "材质", "params": {"value_separators": ["/"]}}]
    }
    std = {}
    report = Step18Standardization()._validate_delivery_values(
        rules, res, std, definitions=definitions
    )
    assert not report["词表外保留"] and not std


def test_delivery_guard_does_not_close_explicitly_open_vocabulary():
    rules = _rules()
    rules.attrs["材质"].vocab_policy = "open"
    res = SimpleNamespace(
        deliverable=[{"xl_row": 2, "attributes": {"材质": "新自由文本"}}]
    )
    std = {}
    report = Step18Standardization()._validate_delivery_values(rules, res, std)
    assert report["已校验格数"] == 0 and not std


def test_step17_confirm_step18_pipeline_with_fake_model(tmp_path, monkeypatch):
    """实际执行三段入口与 Web 保存，核对 JSON 和最终 XLSX；模型完全离线。"""
    from webui import server

    ctx = _ctx(tmp_path, DRY_RUN=False, ATTR_STAB_RUNS=2, ATTR_STAB_MAX_RETRY=1)
    _split(ctx, "bamboo board")
    rules_path = _rule_file(Path(ctx.paths.cat_root) / "categories")
    spec_path = Path(ctx.resolve_artifact(f"{ctx.config.cat}-attribute-spec.json"))
    spec_path.write_text(
        json.dumps(
            {
                "cat": ctx.config.cat,
                "status": "confirmed",
                "attributes": [{"name": "材质", "hint": "从标题中的 bamboo 判断竹制"}],
            }
        ),
        encoding="utf-8",
    )
    calls = []
    ctx._llm = SimpleNamespace(
        chat_json=lambda p, **kw: (calls.append(p) or {"rows": [_row()]})
    )
    annotation = Step17AttrAnnotation()
    annotation.validate_upstream(ctx)
    annotation.run(ctx)
    raw = json.loads(
        Path(ctx.resolve_artifact("17_属性标注/稳定性结果.json")).read_text(
            encoding="utf-8"
        )
    )
    assert raw["passed"] == ["a1", "a2"]
    assert all(g["required_errors"] == 0 for g in raw["gates"].values())
    assert raw["deliverable"][0]["xl_row"] == 2
    assert annotation._vocab_status(ctx) == "pending_confirm"
    handler = server.Handler.__new__(server.Handler)
    handler._std_vocab_cat = lambda _: (ctx.config.cat, str(tmp_path))
    monkeypatch.setattr(server, "push_cat", lambda *a: None)
    saved = handler._api_std_vocab_save(
        {"attributes": [{"属性": "材质", "规范值": ["竹制", "木制"]}]}
    )
    assert saved["vocab_status"] == "confirmed"
    assert Path(annotation._rules_json_path(ctx)) == rules_path
    standardization = Step18Standardization()
    standardization.validate_upstream(ctx)
    standardization.run(ctx)
    final_dir = Path(ctx.resolve_artifact("18_标准化"))
    report = json.loads((final_dir / "交付值校验.json").read_text(encoding="utf-8"))
    assert report["已校验格数"] == 1 and not report["词表外保留"]
    wb = openpyxl.load_workbook(
        final_dir / f"{ctx.config.cat}-属性标注-标准化交付.xlsx"
    )
    try:
        headers = [cell.value for cell in wb["属性标注"][1]]
        column = headers.index("材质") + 1
        assert wb["属性标注"].cell(2, column).value == "竹制"
        assert wb["来源"].cell(2, column).value == "标题"
        assert wb["依据"].cell(2, column).value == "bamboo"
    finally:
        wb.close()
    assert len(calls) == 2
