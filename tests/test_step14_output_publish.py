"""Step 14 must publish the canonical workbook before downstream steps can run."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from core.config import Config
from core.errors import StepFailedError
from core.orchestrator import PipelineRunner
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit
from steps.step15_category_report import SPLIT_FILE, SPLIT_SHEET
from steps.step17_attr_annotation import UPSTREAM_SPLIT, UPSTREAM_SPLIT_SHEET


def _context(tmp_path):
    cfg = Config({"CAT": "publish-test", "DATA_ROOT": str(tmp_path), "DRY_RUN": True,
                  "SITE": "美国", "LLM_PRICES_MODE": "builtin"})
    return StepContext(cfg, Paths(cfg))


def _frame():
    return pd.DataFrame({"父ASIN": ["P", "P"], "变体ASIN": ["A", "B"],
                         "标题": ["new A", "new B"], "配送方式": ["FBA", "未知"],
                         "新老品": ["老品", "未知"], "价格": [10.0, None], "变体月销量": [11, 12]})


def _old_workbook(ctx):
    target = Path(ctx.resolve_artifact(Step14TrafficSplit.artifacts[0]))
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"版本": ["old"]}).to_excel(target, sheet_name="AI已补充", index=False)
    return target


def _pending(target):
    return sorted(target.parent.glob("流量拆分数据-待发布-*.xlsx"))


def _deny_publish(monkeypatch, target):
    original = os.replace

    def replace(source, destination):
        if Path(destination) == target:
            raise PermissionError("simulated target workbook lock")
        return original(source, destination)

    monkeypatch.setattr("steps.step14_traffic_split.os.replace", replace)
    return original


@pytest.mark.parametrize("has_old", [False, True])
def test_publish_updates_fixed_path_and_downstream_reads_new_workbook(tmp_path, has_old):
    ctx = _context(tmp_path)
    step = Step14TrafficSplit()
    target = (_old_workbook(ctx) if has_old else
              Path(ctx.resolve_artifact(step.artifacts[0])))
    step._export(ctx, _frame(), _frame())
    assert target.is_file()
    assert not _pending(target)
    sheets = pd.read_excel(target, sheet_name=None)
    assert set(sheets) == {"原始表", "AI已补充", "人工待补充表"}
    assert len(sheets["人工待补充表"]) == 1
    for path, sheet in ((SPLIT_FILE, SPLIT_SHEET), (UPSTREAM_SPLIT, UPSTREAM_SPLIT_SHEET)):
        assert Path(ctx.resolve_artifact(path)) == target
        assert pd.read_excel(ctx.resolve_artifact(path), sheet_name=sheet)["标题"].tolist() == ["new A", "new B"]


def test_locked_publish_preserves_old_and_complete_pending_then_retry_succeeds(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    target = _old_workbook(ctx)
    old_bytes = target.read_bytes()
    original = _deny_publish(monkeypatch, target)
    step = Step14TrafficSplit()
    with pytest.raises(StepFailedError, match="待发布") as error:
        step._export(ctx, _frame(), _frame())
    assert target.read_bytes() == old_bytes
    pending = _pending(target)
    assert len(pending) == 1
    assert str(pending[0]) in str(error.value)
    assert pd.read_excel(pending[0], sheet_name="AI已补充")["标题"].tolist() == ["new A", "new B"]
    assert len(pd.read_excel(pending[0], sheet_name="人工待补充表")) == 1
    monkeypatch.setattr("steps.step14_traffic_split.os.replace", original)
    step._export(ctx, _frame(), _frame())
    assert pd.read_excel(target, sheet_name="AI已补充")["标题"].tolist() == ["new A", "new B"]


@pytest.mark.parametrize("failure", [RuntimeError("write interrupted"), KeyboardInterrupt()])
def test_serialization_failure_never_truncates_old_workbook(tmp_path, monkeypatch, failure):
    ctx = _context(tmp_path)
    target = _old_workbook(ctx)
    old_bytes = target.read_bytes()
    original = pd.DataFrame.to_excel

    def write(frame, writer, *args, **kwargs):
        if kwargs.get("sheet_name") == "AI已补充":
            raise failure
        return original(frame, writer, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_excel", write)
    with pytest.raises(type(failure)):
        Step14TrafficSplit()._export(ctx, _frame(), _frame())
    assert target.read_bytes() == old_bytes
    assert not _pending(target)


def test_pipeline_marks_failed_even_when_old_declared_artifact_exists(tmp_path, monkeypatch):
    cfg = _context(tmp_path).config
    runner = PipelineRunner(cfg, dry_run=True)
    step = Step14TrafficSplit()
    target = _old_workbook(runner.ctx)
    assert step.missing_artifacts(runner.ctx) == []
    _deny_publish(monkeypatch, target)
    monkeypatch.setattr(step, "validate_upstream", lambda ctx: None)
    monkeypatch.setattr(step, "run", lambda ctx: step._export(ctx, _frame(), _frame()))
    monkeypatch.setattr("core.notify.popup_critical", Mock())
    monkeypatch.setattr("core.notify.push_wecom", Mock())
    sync = Mock()
    monkeypatch.setattr(runner, "_sync_after_step", sync)
    assert runner._run_one(step) == 2
    assert runner.state.status_of(14) == "FAILED"
    assert "待发布" in runner.state.steps[14]["reason"]
    sync.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_real_windows_file_lock_blocks_publish_without_corrupting_old(tmp_path):
    ctx = _context(tmp_path)
    target = _old_workbook(ctx)
    old_bytes = target.read_bytes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # Allow read/write sharing, but deny deletion/replacement as an open Excel workbook can.
    handle = create(str(target), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError(ctypes.get_last_error())
    try:
        with pytest.raises(StepFailedError, match="待发布"):
            Step14TrafficSplit()._export(ctx, _frame(), _frame())
    finally:
        kernel32.CloseHandle(handle)
    assert target.read_bytes() == old_bytes
    pending = _pending(target)
    assert len(pending) == 1
    assert pd.read_excel(pending[0], sheet_name="AI已补充")["标题"].tolist() == ["new A", "new B"]
