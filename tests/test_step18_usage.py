"""步骤18面板任务记入类目累计账本；测试不调用外部模型。"""
import time

from core.config import Config
from core.llm import LLMClient
from core.usage import ledger_paths, load_records, summarize_category
from tests.test_step18_workspace import request, workspace
from webui.server import cat_usage
from webui.step18_api import job_status, start_audit, start_preview


class RecordedClient(LLMClient):
    def __init__(self, config, scenario):
        super().__init__(config)
        self.scenario = scenario

    def chat_json(self, prompt, **kwargs):
        self.meter().record({"prompt_tokens": 100, "completion_tokens": 10},
                            channel="text", model="deepseek-flash")
        if self.scenario == "failed":
            raise ValueError("模拟模型输出失败")
        if self.scenario == "audit":
            return {"judgements": [{"idx": 0, "supported": False, "reason": "无否定依据"}]}
        if self.scenario == "vision":
            return {"operations": [{"type": "重判", "attributes": ["温度调节"]}], "unresolved": []}
        return {"operations": [{"type": "归类", "attributes": ["材质"],
                "mappings": {"材质": {"ABS": "ABS塑料"}}}], "unresolved": []}

    def vision(self, prompt, images, **kwargs):
        self.meter().record({"prompt_tokens": 120, "completion_tokens": 20},
                            channel="vision", model="deepseek-flash")
        return {"rows": [{"row_id": "2", "attributes": {"温度调节": {
            "value": "低/中/高", "source": "图片", "basis": "三档控温开关"}}}]}


def _done(ws, job):
    for _ in range(500):
        result = job_status(ws, job["job_id"])
        if result["status"] not in ("pending", "running", "cancelling"):
            return result
        time.sleep(.01)
    raise AssertionError("预览任务超时")


def test_panel_usage_is_cumulative_for_parallel_success_and_failure(workspace):
    ws = workspace
    config = {"CAT": ws.cat, "DRY_RUN": False, "LLM_PRICES_MODE": "builtin"}
    def factory(scenario):
        return lambda cfg: RecordedClient(Config(cfg), scenario)

    one = start_preview(ws, request(ws, ["材质"], "把ABS归为ABS塑料"), config,
                        client_factory=factory("mapping"))
    req = request(ws, ["温度调节"], "按图片复核", selection={"row_ids": ["2"]})
    req["mode"] = "vision"
    two = start_preview(ws, req, config, client_factory=factory("vision"))
    three = start_preview(ws, request(ws, ["材质"], "按原文重新判断"), config,
                          client_factory=factory("failed"))
    assert [_done(ws, j)["status"] for j in (one, two, three)] == ["done", "done", "failed"]
    folder = ws.root / "20_备份与监控"
    paths = ledger_paths(str(folder))
    assert len(paths) == 3 and all("web-step18-" in p for p in paths)
    records = [r for p in paths for r in load_records(p)]
    assert len([r for r in records if r.get("type") == "call"]) == 4
    assert {r["status"] for r in records if r.get("type") == "step-summary"} == {"DONE", "FAILED"}
    usage = cat_usage(folder)
    assert usage["calls"] == 4 and usage["total_tokens"] == 470
    assert usage["steps"]["18"]["total_tokens"] == 470
    assert usage["by_channel"]["text"]["calls"] == 3
    assert usage["by_channel"]["vision"]["calls"] == 1
    assert usage["cost_cny"] > 0
    assert not (folder / "llm-usage-steps.csv").exists()


