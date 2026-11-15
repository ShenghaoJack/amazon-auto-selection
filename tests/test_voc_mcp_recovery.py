import json
import time
from threading import Event

from core.mcp import MCPBusinessError
from tests.test_voc_opportunity import config
from tools.voc import pipeline as p


def envelope(source):
    if source == "pangolinfo":
        return {"code": 0, "pointCost": 5, "data": {"json": [{"data": {"results": [{"reviewId": "R1", "content": "works"}]}}]}}
    return {"code": 0, "pointCost": 1, "data": {"items": [{"content": "works", "star": 4}], "total": 1}}


def collector(tmp_path, monkeypatch, function):
    class Client:
        def __init__(self, source):
            self.source = source
        def call_raw(self, tool, args):
            return function(self.source, args)
    monkeypatch.setattr(p, "pango_client", lambda cfg: Client("pangolinfo"))
    monkeypatch.setattr(p, "ss_client", lambda cfg: Client("sellersprite"))
    return p.Collector(str(tmp_path), config(), ["pangolinfo", "sellersprite"], log=lambda _: None)


def test_network_failure_falls_back_and_success_is_not_recharged(tmp_path, monkeypatch):
    order = []
    def call(source, args):
        order.append(source)
        if source == "pangolinfo":
            raise RuntimeError("network timeout")
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 1, "critical:1")
    c.retry_failed()
    assert order == ["pangolinfo", "sellersprite"]
    assert not c.failed_parents
    assert c.points == 1
    rows = p.read_jsonl(c.rev_path["sellersprite"])
    normalized, *_ = p.normalize_and_dedupe(rows, {})
    assert normalized[0]["source"] == "sellersprite"
    c.exhausted_sources = {"sellersprite"}
    c.collect_parent("P", [], 1, "critical:1")
    assert len(order) == 2  # Already saved results remain usable after that source runs out of quota.


def test_both_failures_retry_after_other_parent(tmp_path, monkeypatch):
    order = []
    state = {"first": True}
    def call(source, args):
        order.append((source, args["asin"]))
        if args["asin"] == "P1" and state["first"]:
            raise RuntimeError("network timeout")
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P1", [], 1, "critical:1")
    c.collect_parent("P2", [], 1, "critical:1")
    assert c.failed_parents
    assert any(r["source"] == "sellersprite" and r["status"] == "error" for r in c.raw_rows())
    state["first"] = False
    c.retry_failed()
    assert order == [("pangolinfo", "P1"), ("sellersprite", "P1"), ("pangolinfo", "P2"), ("pangolinfo", "P1")]
    assert not c.failed_parents


def test_both_quotas_exhausted_terminate_without_rotating_back(tmp_path, monkeypatch):
    order = []
    def call(source, args):
        order.append(source)
        raise MCPBusinessError("review", 402, "insufficient points")
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 1, "critical:1")
    assert order == ["pangolinfo", "sellersprite"]
    assert c.sources_dead and c.stopped_reason == "all_sources_exhausted"
    c.retry_failed()
    assert len(order) == 2


def test_entire_mcp_deadline_uses_fallback_and_preserves_late_receipt(tmp_path, monkeypatch):
    release = Event()
    def call(source, args):
        if source == "pangolinfo":
            assert release.wait(1)
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.config.set("VOC_MCP_TIMEOUT", 0.02)
    try:
        started = time.monotonic()
        c.collect_parent("P", [], 1, "critical:1")
        assert time.monotonic() - started < 0.5
        assert c.rows == 1
        release.set()
        deadline = time.monotonic() + 1
        while any(not box["done"].is_set() for box in c.deadlines.active.values()) and time.monotonic() < deadline:
            time.sleep(0.001)
        receipts = p.read_jsonl(str(tmp_path / "MCP完整调用回执.jsonl"))
        assert {r["source"] for r in receipts} == {"pangolinfo", "sellersprite"}
        c.retry_failed()
        assert not c.failed_parents
    finally:
        release.set()


def test_failed_queue_persists_and_full_strata_are_requested_once(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append((args["filterByStar"], args["pageCount"]))
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 5, "critical:3,four_star:1,five_star:1")
    assert calls == [("critical", 3), ("four_star", 1), ("five_star", 1)]
    c.collect_parent("P", [], 5, "critical:3,four_star:1,five_star:1")
    assert len(calls) == 3
    assert json.loads((tmp_path / "采集补试队列.json").read_text(encoding="utf-8"))["pending_parents"] == []


def test_cached_seller_probe_does_not_pin_unfinished_star_ranges_to_fallback(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args.get("filterByStar") or args["starList"]))
        return envelope(source)

    c = collector(tmp_path, monkeypatch, call)
    c.one_call("sellersprite", "P", "P", "critical", "critical", [1, 2, 3], 3)
    calls.clear()
    resumed = collector(tmp_path, monkeypatch, call)
    resumed.collect_parent("P", [], 5, "critical:3,four_star:1,five_star:1")
    assert calls == [("pangolinfo", "four_star"), ("pangolinfo", "five_star")]
    # The already successful SellerSprite critical range is not charged again.
    resumed.collect_parent("P", [], 5, "critical:3,four_star:1,five_star:1")
    assert len(calls) == 2


def test_temporary_primary_failure_in_one_star_range_does_not_pin_later_ranges(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args.get("filterByStar")))
        if source == "pangolinfo" and args["filterByStar"] == "four_star":
            raise RuntimeError("temporary network failure")
        return envelope(source)

    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 5, "critical:3,four_star:1,five_star:1")
    assert calls == [("pangolinfo", "critical"), ("pangolinfo", "four_star"),
                     ("sellersprite", None), ("pangolinfo", "five_star")]
