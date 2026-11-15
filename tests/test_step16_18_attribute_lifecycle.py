"""Regression and offline acceptance of attribute editing through steps 16–18."""
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

from core.config import Config
from core.errors import StepDataError, WaitingForHuman
from core.paths import Paths
from steps.base import StepContext
from steps.step16_attribute_annotation import Step16AttributeAnnotation
from steps.step17_attr_annotation import Step17AttrAnnotation, UPSTREAM_SPLIT
from steps.step18_standardization import Step18Standardization
from tools.attr_stability import spec_io
from tools.attr_stability.spec import resolve_rules_file
from webui import server


CAT = "Lifecycle-US-202610"
VALUES = {"材质": ["竹制", "木制"], "雕刻文字": ["支持", "不支持"], "包装形式": ["盒装", "散装"]}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _setup(tmp_path, monkeypatch, shared=False):
    cfg = Config({"CAT": CAT, "DATA_ROOT": str(tmp_path / "data"), "DRY_RUN": False,
                  "ATTR_SPEC_RUNS": 2, "ATTR_SPEC_MIN_RUNS": 2,
                  "ATTR_STAB_RUNS": 2, "ATTR_STAB_WORKERS": 1,
                  "ATTR_STAB_MAX_RETRY": 0, "ATTR_STAB_GEN_VOCAB": True,
                  "ATTR_STAB_REQUIRE_BASIS": True, "ATTR_XC_AUTO": False,
                  "ATTR_STAB_AUTO_ONBOARD": False, "LLM_USAGE_LOG": False})
    ctx = StepContext(cfg, Paths(cfg))
    root = Path(ctx.paths.cat_root)
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(f'CAT: "{CAT}"\nDATA_ROOT: "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "push_cat", lambda *a, **kw: None)
    h = server.Handler.__new__(server.Handler)
    body = {"config_file": f"data/{CAT}/config.yaml"}
    upstream = Path(ctx.resolve_artifact(UPSTREAM_SPLIT))
    upstream.parent.mkdir(parents=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AI已补充"
    ws.append(["标题", "五点描述", "详细参数", "图片链接", "SKU"])
    ws.append(["bamboo board engraved gift box", "engraving supported", "box", "", "sku-a"])
    ws.append(["wood board plain bulk", "no engraving", "bulk", "", "sku-b"])
    wb.save(upstream)
    wb.close()
    inherited = None
    if shared:
        inherited = tmp_path / "data" / "_catalog" / "Lifecycle" / "Lifecycle-DE-202609-attribute-spec.json"
        _write(inherited, {"cat": "Lifecycle-DE-202609", "status": "confirmed", "attributes": [
            {"name": "材质", "hint": "人工定义：从标题判断材质", "modified": True, "_市场": ["DE"], "aliases": ["主体材质"]}]})
        _write(inherited.parent / "shared.rules.json", {"attributes": [{"name": "材质", "rule": "enum", "params": {}}], "vocab_status": "confirmed"})
    return ctx, root, h, body, inherited


class _FakeModel:
    def __init__(self):
        self.calls = []

    def chat_json(self, prompt, **kw):
        self.calls.append(prompt)
        if '"rows"' in prompt:
            rows = []
            for i in range(2):
                attrs = {}
                for name, values in VALUES.items():
                    attrs.update({name: values[i], name + "_来源": "标题", name + "_依据": "bamboo engraved box" if i == 0 else "wood plain bulk"})
                rows.append({"xl_row": i + 2, "category_check": {"判定": "属于本类目"}, "attributes": attrs,
                             "尺寸_拆列": {"长": "", "宽": "", "高": ""}, "尺寸_依据": "无",
                             "suitable_scene_规范串": "", "词表外词": [], "pack_count_数值": None,
                             "跨列纠错": [], "冲突": []})
            return {"rows": rows}
        if '"summary"' in prompt:
            return {"attributes": [{"name": "材质", "summary": "从标题判断竹制或木制"}]}
        if "规范值" in prompt and "归并" in prompt:
            return {"attributes": [{"属性": n, "规范值": v, "归并": [{"原值": x, "归入": x} for x in v]}
                                   for n, v in VALUES.items()]}
        if "互斥" in prompt:
            return {"互斥": []}
        return {"attributes": [{"name": "材质", "hint": "从标题和五点描述判断竹制或木制，无法确认时填未明确"}]}


def _get(h, body):
    return h._api_attr_spec({"file": [body["config_file"]]})


def _confirm(h, body):
    return h._api_attr_spec_confirm({**body, "expected_revision": _get(h, body)["revision"]})


def _annotation(ctx):
    step = Step17AttrAnnotation()
    step.validate_upstream(ctx)
    step.run(ctx)
    return step


def _confirm_vocab(h, body):
    return h._api_std_vocab_save({**body, "attributes": [{"属性": n, "规范值": v} for n, v in VALUES.items()]})


@pytest.mark.parametrize("shared", [False, True])
def test_complete_step16_edit_confirm_step17_vocab_step18(tmp_path, monkeypatch, shared):
    ctx, root, h, body, inherited = _setup(tmp_path, monkeypatch, shared)
    protected = {p: p.read_bytes() for p in inherited.parent.iterdir() if p.is_file()} if inherited else {}
    model = _FakeModel()
    ctx.config.set("ATTR_STAB_INPUT_MODE", " AUTO ")
    ctx._llm = model
    step16 = Step16AttributeAnnotation()
    step16.validate_upstream(ctx)
    step16.run(ctx)
    own = root / f"{CAT}-attribute-spec.json"
    assert own.is_file() and _read(own)["status"] == "pending_confirm"
    if inherited:
        assert _read(own)["attributes"][0]["modified"] is True
        assert _read(own)["attributes"][0]["aliases"] == ["主体材质"]
    with pytest.raises(WaitingForHuman):
        Step17AttrAnnotation().validate_upstream(ctx)
    _write(root / "16_产品属性标注" / "属性集待确认清单.json", {"items": [{"属性": "雕刻文字", "说明": "按标题 engraved/plain 判断"}]})
    old_view = _get(h, body)
    h._api_attr_candidate({**body, "name": "雕刻文字", "action": "adopt", "expected_revision": old_view["revision"]})
    adopted = _get(h, body)
    rp = Path(resolve_rules_file(str(root)))
    assert {a["name"] for a in _read(rp)["attributes"]} == {"材质", "雕刻文字"}
    assert _read(rp).get("vocab_status") != "confirmed"
    before_stale_save = own.read_bytes()
    with pytest.raises(ValueError, match="更新"):
        h._api_attr_spec_save({**body, "attributes": old_view["attributes"], "expected_revision": old_view["revision"]})
    assert own.read_bytes() == before_stale_save
    h._api_attr_spec_save({**body, "attributes": adopted["attributes"], "expected_revision": adopted["revision"]})
    h._api_attr_exclude({**body, "name": "包装形式", "action": "remove", "force": True,
                         "expected_revision": _get(h, body)["revision"]})
    _confirm(h, body)
    assert set(a["name"] for a in _read(rp)["attributes"]) == set(VALUES)
    _annotation(ctx)
    raw = _read(root / "17_属性标注" / "稳定性结果.json")
    assert raw["passed"] == ["a1", "a2"]
    assert all(g["required_errors"] == 0 for g in raw["gates"].values())
    assert [r["xl_row"] for r in raw["deliverable"]] == [2, 3]
    assert raw["annotation_contract"]["definition_revision"] == _read(own)["definition_revision"]
    assert _read(rp)["vocab_status"] == "pending_confirm"
    assert all(a.get("params", {}).get("values") for a in _read(rp)["attributes"]), "必须实际生成词表"
    with pytest.raises(WaitingForHuman):
        Step18Standardization().validate_upstream(ctx)
    _confirm_vocab(h, body)
    step18 = Step18Standardization()
    step18.validate_upstream(ctx)
    step18.run(ctx)
    step18.validate_artifacts(ctx)
    report = _read(root / "18_标准化" / "交付值校验.json")
    assert report["已校验格数"] == 6 and not report["词表外拦截"] and not report["无词表属性"]
    wb = openpyxl.load_workbook(root / "18_标准化" / f"{CAT}-属性标注-标准化交付.xlsx")
    try:
        headers = [c.value for c in wb["属性标注"][1]]
        for name, values in VALUES.items():
            col = headers.index(name) + 1
            for rownum, value in enumerate(values, 2):
                assert wb["属性标注"].cell(rownum, col).value == value
                assert wb["来源"].cell(rownum, col).value == "标题"
                assert wb["依据"].cell(rownum, col).value
        assert wb["属性标注"].max_row == 3
    finally:
        wb.close()
    # 人工确认会发布并替换唯一共享基准；待确认草稿阶段不影响共享源。
    if inherited:
        assert not inherited.exists(), "旧共享属性集已被新确认版本替换"
        assert all(p.read_bytes() == old for p, old in protected.items() if p != inherited)
    from tools.attr_stability.catalog import resolve_spec_path
    shared_spec = resolve_spec_path(root.parent, "Lifecycle-UK-202611")
    assert _read(shared_spec)["status"] == "confirmed"
    assert {a["name"] for a in _read(shared_spec)["attributes"]} == set(VALUES)
    assert model.calls


@pytest.mark.parametrize("bad", ['{"attributes": [', '[]', '{"attributes":[{"name":"材质"},{"name":"材质"}]}'])
def test_corrupt_old_spec_stops_before_model_and_remains_unchanged(tmp_path, monkeypatch, bad):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    sp = root / f"{CAT}-attribute-spec.json"
    sp.write_text(bad, encoding="utf-8")
    before = sp.read_bytes()
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: pytest.fail("坏基底不得调用模型"))
    step = Step16AttributeAnnotation()
    step.validate_upstream(ctx)
    with pytest.raises(StepDataError, match="校验失败"):
        step.run(ctx)
    with pytest.raises(ValueError, match="校验失败"):
        h._api_attr_spec_save({**body, "attributes": [{"name": "新属性", "hint": ""}]})
    assert sp.read_bytes() == before and not list(root.glob("*.rules.json"))


def test_rules_commit_failure_rolls_back_and_does_not_confirm(tmp_path, monkeypatch):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    h._api_attr_spec_save({**body, "attributes": [{"name": "材质", "hint": "h"}]})
    sp = root / f"{CAT}-attribute-spec.json"
    rp = Path(resolve_rules_file(str(root)))
    before = {p: p.read_bytes() for p in (sp, rp)}
    real_replace = spec_io.os.replace
    def replace(source, dest):
        if Path(dest) == sp:
            raise OSError("fixture spec commit failure")
        return real_replace(source, dest)
    monkeypatch.setattr(spec_io.os, "replace", replace)
    with pytest.raises(OSError, match="fixture"):
        h._api_attr_candidate({**body, "name": "雕刻文字", "action": "adopt"})
    assert all(p.read_bytes() == old for p, old in before.items())
    assert not list(root.glob("*.tmp"))


def test_step17_rejects_spec_rules_drift_before_model(tmp_path, monkeypatch):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    h._api_attr_spec_save({**body, "attributes": [{"name": "材质", "hint": ""}]})
    _confirm(h, body)
    rp = Path(resolve_rules_file(str(root)))
    data = _read(rp)
    data["attributes"].append({"name": "陌生列", "rule": "enum", "params": {}})
    _write(rp, data)
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: pytest.fail("漂移不得调用模型"))
    step = Step17AttrAnnotation()
    step.validate_upstream(ctx)
    with pytest.raises(StepDataError, match="属性集与规则不一致"):
        step.run(ctx)


