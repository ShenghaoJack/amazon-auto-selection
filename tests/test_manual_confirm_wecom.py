"""Tests for WeCom notifications when Steps 8/15/16/18 wait for manual confirmation."""
from __future__ import annotations

import json
from pathlib import Path

from core import notify
from core.config import Config
from core.errors import WaitingForHuman
from core.orchestrator import PipelineRunner


class _StubStep:
    def __init__(self, number: int, title: str, on_run=None):
        self.number = number
        self.title = title
        self._on_run = on_run
        self.artifacts = []
        self.extra_archive_files = []
        self.archive_dirs = []

    def bind_cat(self, ctx) -> None:
        pass

    def skip_when(self, ctx) -> bool:
        return False

    def validate_upstream(self, ctx) -> None:
        pass

    def run(self, ctx) -> None:
        if self._on_run is not None:
            self._on_run(ctx)

    def missing_artifacts(self, ctx) -> list[str]:
        return []


def _make_runner(tmp_path: Path, monkeypatch, webhook: str = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"):
    monkeypatch.setenv("PIPELINE_CATEGORY_OWNER", "S036")
    monkeypatch.setenv("PIPELINE_WORKBENCH_USER", "admin")
    monkeypatch.setenv("PIPELINE_RUN_ID", "run_20261123_demo")
    cfg = Config({
        "CAT": "Air Fryers-US-20261120-RECENT30",
        "DATA_ROOT": str(tmp_path),
        "WECOM_WEBHOOK": webhook,
        "DRY_RUN": True,
    })
    runner = PipelineRunner(cfg)
    monkeypatch.setattr(runner.ctx, "begin_step", lambda *a: None)
    monkeypatch.setattr(runner, "_log_step_usage", lambda *a: None)
    monkeypatch.setattr(runner, "_sync_after_step", lambda *a: None)
    return runner


def _assert_header_matches_screenshot(msg: str) -> list[str]:
    lines = msg.splitlines()
    assert lines[0] == "类目：Air Fryers-US-20261120-RECENT30"
    assert lines[1] == "创建人：S036"
    assert lines[2] == "运行人：admin"
    assert len(lines) == 4
    assert "运行批次：" not in msg
    return lines


def test_step08_done_pushes_manual_confirm_wecom(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "push_wecom", lambda wh, body: sent.append((wh, body)) or True)
    from core import keyword_confirmation as kc
    monkeypatch.setattr(kc, "current", lambda cat, root: {"core": ["air fryer"], "longtail": [], "stopped": False})

    rc = runner._run_one(_StubStep(8, "关键词报告"))
    assert rc == 0
    assert len(sent) == 1
    lines = _assert_header_matches_screenshot(sent[0][1])
    assert lines[3].startswith("步骤 08 关键词报告 等待人工确认: 请核对并确认关键词清单后继续。(耗时 ")
    assert lines[3].endswith(")")

    # Refreshing keywords during confirmation must not re-send waiting notification.
    sent.clear()
    runner._refreshing_keywords = True
    try:
        assert runner._run_one(_StubStep(8, "关键词报告")) == 0
    finally:
        runner._refreshing_keywords = False
    assert sent == []


def test_step15_done_pushes_manual_confirm_wecom(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "push_wecom", lambda wh, body: sent.append((wh, body)) or True)
    from core import step15_review
    monkeypatch.setattr(step15_review, "pending", lambda root: True)

    rc = runner._run_one(_StubStep(15, "初步类目分析报告"))
    assert rc == 0
    assert len(sent) == 1
    lines = _assert_header_matches_screenshot(sent[0][1])
    assert lines[3].startswith("步骤 15 初步类目分析报告 等待人工确认: 请查看步骤15选品指标及AI建议，确认后继续。(耗时 ")
    assert lines[3].endswith(")")


def test_step16_done_pushes_only_when_unconfirmed(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "push_wecom", lambda wh, body: sent.append((wh, body)) or True)

    spec_path = Path(runner.paths.cat_root) / f"{runner.config.cat}-attribute-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps({"status": "pending_confirm", "attributes": []}), encoding="utf-8")

    assert runner._run_one(_StubStep(16, "属性集提取与确认")) == 0
    assert len(sent) == 1
    lines = _assert_header_matches_screenshot(sent[0][1])
    assert lines[3].startswith("步骤 16 属性集提取与确认 等待人工确认: 请核对并确认步骤16属性集后继续。(耗时 ")
    assert lines[3].endswith(")")

    # Already confirmed spec -> no duplicate notification.
    sent.clear()
    spec_path.write_text(json.dumps({"status": "confirmed", "attributes": []}), encoding="utf-8")
    assert runner._run_one(_StubStep(16, "属性集提取与确认")) == 0
    assert sent == []


def test_step17_done_and_step18_waiting_push_step18_confirm_wecom(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "push_wecom", lambda wh, body: sent.append((wh, body)) or True)

    # Normal pipeline pause after Step 17 -> waiting for Step 18 standardization confirm/export.
    assert runner._run_one(_StubStep(17, "属性标注")) == 0
    assert len(sent) == 1
    lines = _assert_header_matches_screenshot(sent[0][1])
    assert lines[3].startswith(
        "步骤 18 标准化 等待人工确认: 步骤 17 属性标注已完成，请确认当前标准化结果并导出交付后继续。(耗时 "
    )
    assert lines[3].endswith(")")

    # Direct Step 18 WaitingForHuman (e.g. unexported panel version or unconfirmed vocab).
    sent.clear()
    def _raise_wait(_ctx):
        raise WaitingForHuman("步骤18当前版本尚未确认导出")

    assert runner._run_one(_StubStep(18, "标准化", on_run=_raise_wait)) == 3
    assert runner.state.status_of(18) == "PENDING"
    assert len(sent) == 1
    lines = _assert_header_matches_screenshot(sent[0][1])
    assert lines[3].startswith("步骤 18 标准化 等待人工确认: 步骤18当前版本尚未确认导出(耗时 ")
    assert lines[3].endswith(")")


def test_empty_webhook_and_push_failure_do_not_block_step(tmp_path, monkeypatch):
    # Empty webhook skips silently.
    runner_no_wh = _make_runner(tmp_path / "a", monkeypatch, webhook="")
    called: list[str] = []
    monkeypatch.setattr(notify, "push_wecom", lambda wh, body: called.append(body) or True)
    from core import step15_review
    monkeypatch.setattr(step15_review, "pending", lambda root: True)
    assert runner_no_wh._run_one(_StubStep(15, "初步类目分析报告")) == 0
    assert called == []

    # Failed push still lets step succeed.
    runner_fail = _make_runner(tmp_path / "b", monkeypatch, webhook="https://example.invalid")
    monkeypatch.setattr(notify, "push_wecom", lambda wh, body: False)
    assert runner_fail._run_one(_StubStep(15, "初步类目分析报告")) == 0
    assert runner_fail.state.status_of(15) == "DONE"
