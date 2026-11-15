from types import SimpleNamespace

import pytest

from core.errors import StepFailedError
from steps import step12_competitor_submit as mod


def _step(monkeypatch, results):
    step = mod.Step12CompetitorSubmit()
    step.log = lambda *args: None
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    monkeypatch.setattr(step, '_platform_task_name_real', lambda *args: None)
    for name in ('_select_site', '_select_month', '_fill_asins', '_click_by_text',
                 '_uncheck_show_all_variants', '_check_quota', '_handle_export_dialog'):
        monkeypatch.setattr(step, name, lambda *args, **kwargs: None)
    clicks = []
    monkeypatch.setattr(step, '_click_export', lambda *args: clicks.append(1))
    monkeypatch.setattr(step, '_wait_export_task_created', lambda *args: next(results))
    browser = SimpleNamespace(goto=lambda *args, **kwargs: None,
                              wait_selector=lambda *args, **kwargs: None)
    return step, clicks, browser


def test_missing_export_is_submitted_again(monkeypatch):
    step, clicks, browser = _step(monkeypatch, iter([None, 'task-2']))
    ctx = SimpleNamespace(config={'COMPETITOR_MONTH': '2026-09'})
    assert step._submit_group_real(ctx, browser, object(), '美国', ['B000C91625'], True) == 'task-2'
    assert len(clicks) == 2


def test_missing_export_stops_after_three_attempts(monkeypatch):
    step, clicks, browser = _step(monkeypatch, iter([None, None, None]))
    ctx = SimpleNamespace(config={'COMPETITOR_MONTH': '2026-09'})
    with pytest.raises(StepFailedError, match='导出任务未在'):
        step._submit_group_real(ctx, browser, object(), '美国', ['B000C91625'], True)
    assert len(clicks) == 3
