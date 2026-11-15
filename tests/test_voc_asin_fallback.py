"""Missing ASIN is a row-level quality failure, including mixed batches."""
import csv

from tests.test_voc_mcp_recovery import collector
from tools.voc.asin_recovery import AsinRecovery
from tools.voc.pipeline import normalize_and_dedupe, build_collection_plan
from tools.voc.normalize import normalize_record


def item(key, asin=None):
    return {"reviewId": key, "author": key, "title": "title " + key, "content": "body " + key,
            "date": "2026-09-19", "star": 4, **({"asin": asin} if asin else {})}


def envelope(source, rows):
    return ({"code": 0, "data": {"json": [{"data": {"results": rows}}]}} if source == "pangolinfo"
            else {"code": "OK", "data": {"items": rows, "total": len(rows)}})


def rows(c):
    raw = [r for s in ("pangolinfo", "sellersprite") for r in c._read_raw(c.rev_path[s])]
    return normalize_and_dedupe(raw, {})[0]


def recovery(c, path, parents=("P",)):
    catalog = {"products": [{"parent_asin": p, "variant_asin": "V"} for p in parents]}
    return AsinRecovery(c, path, catalog, build_collection_plan(1, "critical:1"))


def seed(c, source, parent="P"):
    c.one_call(source, parent, parent, "critical", "critical", [1, 2, 3], 1)


def test_partial_primary_missing_asin_uses_seller_and_preserves_good_rows(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append(source)
        return envelope(source, [item("good", "V"), item("missing")] if source == "pangolinfo" else [item("missing", "V2")])
    c = collector(tmp_path / "raw", monkeypatch, call)
    seed(c, "pangolinfo")
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    final = rows(c)
    result = r.annotate(final)
    assert calls == ["pangolinfo", "sellersprite"]
    assert {x["author"]: x["variant_asin"] for x in final} == {"good": "V", "missing": "V2"}
    assert result["missing_reviews_before"] == 1 and result["missing_reviews_after"] == 0
    audit = list(csv.DictReader((r.directory / "ASIN缺失评论明细.csv").open(encoding="utf-8-sig")))
    assert audit[0]["确认ASIN"] == "V2" and audit[0]["ASIN状态"] == "已补齐"


def test_seller_missing_groups_requests_not_per_review_and_new_reviews_do_not_fill_old(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append(source)
        return envelope(source, [item("a"), item("b")] if source == "sellersprite" else [item("a", "V"), item("new", "V")])
    c = collector(tmp_path / "raw", monkeypatch, call)
    seed(c, "sellersprite")
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    final = rows(c)
    result = r.annotate(final)
    assert calls == ["sellersprite", "pangolinfo"] and result["request_groups"] == 1
    assert result["missing_reviews_after"] == 1
    b = next(x for x in final if x["author"] == "b")
    assert b["asin_status"] == "缺失" and b["asin_recovery_status"] == "已跨源补抓仍未确认"
    audit = list(csv.DictReader((r.directory / "ASIN缺失评论明细.csv").open(encoding="utf-8-sig")))
    assert next(x for x in audit if x["评论原文"] == "body b")["确认ASIN"] == ""
    # A new process restores both paid responses, without requesting either again.
    resumed = collector(tmp_path / "raw", monkeypatch, call)
    r2 = recovery(resumed, tmp_path / "out")
    r2.run(rows(resumed))
    assert calls == ["sellersprite", "pangolinfo"]


def test_both_sources_missing_stay_flagged_and_do_not_loop(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append(source)
        return envelope(source, [item("a")])
    c = collector(tmp_path / "raw", monkeypatch, call)
    seed(c, "sellersprite")
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    final = rows(c)
    result = r.annotate(final)
    assert result["pending_tasks"] == 0 and result["missing_reviews_after"] == 1
    assert final[0]["asin_recovery_status"] == "已跨源补抓仍未确认"
    r2 = recovery(c, tmp_path / "out")
    r2.run(final)
    assert calls == ["sellersprite", "pangolinfo"]


def test_failed_alternate_keeps_success_and_retries_only_missing_group(tmp_path, monkeypatch):
    calls, broken = [], {"P1"}
    def call(source, args):
        parent = args["asin"]
        calls.append((source, parent))
        if source == "pangolinfo" and parent in broken:
            raise RuntimeError("connection lost")
        return envelope(source, [item(parent, "V" if source == "pangolinfo" else None)])
    c = collector(tmp_path / "raw", monkeypatch, call)
    for parent in ("P1", "P2"):
        seed(c, "sellersprite", parent)
    r = recovery(c, tmp_path / "out", ("P1", "P2"))
    r.run(rows(c))
    assert r.annotate(rows(c))["pending_tasks"] == 1
    assert calls.count(("pangolinfo", "P1")) == 2 and calls.count(("pangolinfo", "P2")) == 1
    broken.clear()
    resumed = collector(tmp_path / "raw", monkeypatch, call)
    r2 = recovery(resumed, tmp_path / "out", ("P1", "P2"))
    r2.run(rows(resumed))
    assert r2.annotate(rows(resumed))["pending_tasks"] == 0
    assert calls.count(("pangolinfo", "P1")) == 3 and calls.count(("pangolinfo", "P2")) == 1


def test_outside_step18_is_marked_without_new_calls(tmp_path, monkeypatch):
    calls = []
    c = collector(tmp_path / "raw", monkeypatch, lambda s, a: calls.append(s) or envelope(s, [item("x")]))
    seed(c, "sellersprite", "OUTSIDE")
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    assert calls == ["sellersprite"]
    assert next(iter(r.tasks.values()))["status"] == "out_of_scope"


def test_same_text_with_different_native_review_ids_cannot_be_claimed_recovered(tmp_path, monkeypatch):
    c = collector(tmp_path / "raw", monkeypatch, lambda *a: None)
    r = recovery(c, tmp_path / "out")
    original = normalize_record(dict(item("R1"), _source="pangolinfo", _request_asin="P", _queried_asin="P"))
    other = dict(original, review_id="R2", variant_asin="V", asin_role="attributed")
    r.initial = [original]
    result = r.annotate([original, other])
    assert result["missing_reviews_after"] == 1
    audit = list(csv.DictReader((r.directory / "ASIN缺失评论明细.csv").open(encoding="utf-8-sig")))
    assert audit[0]["确认ASIN"] == ""
