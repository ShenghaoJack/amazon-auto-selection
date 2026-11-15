from copy import deepcopy
import json

import pytest

from core import keyword_confirmation as kc
from core.config import Config
from core.orchestrator import PipelineRunner
from steps.base import BaseStep


@pytest.fixture
def setup_gate(tmp_path, monkeypatch):
    runner = PipelineRunner(Config({"CAT": "Test", "DATA_ROOT": str(tmp_path),
                                    "LLM_PRICES_MODE": "builtin"}), dry_run=True)
    values = {"core": ["dog bowls"], "longtail": ["small dog bowl"],
              "mode": "manual", "stopped": False}
    (runner.paths.cat_root / "Test-amazon-keyword-data.json").write_text(
        json.dumps([{"keyword": "dog bowls"}]), encoding="utf-8")
    (runner.paths.cat_root / "Test-keyword-report.xlsx").write_bytes(b"test")
    monkeypatch.setattr(kc, "current", lambda *args: deepcopy(values))
    kc.pending(runner.paths.cat_root, deepcopy(values))
    return runner, values


def test_confirm_new_core_updates_5_to_8_then_resumes(setup_gate, monkeypatch):
    runner, values = setup_gate
    values["core"].append("slow feeder dog bowls")
    calls = []
    monkeypatch.setattr(runner, "run_single", lambda n: calls.append(n) or 0)
    monkeypatch.setattr(runner, "run", lambda **kwargs: calls.append(kwargs) or 0)
    assert runner.confirm_keywords_and_resume(kc.revision(values)) == 0
    assert calls == [5, 6, 7, 8, {"from_step": 9, "skip_finished": True}]
    assert kc.read(runner.paths.cat_root)["status"] == "confirmed"


def test_confirm_without_changes_does_not_refresh(setup_gate, monkeypatch):
    runner, values = setup_gate
    calls = []
    monkeypatch.setattr(runner, "run_single", lambda n: calls.append(n) or 0)
    monkeypatch.setattr(runner, "run", lambda **kwargs: calls.append(kwargs) or 0)
    assert runner.confirm_keywords_and_resume(kc.revision(values)) == 0
    assert calls == [{"from_step": 9, "skip_finished": True}]


def test_existing_added_core_missing_from_step5_refreshes_all(setup_gate, monkeypatch):
    runner, values = setup_gate
    values["core"].append("slow feeder dog bowls")
    kc.pending(runner.paths.cat_root, deepcopy(values))
    calls = []
    monkeypatch.setattr(runner, "run_single", lambda n: calls.append(n) or 0)
    monkeypatch.setattr(runner, "run", lambda **kwargs: calls.append(kwargs) or 0)
    assert runner.confirm_keywords_and_resume(kc.revision(values)) == 0
    assert calls == [5, 6, 7, 8, {"from_step": 9, "skip_finished": True}]


def test_failed_metric_record_is_not_treated_as_completed(setup_gate):
    runner, _ = setup_gate
    (runner.paths.cat_root / "Test-amazon-keyword-data.json").write_text(
        json.dumps([{"keyword": "dog bowls", "status": "error"}]), encoding="utf-8")
    assert kc.missing_metrics(runner.paths.cat_root, "Test", ["dog bowls"]) == ["dog bowls"]


def test_longtail_change_updates_report_only(setup_gate, monkeypatch):
    runner, values = setup_gate
    values["longtail"].append("dog bowl for travel")
    calls = []
    monkeypatch.setattr(runner, "run_single", lambda n: calls.append(n) or 0)
    monkeypatch.setattr(runner, "run", lambda **kwargs: calls.append(kwargs) or 0)
    assert runner.confirm_keywords_and_resume(kc.revision(values)) == 0
    assert calls == [8, {"from_step": 9, "skip_finished": True}]


def test_failed_refresh_remains_pending_and_does_not_continue(setup_gate, monkeypatch):
    runner, values = setup_gate
    values["core"].append("new")
    calls = []
    monkeypatch.setattr(runner, "run_single", lambda n: calls.append(n) or (2 if n == 6 else 0))
    monkeypatch.setattr(runner, "run", lambda **kwargs: pytest.fail("must not resume"))
    assert runner.confirm_keywords_and_resume(kc.revision(values)) == 2
    assert calls == [5, 6]
    assert kc.read(runner.paths.cat_root)["status"] == "pending"


