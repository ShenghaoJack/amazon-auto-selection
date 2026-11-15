"""Confirmed-only shared baseline, atomic publication and ownership-aware reset."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest
from core.config import Config
from core.orchestrator import PipelineRunner
from tools.attr_stability import spec_io
from tools.attr_stability.catalog import (
    catalog_dir_of, resolve_spec_path, CONFIRMED_SPEC_FILE, CONFIRMED_RULES_FILE,
)
from tools.attr_stability.spec import resolve_rules_file
from steps.step16_attribute_annotation import Step16AttributeAnnotation

CAT = "Baseline-US-202608"
OTHER = "Baseline-DE-202609"


def ctx(tmp_path, cat=CAT):
    root = tmp_path / cat
    root.mkdir(parents=True, exist_ok=True)
    shared = catalog_dir_of(tmp_path, "Baseline")
    return root, shared


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def draft(name="材质", status="pending_confirm"):
    return {"status": status, "attributes": [{"name": name, "hint": "人工检查说明"}]}


def publish(root, name="材质"):
    pending = spec_io.commit_attr_spec(root, draft(name))
    approved = pending["spec"]
    approved["status"] = "confirmed"
    return spec_io.commit_attr_spec(root, approved, expected_revision=pending["revision"])


def test_pending_is_local_and_never_overwrites_the_confirmed_shared_baseline(tmp_path):
    first, shared = ctx(tmp_path)
    publish(first)
    before = {p: p.read_bytes() for p in shared.iterdir() if p.is_file()}
    second, _ = ctx(tmp_path, OTHER)
    spec_io.commit_attr_spec(second, draft("颜色"))
    assert all(p.read_bytes() == value for p, value in before.items())
    assert read(shared / CONFIRMED_SPEC_FILE)["attributes"][0]["name"] == "材质"
    assert not (shared / "属性池.json").exists()


def test_confirmation_publishes_one_snapshot_and_retires_raw_history(tmp_path):
    root, shared = ctx(tmp_path)
    write(shared / "属性池.json", {"轮次": [{"属性": [{"name": "历史噪声"}]}]})
    write(shared / "old-attribute-spec.json", draft("旧确认项", "confirmed"))
    approved = publish(root)
    snapshot = shared / CONFIRMED_SPEC_FILE
    assert snapshot.read_bytes() == Path(approved["path"]).read_bytes()
    assert read(snapshot)["status"] == "confirmed"
    assert read(shared / "_catalog.json")["属性集来源"] == CAT
    assert sorted(p.name for p in shared.glob("*-attribute-spec.json")) == [CONFIRMED_SPEC_FILE]
    assert not (shared / "属性池.json").exists()
    spec_io.assert_rule_alignment(read(snapshot), read(shared / CONFIRMED_RULES_FILE))


def test_later_human_confirmation_replaces_instead_of_accumulating(tmp_path):
    root, shared = ctx(tmp_path)
    publish(root, "旧项")
    second, _ = ctx(tmp_path, OTHER)
    publish(second, "新确认项")
    snapshot = read(shared / CONFIRMED_SPEC_FILE)
    assert [a["name"] for a in snapshot["attributes"]] == ["新确认项"]
    assert snapshot["_confirmed_from_cat"] == OTHER
    assert len(list(shared.glob("*-attribute-spec.json"))) == 1


@pytest.mark.parametrize("status", ["pending_confirm", "", None])
def test_unconfirmed_legacy_shared_spec_is_never_inherited(tmp_path, status):
    root, shared = ctx(tmp_path)
    value = draft()
    value["status"] = status
    write(shared / "legacy-attribute-spec.json", value)
    write(shared / "_catalog.json", {"属性集": "legacy-attribute-spec.json"})
    assert resolve_spec_path(tmp_path, CAT) == spec_io.own_spec_path(root)


def test_own_draft_is_editable_even_when_shared_is_confirmed(tmp_path):
    root, shared = ctx(tmp_path)
    publish(root)
    spec_io.commit_attr_spec(root, draft("未确认编辑"))
    assert resolve_spec_path(tmp_path, CAT) == spec_io.own_spec_path(root)
    assert read(shared / CONFIRMED_SPEC_FILE)["attributes"][0]["name"] == "材质"


def test_reset_owner_withdraws_its_snapshot_and_does_not_resurrect_history(tmp_path):
    root, shared = ctx(tmp_path)
    publish(root)
    # Even an orphaned historical confirmed file must not reappear after reset.
    write(shared / "orphan-attribute-spec.json", {**draft("旧副本", "confirmed"), "cat": OTHER})
    write(shared / "属性池.json", {"轮次": []})
    removed = spec_io.reset_attr_spec(root)
    assert str(spec_io.own_spec_path(root)) in removed
    assert not spec_io.own_spec_path(root).exists()
    assert not (shared / CONFIRMED_SPEC_FILE).exists()
    assert not (shared / CONFIRMED_RULES_FILE).exists()
    assert not (shared / "属性池.json").exists()
    assert resolve_spec_path(tmp_path, CAT) == spec_io.own_spec_path(root)
    # A new run also sees no baseline; the cleared shared declaration is authoritative.
    assert not resolve_spec_path(tmp_path, "Baseline-UK-202610").exists()
    assert resolve_rules_file(str(root)) == str(root / "baseline.rules.json")  # local rule file stays local


def test_reset_another_run_preserves_the_current_confirmed_baseline(tmp_path):
    first, shared = ctx(tmp_path)
    publish(first)
    other, _ = ctx(tmp_path, OTHER)
    before = (shared / CONFIRMED_SPEC_FILE).read_bytes()
    spec_io.reset_attr_spec(other)
    assert (shared / CONFIRMED_SPEC_FILE).read_bytes() == before
    assert resolve_spec_path(tmp_path, OTHER) == shared / CONFIRMED_SPEC_FILE


def test_reset_previous_publisher_cannot_delete_a_newer_publishers_baseline(tmp_path):
    first, shared = ctx(tmp_path)
    publish(first)
    second, _ = ctx(tmp_path, OTHER)
    publish(second, "最新项")
    spec_io.reset_attr_spec(first)
    assert read(shared / CONFIRMED_SPEC_FILE)["_confirmed_from_cat"] == OTHER
    assert read(shared / CONFIRMED_SPEC_FILE)["attributes"][0]["name"] == "最新项"


def test_pipeline_reset_cleans_generated_candidates_but_not_inherited_files(tmp_path):
    first, shared = ctx(tmp_path)
    publish(first)
    second, _ = ctx(tmp_path, OTHER)
    candidate = second / "16_产品属性标注" / "属性集待确认清单.json"
    write(candidate, {"items": []})
    write(second / "16_产品属性标注" / "属性-价格段.json", {})
    upstream = second / "14_流量拆分" / "upstream.json"
    write(upstream, {"keep": True})
    # Downstream reset must also avoid deleting shared vocabulary reports.
    report = shared / "18_标准化" / "词表聚类报告.json"
    write(report, {"keep": True})
    cfg = Config({"CAT": OTHER, "DATA_ROOT": str(tmp_path), "DRY_RUN": True})
    PipelineRunner(cfg).reset_steps_from(16)
    assert not candidate.parent.exists()
    assert upstream.exists() and report.exists()
    assert (shared / CONFIRMED_SPEC_FILE).exists()
    assert read(shared / CONFIRMED_SPEC_FILE)["_confirmed_from_cat"] == CAT


def test_pipeline_reset_owner_removes_local_and_published_spec(tmp_path):
    root, shared = ctx(tmp_path)
    publish(root)
    write(root / "16_产品属性标注" / "属性集待确认清单.json", {"items": []})
    cfg = Config({"CAT": CAT, "DATA_ROOT": str(tmp_path), "DRY_RUN": True})
    PipelineRunner(cfg).reset_steps_from(16)
    assert not spec_io.own_spec_path(root).exists()
    assert not (root / "16_产品属性标注").exists()
    assert not (shared / CONFIRMED_SPEC_FILE).exists()
    assert not resolve_spec_path(tmp_path, CAT).exists()


@pytest.mark.parametrize("failure_name", [CONFIRMED_SPEC_FILE, CONFIRMED_RULES_FILE, "_catalog.json"])
def test_failed_publication_rolls_back_confirmation_deletions_and_shared_state(tmp_path, monkeypatch, failure_name):
    root, shared = ctx(tmp_path)
    publish(root)
    pending = spec_io.commit_attr_spec(root, draft("待确认新项"))
    write(shared / "属性池.json", {"legacy": True})
    write(shared / "legacy-attribute-spec.json", draft("历史", "confirmed"))
    before = {p: p.read_bytes() for directory in (root, shared) for p in directory.iterdir() if p.is_file()}
    real_replace = spec_io.os.replace
    def replace(src, dst):
        if Path(dst) == shared / failure_name:
            raise OSError("fixture publication failure")
        return real_replace(src, dst)
    monkeypatch.setattr(spec_io.os, "replace", replace)
    approved = pending["spec"]
    approved["status"] = "confirmed"
    with pytest.raises(OSError, match="fixture"):
        spec_io.commit_attr_spec(root, approved, expected_revision=pending["revision"])
    assert all(p.read_bytes() == content for p, content in before.items())
    assert read(spec_io.own_spec_path(root))["status"] == "pending_confirm"


def test_parallel_confirmations_keep_a_matching_single_spec_rules_pair(tmp_path):
    roots = [ctx(tmp_path, cat)[0] for cat in (CAT, OTHER)]
    barrier = threading.Barrier(2)
    def approve(item):
        barrier.wait(timeout=5)
        return publish(item[0], item[1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(approve, zip(roots, ["材质", "颜色"])))
    shared = catalog_dir_of(tmp_path, "Baseline")
    spec = read(shared / CONFIRMED_SPEC_FILE)
    rules = read(shared / CONFIRMED_RULES_FILE)
    spec_io.assert_rule_alignment(spec, rules)
    assert read(shared / "_catalog.json")["属性集来源"] == spec["_confirmed_from_cat"]
    assert len(list(shared.glob("*-attribute-spec.json"))) == 1


def test_deprecated_pool_hook_never_reads_or_writes_raw_history(tmp_path):
    from types import SimpleNamespace
    root, shared = ctx(tmp_path)
    legacy = shared / "属性池.json"
    write(legacy, {"轮次": [{"属性": [{"name": "历史噪声"}]}]})
    before = legacy.read_bytes()
    step = Step16AttributeAnnotation()
    context = SimpleNamespace(config=Config({"CAT": CAT}), paths=SimpleNamespace(data_root=tmp_path))
    fresh = [{"name": "材质", "runs": 3}]
    assert step._pool_merge(context, fresh) == fresh
    assert legacy.read_bytes() == before


def test_confirmation_preserves_manual_catalog_exclusions(tmp_path):
    root, shared = ctx(tmp_path)
    write(shared / "_catalog.json", {"排除属性": ["品牌"], "自定义说明": "保留"})
    publish(root)
    meta = read(shared / "_catalog.json")
    assert meta["排除属性"] == ["品牌"] and meta["自定义说明"] == "保留"


def test_damaged_catalog_blocks_confirmation_without_overwriting_draft(tmp_path):
    root, shared = ctx(tmp_path)
    pending = spec_io.commit_attr_spec(root, draft())
    meta = shared / "_catalog.json"
    meta.write_text("{broken", encoding="utf-8")
    before = Path(pending["path"]).read_bytes()
    approved = pending["spec"]
    approved["status"] = "confirmed"
    with pytest.raises(ValueError, match="损坏"):
        spec_io.commit_attr_spec(root, approved, expected_revision=pending["revision"])
    assert Path(pending["path"]).read_bytes() == before
    assert meta.read_text(encoding="utf-8") == "{broken"
    assert not (shared / CONFIRMED_SPEC_FILE).exists()


def test_reset_failure_rolls_back_deleted_confirmed_files(tmp_path, monkeypatch):
    root, shared = ctx(tmp_path)
    publish(root)
    before = {p: p.read_bytes() for d in (root, shared) for p in d.iterdir() if p.is_file()}
    real_replace = spec_io.os.replace
    def replace(src, dst):
        if Path(dst) == shared / "_catalog.json":
            raise OSError("reset metadata failure")
        return real_replace(src, dst)
    monkeypatch.setattr(spec_io.os, "replace", replace)
    with pytest.raises(OSError, match="reset metadata"):
        spec_io.reset_attr_spec(root)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_reset_removes_damaged_legacy_file_identified_by_its_run_name(tmp_path):
    root, shared = ctx(tmp_path)
    shared.mkdir(parents=True, exist_ok=True)
    owned = shared / (CAT + "-attribute-spec.json")
    owned.write_text("{broken", encoding="utf-8")
    spec_io.reset_attr_spec(root)
    assert not owned.exists()
