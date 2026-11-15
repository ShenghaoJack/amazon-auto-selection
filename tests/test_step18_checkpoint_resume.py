import pytest

from core.config import Config
from core.errors import LLMError
from core.llm import LLMClient
from tests.test_step18_workspace import workspace
from tools.attr_stability.step18_model import _annotate_batch
from tools.attr_stability.spec_io import write_json_atomic
from webui.step18_api import _TextBatchCheckpoint


def test_null_model_response_is_recorded_as_retryable_error(monkeypatch):
    client = LLMClient(Config({"DRY_RUN": False, "LLM_API_KEY": "fake", "LLM_BASE_URL": "https://invalid", "LLM_MODEL": "fake"}))
    monkeypatch.setattr(client, "_post_with_watchdog", lambda *a: None)
    failures = []
    monkeypatch.setattr(client, "_note_failure", lambda exc, *a: failures.append(str(exc)))
    with pytest.raises(LLMError, match="空响应"):
        client._request("https://invalid", "fake", "fake", [{"role": "user", "content": "test"}], 0, 10, True)
    assert len(failures) == 1


def test_resume_saves_only_complete_batches_and_respects_version(workspace):
    state = workspace.open()
    row = state["rows"][0]
    rid = row["id"]
    attr = "功率"
    job1 = "J" + "1" * 32
    job2 = "J" + "2" * 32
    write_json_atomic(workspace._path("jobs", job1, "J"), {"id": job1, "status": "failed", "base_version": state["version"]})
    first = _TextBatchCheckpoint(workspace, job1)
    class Model:
        config = {"LLM_MODEL": "fake", "LLM_BASE_URL": "https://invalid"}
        calls = 0
        def chat_json(self, prompt, **kwargs):
            self.calls += 1
            return {"rows": [{"row_id": rid, "attributes": {attr:
                    {"value": "未明确", "source": "", "basis": "无法确定"}}}]}
    m = Model()
    initial = _annotate_batch(m, "重判", [attr], [row], state, checkpoint=first)
    assert m.calls == 1 and (rid, attr) in initial[0]
    resumed = _TextBatchCheckpoint(workspace, job2, job1)
    assert _annotate_batch(m, "重判", [attr], [row], state, checkpoint=resumed) == initial
    assert m.calls == 1
    changed = {**state, "version": "different"}
    _annotate_batch(m, "重判", [attr], [row], changed, checkpoint=resumed)
    assert m.calls == 2


def test_incomplete_batch_reuses_complete_rows_only(workspace):
    state = workspace.open()
    rows = state["rows"]
    job = "J" + "3" * 32
    checkpoint = _TextBatchCheckpoint(workspace, job)
    class Model:
        config = {"LLM_MODEL": "fake", "LLM_BASE_URL": "https://invalid"}
        calls = 0
        prompts = []
        def chat_json(self, prompt, **kwargs):
            self.calls += 1
            self.prompts.append(prompt)
            rid = rows[0]["id"] if self.calls == 1 else rows[1]["id"]
            return {"rows": [{"row_id": rid, "attributes": {"功率":
                    {"value": "未明确", "source": "", "basis": "无法确定"}}}]}
    m = Model()
    initial, _ = _annotate_batch(m, "重判", ["功率"], rows, state, checkpoint=checkpoint)
    resumed, _ = _annotate_batch(m, "重判", ["功率"], rows, state, checkpoint=checkpoint)
    assert m.calls == 2
    assert set(initial) == {(rows[0]["id"], "功率")}
    assert set(resumed) == {(row["id"], "功率") for row in rows}
    assert '"row_id":"%s"' % rows[0]["id"] not in m.prompts[1]


def test_retry_after_service_restart_uses_interrupted_job(workspace, monkeypatch):
    from webui import step18_api as api
    version = workspace.open()["version"]
    job = "J" + "4" * 32
    request = {"selection": {"attributes": ["功率"]}, "requirement": "重判", "mode": "text"}
    write_json_atomic(workspace._path("jobs", job, "J"),
                      {"id": job, "status": "running", "base_version": version, "request": request})
    captured = {}
    monkeypatch.setattr(api, "start_preview", lambda ws, req, cfg: captured.update(req) or {"ok": True})
    api.retry_job(workspace, job, version, {})
    assert captured["_resume_from"] == job
    captured.clear()
    api.retry_job(workspace, job, "other-version", {})
    assert "_resume_from" not in captured
