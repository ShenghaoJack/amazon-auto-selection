"""Regression tests use temporary files and stub execution, never business sources."""
from unittest.mock import Mock

import pytest

from core.config import Config
from core.orchestrator import PipelineRunner
from core.state import RunState
from steps.ids import PIPELINE_STEP_NUMBERS, require_step
from steps.registry import get_steps


def runner_at(tmp_path, monkeypatch):
    runner = PipelineRunner(Config({'CAT': 'M0-Range', 'DATA_ROOT': str(tmp_path), 'DRY_RUN': True}))
    for name in ('snapshot_dir', '_log_price_source', '_report_interruptions', '_backfill_synced_steps',
                 '_log_run_summary', '_precheck_login'):
        monkeypatch.setattr(runner, name, Mock())
    monkeypatch.setattr(runner, '_recheck_done_steps', lambda start: start)
    monkeypatch.setattr(runner, '_usage_suffix', lambda: '')
    return runner


def test_registry_covers_all_21_steps_and_final_monitor():
    steps = get_steps()
    assert tuple(s.number for s in steps) == PIPELINE_STEP_NUMBERS
    assert steps[-1].title == '备份与最终体检'


def test_pending_final_monitor_is_not_reported_as_complete(tmp_path):
    state = RunState(str(tmp_path / 'status.txt'), str(tmp_path / 'duration.csv'))
    for step in range(20):
        state.mark_done(step)
    assert state.first_unfinished() == 20
    reloaded = RunState(state.status_file, state.duration_csv)
    assert reloaded.first_unfinished(20) == 20
    reloaded.mark_done(20)
    assert reloaded.first_unfinished() is None


@pytest.mark.parametrize('start', [0, 18, 19, 20])
def test_run_executes_through_final_monitor(tmp_path, monkeypatch, start):
    runner = runner_at(tmp_path, monkeypatch)
    for step in range(start):
        runner.state.mark_done(step)
    calls = []

    def execute(step):
        calls.append(step.number)
        runner.state.mark_done(step.number)
        return 0

    monkeypatch.setattr(runner, '_run_one', execute)
    assert runner.run(from_step=start) == 0
    assert calls == list(range(start, 21))


@pytest.mark.parametrize('stop_step,code', [(17, 3), (18, 2), (20, 2)])
def test_human_wait_or_failure_does_not_run_downstream(tmp_path, monkeypatch, stop_step, code):
    runner = runner_at(tmp_path, monkeypatch)
    calls = []

    def execute(step):
        calls.append(step.number)
        return code if step.number == stop_step else 0

    monkeypatch.setattr(runner, '_run_one', execute)
    assert runner.run() == code
    assert calls == list(range(stop_step + 1))


def test_reset_from_20_clears_health_but_keeps_monitor_history(tmp_path):
    runner = PipelineRunner(Config({'CAT': 'M0-Reset', 'DATA_ROOT': str(tmp_path), 'DRY_RUN': True}))
    health = tmp_path / 'M0-Reset' / '17_备份与监控' / 'health-report.json'
    health.write_text('{}', encoding='utf-8')
    ledger = health.with_name('llm-usage.jsonl')
    ledger.write_text('history\n', encoding='utf-8')
    runner.state.mark_done(19)
    runner.state.mark_done(20)
    removed = runner.reset_steps_from(20)
    assert str(health) in removed and not health.exists()
    assert ledger.read_text(encoding='utf-8') == 'history\n'
    reloaded = RunState(runner.state.status_file, runner.state.duration_csv)
    assert reloaded.is_finished(19)
    assert not reloaded.is_finished(20)


@pytest.mark.parametrize('bad', [-1, 21, True, 1.0, '1'])
def test_step_number_validation(bad):
    with pytest.raises(ValueError):
        require_step(bad)


@pytest.mark.parametrize('bad', ['-1', '21'])
def test_cli_rejects_invalid_resume_step_before_loading_config(bad):
    import main
    with pytest.raises(SystemExit) as error:
        main.main(['run', '--from', bad])
    assert error.value.code == 2
