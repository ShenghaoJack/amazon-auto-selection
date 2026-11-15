"""Explicit no-review receipts are terminal only for their confirmed scope."""
import pytest

from tests.test_voc_asin_fallback import envelope, item, recovery, rows
from tests.test_voc_mcp_recovery import collector
from tools.voc.pipeline import no_review_scope


def empty(source, *, absent=False):
    if absent:
        return {"code": 0, "message": "Review page not found for this ASIN", "data": {"json": []}}
    result = envelope(source, [])
    if source == "pangolinfo":
        result["data"]["json"][0]["data"]["totalReviews"] = "0"
    return result


@pytest.mark.parametrize("source", ["pangolinfo", "sellersprite"])
def test_confirmed_empty_is_reused_even_if_requested_page_budget_increases(tmp_path, monkeypatch, source):
    calls = []
    c = collector(tmp_path, monkeypatch, lambda s, a: calls.append(s) or empty(s))
    c._collect_call(source, "P", "P", "critical", "critical", [1, 2, 3], 1)
    result = c._collect_call(source, "P", "P", "critical", "critical", [1, 2, 3], 3)
    assert calls == [source]
    assert no_review_scope(result) == "stratum"
    resumed = collector(tmp_path, monkeypatch, lambda *a: pytest.fail("empty query must not repeat"))
    assert no_review_scope(resumed._collect_call(source, "P", "P", "critical", "critical", [1, 2, 3], 5)) == "stratum"
    resumed.identity_repair_variants = {"V"}
    assert no_review_scope(resumed._collect_call("pangolinfo", "P", "P", "critical", "critical", [1, 2, 3], 5)) == "stratum"


def test_empty_critical_does_not_skip_positive_stars_or_probe_children(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append((source, args["asin"], args.get("filterByStar")))
        if args["filterByStar"] == "critical":
            return empty(source)
        return envelope(source, [item(args["filterByStar"], "V")])

    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", ["V"], 5, "critical:3,four_star:1,five_star:1")
    assert calls == [("pangolinfo", "P", star) for star in ["critical", "four_star", "five_star"]]
    assert not c.failed_parents
    assert len(rows(c)) == 2


def test_absent_review_page_skips_all_stars_and_child_probes_and_resumes(tmp_path, monkeypatch):
    calls = []
    c = collector(tmp_path, monkeypatch, lambda s, a: calls.append(s) or empty(s, absent=True))
    c.collect_parent("P", ["V", "V2"], 5, "critical:3,four_star:1,five_star:1")
    assert calls == ["pangolinfo"]
    resumed = collector(tmp_path, monkeypatch, lambda *a: pytest.fail("absent ASIN must not repeat"))
    resumed.collect_parent("P", ["V"], 5, "critical:3,four_star:1,five_star:1")
    assert not resumed.failed_parents


def test_seller_stops_pagination_after_confirmed_empty_page(tmp_path, monkeypatch):
    calls = []
    c = collector(tmp_path, monkeypatch, lambda s, a: calls.append(a["page"]) or empty(s))
    receipt = c.one_call("sellersprite", "P", "P", "critical", "critical", [1, 2, 3], 3)
    assert calls == [1] and no_review_scope(receipt) == "stratum"


def test_asin_recovery_uses_absent_receipt_across_star_ranges_without_paid_calls(tmp_path, monkeypatch):
    calls = []

    def call(source, args):
        calls.append(source)
        return empty(source, absent=True) if source == "pangolinfo" else envelope(source, [item(str(args["starList"]))])

    c = collector(tmp_path / "raw", monkeypatch, call)
    for stratum, stars in [("critical", [1, 2, 3]), ("five_star", [5])]:
        c.one_call("sellersprite", "P", "P", stratum, stratum, stars, 1)
    c.one_call("pangolinfo", "P", "P", "critical", "critical", [1, 2, 3], 1)
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    assert calls == ["sellersprite", "sellersprite", "pangolinfo"]
    for task in r.tasks.values():
        assert task.get("skipped_no_reviews"), str(task)
    summary = r.annotate(rows(c))
    assert summary["pending_tasks"] == 0 and summary["missing_reviews_after"] == 2


@pytest.mark.parametrize("raw", [{"code": 429, "message": "Rate limit exceeded"},
                                  {"code": 402, "message": "insufficient points"},
                                  {"code": 0, "data": {}},
                                  {"code": 0, "data": {"json": [{"code": "RATE_LIMIT", "data": {"results": [], "totalReviews": 0}}]}}])
def test_errors_and_missing_data_are_not_confirmed_no_reviews(raw):
    assert no_review_scope({"source": "pangolinfo", "status": "ok", "rows": 0, "raw": raw}) == ""