def test_cached_client_without_revision_cannot_overwrite_new_attributes(tmp_path, monkeypatch):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    h._api_attr_spec_save({**body, "attributes": [{"name": "材质", "hint": ""}]})
    old_view = _get(h, body)
    h._api_attr_candidate({**body, "name": "雕刻文字", "action": "adopt"})
    sp = root / f"{CAT}-attribute-spec.json"
    before = sp.read_bytes()
    with pytest.raises(ValueError, match="必须携带版本"):
        h._api_attr_spec_save({**body, "attributes": old_view["attributes"]})
    with pytest.raises(ValueError, match="必须携带版本"):
        h._api_attr_spec_confirm(body)
    assert sp.read_bytes() == before


def test_upstream_change_after_snapshot_stops_before_annotation_calls(tmp_path, monkeypatch):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    h._api_attr_spec_save({**body, "attributes": [{"name": "材质", "hint": ""}]})
    _confirm(h, body)
    model = _FakeModel()
    ctx._llm = model
    step = Step17AttrAnnotation()
    step.validate_upstream(ctx)
    original = step._load_rows
    def load_and_change(ctx, out_dir):
        rows = original(ctx, out_dir)
        with Path(ctx.resolve_artifact(UPSTREAM_SPLIT)).open("ab") as fh:
            fh.write(b"changed during input loading")
        return rows
    monkeypatch.setattr(step, "_load_rows", load_and_change)
    with pytest.raises(StepDataError, match="快照后发生变化"):
        step.run(ctx)
    assert not model.calls


