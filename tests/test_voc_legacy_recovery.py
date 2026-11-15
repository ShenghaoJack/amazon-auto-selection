import json
import re
from threading import Event

import pytest

from core.errors import LLMError
from tests.test_voc_opportunity import config, row
from tools.voc import annotate as a


def test_compatibility_annotation_defers_failed_batch_and_resumes_success(tmp_path, monkeypatch):
    order = []
    class Client:
        pass
    monkeypatch.setattr(a, "LLMClient", lambda *args, **kwargs: Client())
    def classify(llm, rows, number, **kwargs):
        order.append(number)
        if len(order) == 1:
            return []
        return [{"i": i, "quote": r["review_body"]} for i, r in enumerate(rows)]
    monkeypatch.setattr(a, "classify_batch", classify)
    labels, stats = a.run_annotation([row("A", variant_asin="A"), row("B", variant_asin="B")], config(),
                                    workers=1, cache_path=str(tmp_path / "cache.jsonl"), log=lambda _: None)
    assert order == [0, 1, 0]
    assert all(labels) and stats["failed_reviews"] == 0


def test_unresponsive_annotation_does_not_block_other_batch(tmp_path, monkeypatch):
    release = Event()
    cfg = config()
    cfg.set("VOC_TASK_TIMEOUT", 0.02)
    monkeypatch.setattr(a, "LLMClient", lambda *args, **kwargs: object())
    def classify(llm, rows, number, **kwargs):
        if number == 0:
            assert release.wait(1)
        return [{"i": i, "quote": r["review_body"]} for i, r in enumerate(rows)]
    monkeypatch.setattr(a, "classify_batch", classify)
    try:
        labels, stats = a.run_annotation([row("A", variant_asin="A"), row("B", variant_asin="B")], cfg,
                                        workers=2, cache_path=str(tmp_path / "cache.jsonl"), log=lambda _: None)
        assert not labels[0] and labels[1]
        assert stats["failed_reviews"] == 1
    finally:
        release.set()


def test_canonicalization_failure_defers_instead_of_rule_success(tmp_path, monkeypatch):
    order = []
    class Client:
        def chat_json(self, prompt, **kwargs):
            issues = json.loads(re.search(r"待归一的问题标签: (.+)\n", prompt)[1])
            scenes = json.loads(re.search(r"待归一的场景标签: (.+)\n", prompt)[1])
            order.append(len(issues))
            if len(order) == 1:
                raise LLMError("connection lost")
            return {"issues": {x: a.CANON_ISSUES[0] for x in issues}, "scenes": {x: a.CANON_SCENES[0] for x in scenes}}
    monkeypatch.setattr(a, "LLMClient", lambda *args, **kwargs: Client())
    labels = [{"issue": f"issue{i:02}", "scene": ""} for i in range(41)]
    a.canonicalize(labels, [row() for _ in labels], config(), cache_path=str(tmp_path / "canon.json"), log=lambda _: None)
    assert order == [40, 1, 40]
    assert all(x["issue_canon"] for x in labels)


def test_permanently_failed_canonicalization_remains_pending(tmp_path, monkeypatch):
    class Client:
        def chat_json(self, *args, **kwargs):
            raise LLMError("connection lost")
    monkeypatch.setattr(a, "LLMClient", lambda *args, **kwargs: Client())
    labels = [{"issue": "测试观点", "scene": ""}]
    with pytest.raises(RuntimeError, match="待补试"):
        a.canonicalize(labels, [row()], config(), cache_path=str(tmp_path / "canon.json"), log=lambda _: None)
    assert "issue_canon" not in labels[0]
