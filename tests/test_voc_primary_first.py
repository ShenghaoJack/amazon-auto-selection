"""Finish primary collection and retries before spending SellerSprite credits."""
import json
from threading import Event

from core.mcp import MCPBusinessError
from tests.test_voc_asin_fallback import envelope, item, recovery, rows, seed
from tests.test_voc_mcp_recovery import collector


def sequential(c):
    c.config.set("VOC_FETCH_WORKERS", 1)
    return c


def test_transient_primary_failure_recovers_before_any_seller_call(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"]))
        if args["asin"] == "P1" and len(calls) == 1:
            raise RuntimeError("temporary network failure")
        return envelope(source, [item(args["asin"], "V")])

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.collect_with_fallback(["P1", "P2"], {}, 1, "critical:1")
    assert calls == [("pangolinfo", "P1"), ("pangolinfo", "P2"), ("pangolinfo", "P1")]
    assert not c.failed_parents
    phase = json.loads((tmp_path / "采集来源阶段.json").read_text(encoding="utf-8"))
    assert phase["status"] == "complete"


def test_only_unresolved_queries_reach_seller_after_all_primary_retries(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"]))
        if source == "pangolinfo" and args["asin"] == "P1":
            raise RuntimeError("network failure")
        return envelope(source, [item(args["asin"], "V")])

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.collect_with_fallback(["P1", "P2"], {}, 1, "critical:1")
    assert calls == [("pangolinfo", "P1"), ("pangolinfo", "P2"), ("pangolinfo", "P1"), ("sellersprite", "P1")]
    assert not c.failed_parents
    before = list(calls)
    resumed = sequential(collector(tmp_path, monkeypatch, call))
    resumed.collect_with_fallback(["P1", "P2"], {}, 1, "critical:1")
    assert calls == before


def test_successful_child_prevents_redundant_seller_parent_probe(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"]))
        if args["asin"] == "P":
            raise RuntimeError("parent inaccessible")
        return envelope(source, [item("R1", "V")])

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.collect_with_fallback(["P"], {"P": ["V"]}, 1, "critical:1")
    assert {s for s, _ in calls} == {"pangolinfo"}
    assert not c.failed_parents


def test_partial_child_fallback_fills_only_missing_range(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"], args.get("filterByStar")))
        if source == "pangolinfo" and (args["asin"] == "P" or args["filterByStar"] == "five_star"):
            raise RuntimeError("network failure")
        return envelope(source, [item("R1", "V")])

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.collect_with_fallback(["P"], {"P": ["V"]}, 2, "critical:1,five_star:1")
    seller = [(s, t) for s, t, _ in calls if s == "sellersprite"]
    assert seller == [("sellersprite", "V")]
    assert calls[-1][0] == "sellersprite" and not c.failed_parents


def test_real_quota_failure_does_not_spend_seller_credits_until_final_phase(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"]))
        if source == "pangolinfo":
            raise MCPBusinessError("get_amazon_reviews", 402, "insufficient points")
        return envelope(source, [item("R1", "V")])

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.collect_with_fallback(["P1", "P2"], {}, 1, "critical:1")
    assert calls == [("pangolinfo", "P1"), ("sellersprite", "P1"), ("sellersprite", "P2")]
    assert not c.failed_parents


def test_unconfigured_primary_goes_directly_to_final_fallback(tmp_path, monkeypatch):
    calls = []
    c = sequential(collector(tmp_path, monkeypatch,
        lambda source, args: calls.append(source) or envelope(source, [item("R1", "V")])))
    c.pango = None
    c.collect_with_fallback(["P"], {}, 1, "critical:1")
    assert calls == ["sellersprite"] and not c.failed_parents


def test_unfinished_primary_call_defers_seller_and_preserves_late_result(tmp_path, monkeypatch):
    release, calls = Event(), []

    def call(source, args):
        calls.append(source)
        if source == "pangolinfo":
            assert release.wait(2)
        return envelope(source, [item("R1", "V")])

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.config.set("VOC_MCP_TIMEOUT", 0.02)
    c.config.set("VOC_RETRY_ROUNDS", 0)
    try:
        c.collect_with_fallback(["P"], {}, 1, "critical:1")
        assert calls == ["pangolinfo"]
        assert "P" in c.failed_parents
    finally:
        release.set()


def test_quality_recovery_finishes_primary_jobs_and_retries_before_seller(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"]))
        if len(calls) <= 2:
            return envelope(source, [item(args["asin"])])
        if source == "pangolinfo" and calls.count((source, args["asin"])) == 1:
            raise RuntimeError("temporary primary error")
        return envelope(source, [item(args["asin"], "V")])

    c = sequential(collector(tmp_path / "raw", monkeypatch, call))
    seed(c, "pangolinfo", "P1")
    seed(c, "sellersprite", "P2")
    r = recovery(c, tmp_path / "out", ("P1", "P2"))
    r.run(rows(c))
    assert calls[2:] == [("pangolinfo", "P2"), ("pangolinfo", "P2"), ("sellersprite", "P1")]
    assert r.annotate(rows(c))["pending_tasks"] == 0


def test_final_fallback_failure_remains_resumable_and_restores_phase(tmp_path, monkeypatch):
    def call(source, args):
        raise RuntimeError("both unavailable")

    c = sequential(collector(tmp_path, monkeypatch, call))
    c.collect_with_fallback(["P"], {}, 1, "critical:1")
    assert "P" in c.failed_parents and c.source_phase == "normal"
    phase = json.loads((tmp_path / "采集来源阶段.json").read_text(encoding="utf-8"))
    assert phase["status"] == "pending"