def test_workflow_subprocess_cannot_overwrite_new_web_revision(tmp_path, monkeypatch):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    h._api_attr_spec_save({**body, "attributes": [{"name": "材质", "hint": "old"}]})
    sp = root / f"{CAT}-attribute-spec.json"
    previous = _read(sp)
    draft = copy.deepcopy(previous)
    draft["attributes"][0]["hint"] = "stale child draft"
    script = """
import json,sys
from tools.attr_stability.spec_io import commit_attr_spec
print('ready',flush=True)
try:
    commit_attr_spec(sys.argv[1],json.loads(sys.argv[2]),expected_revision=sys.argv[3])
except ValueError:
    print('stale rejected',flush=True)
else:
    raise SystemExit('stale draft was accepted')
"""
    project = Path(__file__).resolve().parents[1]
    with spec_io.spec_lock(root):
        process = subprocess.Popen([sys.executable, "-B", "-c", script, str(root), json.dumps(draft), spec_io.spec_revision(previous)],
                                   cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            assert process.stdout.readline().strip() == "ready"
            newer = copy.deepcopy(previous)
            newer["attributes"][0]["hint"] = "new web definition"
            spec_io.write_json_atomic(sp, newer)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stdout + stderr
    assert "stale rejected" in stdout
    assert _read(sp)["attributes"][0]["hint"] == "new web definition"


@pytest.mark.parametrize("mutation", ["add", "hint", "input", "upstream", "legacy", "unfinished", "missing", "duplicate"])
def test_step18_rejects_stale_or_incomplete_annotation(tmp_path, monkeypatch, mutation):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    ctx._llm = _FakeModel()
    h._api_attr_spec_save({**body, "attributes": [{"name": n, "hint": "从标题判断"} for n in VALUES]})
    _confirm(h, body)
    _annotation(ctx)
    _confirm_vocab(h, body)
    result_path = root / "17_属性标注" / "稳定性结果.json"
    if mutation == "add":
        h._api_attr_candidate({**body, "name": "新维度", "action": "adopt"})
        _confirm(h, body)
        _confirm_vocab(h, body)  # 仅再确认词表也不能让旧标注重新有效
    elif mutation == "hint":
        sp = root / f"{CAT}-attribute-spec.json"
        data = _read(sp)
        data["attributes"][0]["hint"] = "直接编辑后的新定义"
        _write(sp, data)
    elif mutation == "input":
        p = root / "17_属性标注" / "input.json"
        data = _read(p)
        data["rows"][0]["标题"] = "changed"
        _write(p, data)
    elif mutation == "upstream":
        p = Path(ctx.resolve_artifact(UPSTREAM_SPLIT))
        with p.open("ab") as fh:
            fh.write(b"changed source")
    else:
        data = _read(result_path)
        if mutation == "legacy":
            data.pop("annotation_contract")
        elif mutation == "unfinished":
            data["annotation_contract"]["completed"] = False
        elif mutation == "missing":
            data["deliverable"][0]["attributes"].pop("材质")
        else:
            data["deliverable"].append(copy.deepcopy(data["deliverable"][0]))
        _write(result_path, data)
    final_dir = root / "18_标准化"
    before = {p.relative_to(final_dir): p.read_bytes() for p in final_dir.rglob("*") if p.is_file()}
    with pytest.raises(StepDataError, match="校验失败"):
        Step18Standardization().validate_upstream(ctx)
    with pytest.raises(StepDataError, match="校验失败"):
        Step18Standardization().run(ctx)
    after = {p.relative_to(final_dir): p.read_bytes() for p in final_dir.rglob("*") if p.is_file()}
    assert after == before
    assert not (final_dir / f"{CAT}-属性标注-标准化交付.xlsx").exists()


def test_vocab_generation_failure_cannot_reuse_previous_confirmation(tmp_path, monkeypatch):
    ctx, root, h, body, _ = _setup(tmp_path, monkeypatch)
    ctx._llm = _FakeModel()
    h._api_attr_spec_save({**body, "attributes": [{"name": n, "hint": "从标题判断"} for n in VALUES]})
    _confirm(h, body)
    _annotation(ctx)
    _confirm_vocab(h, body)
    step = Step17AttrAnnotation()
    step.validate_upstream(ctx)
    def fail_generation(*a, **kw):
        raise ValueError("fixture clustering failure")
    monkeypatch.setattr(step, "_generate_vocab", fail_generation)
    step.run(ctx)
    assert _read(resolve_rules_file(str(root)))["vocab_status"] == "pending_confirm"
    with pytest.raises(WaitingForHuman):
        Step18Standardization().validate_upstream(ctx)


def test_frontend_saves_draft_and_refreshes_after_adopt_or_restore():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run([node, str(Path(__file__).with_name("attr_lifecycle_frontend.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