def test_changed_revision_never_runs(setup_gate, monkeypatch):
    runner, values = setup_gate
    old_revision = kc.revision(values)
    values["core"].append("new")
    monkeypatch.setattr(runner, "run_single", lambda n: pytest.fail("must not refresh"))
    assert runner.confirm_keywords_and_resume(old_revision) == 3
    assert kc.read(runner.paths.cat_root)["status"] == "pending"


def test_refresh_that_changes_effective_words_requires_new_confirmation(setup_gate, monkeypatch):
    runner, values = setup_gate
    values["core"].append("new")
    revision = kc.revision(values)
    def refresh(n):
        if n == 8:
            values["core"].append("changed by refresh")
        return 0
    monkeypatch.setattr(runner, "run_single", refresh)
    monkeypatch.setattr(runner, "run", lambda **kwargs: pytest.fail("must not resume"))
    assert runner.confirm_keywords_and_resume(revision) == 3
    assert kc.read(runner.paths.cat_root)["status"] == "pending"


def test_step8_creates_pending_and_step9_is_blocked(setup_gate, monkeypatch):
    runner, values = setup_gate

    class FakeStep(BaseStep):
        number = 8
        title = "关键词报告"
        artifacts = []
        def run(self, ctx):
            pass

    monkeypatch.setattr(runner, "_sync_after_step", lambda step: None)
    assert runner._run_one(FakeStep()) == 0
    assert runner.state.status_of(8) == "DONE"
    assert kc.read(runner.paths.cat_root)["baseline"] == values
    assert runner.run_single(9) == 3


def test_corrupt_gate_does_not_silently_pass(setup_gate):
    runner, _ = setup_gate
    kc.path(runner.paths.cat_root).write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        runner._keyword_confirmation_pending()


def test_existing_step8_waits_before_first_step9(tmp_path, monkeypatch):
    runner = PipelineRunner(Config({"CAT": "Existing", "DATA_ROOT": str(tmp_path)}), dry_run=True)
    current = {"core": ["dog bowls"], "longtail": [], "mode": "auto", "stopped": False}
    monkeypatch.setattr(kc, "current", lambda *args: current)
    runner.state.start_step(8)
    runner.state.mark_done(8)
    assert runner._keyword_confirmation_pending()
    assert kc.read(runner.paths.cat_root)["baseline"] == current


def test_external_edit_after_confirmation_requests_new_confirmation(setup_gate, monkeypatch):
    runner, values = setup_gate
    monkeypatch.setattr(runner, "run", lambda **kwargs: 0)
    assert runner.confirm_keywords_and_resume(kc.revision(values)) == 0
    values["core"].append("new externally edited keyword")
    assert runner._keyword_confirmation_pending()
    assert kc.read(runner.paths.cat_root)["baseline"]["core"] == ["dog bowls"]


def test_resume_skips_completed_downstream_steps(setup_gate, monkeypatch):
    import core.orchestrator as orchestrator
    runner, _ = setup_gate
    gate = kc.read(runner.paths.cat_root)
    gate["status"] = "confirmed"
    kc.write(runner.paths.cat_root, gate)
    for n in (9, 11):
        runner.state.start_step(n)
        runner.state.mark_done(n)
    class FakeStep:
        title = "测试"
        def __init__(self, number):
            self.number = number
    monkeypatch.setattr(orchestrator, "PIPELINE_STEP_NUMBERS", (9, 10, 11, 12))
    monkeypatch.setattr(orchestrator, "get_steps", lambda: [FakeStep(n) for n in (9, 10, 11, 12)])
    monkeypatch.setattr(runner, "snapshot_dir", lambda: None)
    monkeypatch.setattr(runner, "_log_price_source", lambda: None)
    monkeypatch.setattr(runner, "_report_interruptions", lambda: None)
    monkeypatch.setattr(runner, "_precheck_login", lambda _: None)
    monkeypatch.setattr(runner, "_backfill_synced_steps", lambda _: None)
    monkeypatch.setattr(runner, "_log_run_summary", lambda _: None)
    calls = []
    monkeypatch.setattr(runner, "_run_one", lambda step: calls.append(step.number) or 0)
    assert runner.run(from_step=9, skip_finished=True) == 0
    assert calls == [10, 12]
