import json
import time
from types import SimpleNamespace

import pytest

from tools.voc import tasks
from tools.voc.tasks import BatchQueue


def engine(tmp_path):
    logs = []
    return SimpleNamespace(config={"VOC_LLM_WORKERS": 3}, directory=tmp_path, log=logs.append), logs


def test_large_independent_queue_batches_writes_and_keeps_complete_history(tmp_path, monkeypatch):
    from tools.voc import opportunity
    original = opportunity.save
    writes = []
    def save(path, data):
        writes.append(len(data["events"]))
        original(path, data)
    monkeypatch.setattr(opportunity, "save", save)
    monkeypatch.setattr(tasks, "_AUDIT_INTERVAL_SECONDS", 3600)
    e, _ = engine(tmp_path)
    q = BatchQueue(e, "已有解法核查", [])
    for i in range(1000):
        q.record([i], "complete")
    q.finish()
    data = json.loads((tmp_path / "已有解法核查-任务队列.json").read_text(encoding="utf-8"))
    assert len(data["events"]) == 1000
    assert all(event["status"] == "complete" for event in data["events"])
    assert len(writes) <= 12
    assert writes[-1] == 1000


def test_legacy_queue_remains_durable_after_each_event(tmp_path):
    e, _ = engine(tmp_path)
    q = BatchQueue(e, "Need归并", [])
    for n in range(2):
        q.record([n], "complete")
        data = json.loads((tmp_path / "Need归并-任务队列.json").read_text(encoding="utf-8"))
        assert len(data["events"]) == n + 1


@pytest.mark.parametrize("ordered", [True, False])
def test_parallel_flushes_audit_without_finish(tmp_path, monkeypatch, ordered):
    monkeypatch.setattr(tasks, "_AUDIT_INTERVAL_SECONDS", 3600)
    e, _ = engine(tmp_path)
    q = BatchQueue(e, "Need功能字段关联", [[n] for n in range(8)])
    results = list(q.parallel(lambda batch: batch[0], ordered=ordered))
    assert len(results) == 8
    data = json.loads((tmp_path / "Need功能字段关联-任务队列.json").read_text(encoding="utf-8"))
    assert not data["in_flight"] and data["ready"] == 0
    assert sum(x["status"] == "complete" for x in data["events"]) == 8


def test_pending_tasks_flushed_before_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "_AUDIT_INTERVAL_SECONDS", 3600)
    e, _ = engine(tmp_path)
    e.config["VOC_RETRY_ROUNDS"] = 0
    q = BatchQueue(e, "已有解法核查", [["a"]], split=False)
    def bad(_):
        raise ValueError("故障")
    assert list(q.parallel(bad, ordered=False)) == []
    with pytest.raises(RuntimeError, match="1个任务待重试"):
        q.finish()
    data = json.loads((tmp_path / "已有解法核查-任务队列.json").read_text(encoding="utf-8"))
    assert data["pending"] == 1
    assert data["events"][-1]["status"] == "pending"


def test_progress_logging_includes_cache_replay(tmp_path):
    e, logs = engine(tmp_path)
    q = BatchQueue(e, "已有解法核查", [["a"], ["b"]])
    q._last_progress = time.monotonic() - 31
    q.record(["a"], "complete")
    assert any("已有解法核查" in line and "已完成 1 批" in line for line in logs)