def test_panel_usage_adds_to_summary_only_legacy_without_double_counting():
    records = [
        {"type": "step-summary", "run_id": "old", "step": 18, "calls": 5,
         "in_hit": 0, "in_miss": 500, "out": 50, "cost_cny": 1.25},
        {"type": "call", "run_id": "web-step18-J1", "step": 18, "channel": "text",
         "model": "deepseek-flash", "in_hit": 0, "in_miss": 100, "out": 10, "cost_cny": .5},
        {"type": "step-summary", "run_id": "web-step18-J1", "step": 18, "calls": 1,
         "in_hit": 0, "in_miss": 100, "out": 10, "cost_cny": .5},
    ]
    usage = summarize_category(records)
    assert usage["totals"]["calls"] == 6
    assert usage["totals"]["total_tokens"] == 660
    assert usage["totals"]["cost_cny"] == 1.75
    assert usage["steps"][18]["calls"] == 6
    assert usage["run_id"] == "old"
    live = summarize_category(records[:-1])
    assert live["totals"]["calls"] == 6 and live["totals"]["total_tokens"] == 660


def test_negative_audit_usage_is_recorded(workspace):
    ws = workspace
    config = {"CAT": ws.cat, "DRY_RUN": False, "LLM_PRICES_MODE": "builtin"}
    job = start_audit(ws, {"expected_version": ws.open()["version"]}, config,
                      client_factory=lambda cfg: RecordedClient(Config(cfg), "audit"))
    assert _done(ws, job)["status"] == "done"
    usage = cat_usage(ws.root / "20_备份与监控")
    assert usage["steps"]["18"]["calls"] == 1
    assert usage["steps"]["18"]["total_tokens"] == 110


def test_cancel_running_job_stops_after_current_model_call(workspace):
    import threading
    from webui.step18_api import cancel_job
    started, release = threading.Event(), threading.Event()
    class Slow(RecordedClient):
        def chat_json(self, prompt, **kwargs):
            started.set()
            assert release.wait(10)
            return super().chat_json(prompt, **kwargs)
    cfg = {"CAT": workspace.cat, "DRY_RUN": False, "LLM_PRICES_MODE": "builtin"}
    initial = workspace.open()["version"]
    job = start_preview(workspace, request(workspace, ["材质"], "把ABS归为ABS塑料"), cfg,
                        client_factory=lambda c: Slow(Config(c), "mapping"))
    assert started.wait(5)
    assert cancel_job(workspace, job["job_id"])["status"] == "cancelling"
    release.set()
    result = _done(workspace, job)
    assert result["status"] == "cancelled" and result["result"] is None
    assert workspace.open()["version"] == initial


def test_cancel_finished_preview_blocks_apply_and_retry_uses_saved_request(workspace, monkeypatch):
    from webui import step18_api as api
    cfg = {"CAT": workspace.cat, "DRY_RUN": False, "LLM_PRICES_MODE": "builtin"}
    req = request(workspace, ["材质"], "把ABS归为ABS塑料")
    job = start_preview(workspace, req, cfg, client_factory=lambda c: RecordedClient(Config(c), "mapping"))
    done = _done(workspace, job)
    proposal = done["result"]["id"]
    assert api.cancel_job(workspace, job["job_id"])["status"] == "cancelled"
    import pytest
    with pytest.raises(ValueError, match="已取消"):
        workspace.apply(proposal, workspace.open()["version"])
    captured = {}
    def fake_start(ws, new_request, config):
        captured.update(new_request)
        return {"ok": True}
    monkeypatch.setattr(api, "start_preview", fake_start)
    api.retry_job(workspace, job["job_id"], workspace.open()["version"], cfg)
    assert captured["selection"] == req["selection"] and captured["mode"] == req["mode"]
    assert captured["requirement"] == req["requirement"]


def test_legacy_vocab_clients_record_to_the_correct_step(workspace):
    from webui.server import _meter_legacy_vocab_client
    config = Config({"CAT": workspace.cat, "DRY_RUN": False, "LLM_PRICES_MODE": "builtin"})
    for step in (17, 18):
        client = _meter_legacy_vocab_client(RecordedClient(config, "mapping"), workspace.root, step)
        client.chat_json("example")
    usage = cat_usage(workspace.root / "20_备份与监控")
    assert usage["steps"]["17"]["total_tokens"] == 110
    assert usage["steps"]["18"]["total_tokens"] == 110
