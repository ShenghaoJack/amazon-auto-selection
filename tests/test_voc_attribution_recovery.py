"""Primary endpoint, honest identities, cross-source recovery and cached fallback."""
import copy

import pytest

from tests.test_voc_mcp_recovery import collector, envelope
from tests.test_voc_normalize import PANGO, SS
from tools.voc.dedupe import dedupe_reviews
from tools.voc.normalize import normalize_record
from tools.voc.pipeline import PANGOLIN_MCP_URL


def test_primary_endpoint_matches_connected_service():
    assert PANGOLIN_MCP_URL == "https://mcp.pangolinfo.com/mcp"


def test_unknown_seller_variant_never_uses_queried_parent_as_identity():
    r = normalize_record(dict(SS, _queried_asin="PARENT"))
    assert r["variant_asin"] == "" and r["asin_role"] == "unknown"
    assert r["queried_asin"] == "PARENT"


@pytest.mark.parametrize("field", ["asin", "childAsin", "variantAsin", "variant_asin"])
def test_explicit_seller_review_variant_is_retained(field):
    r = normalize_record(dict(SS, **{field: " v ", "_queried_asin": "PARENT"}))
    assert r["variant_asin"] == "V" and r["asin_role"] == "attributed"
    assert r["queried_asin"] == "PARENT"


def test_conflicting_seller_fields_cannot_be_admitted():
    r = normalize_record(dict(SS, childAsin="V", variantAsin="B"))
    assert not r["variant_asin"] and r["asin_role"] == "conflict"


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_source_merge_promotes_only_explicit_variant(reverse):
    pango, seller = normalize_record(PANGO), normalize_record(SS)
    rows = [pango, seller]
    if reverse:
        rows.reverse()
    unique, _ = dedupe_reviews(rows)
    assert len(unique) == 1
    assert unique[0]["variant_asin"] == pango["variant_asin"]
    assert unique[0]["asin_role"] == "attributed"
    assert set(unique[0]["sources"]) == {"pangolinfo", "sellersprite"}


def test_duplicate_review_with_conflicting_confirmed_variants_remains_held():
    a, b = normalize_record(PANGO), normalize_record(dict(PANGO, asin="OTHER"))
    unique, _ = dedupe_reviews([a, b, copy.deepcopy(a)])
    assert len(unique) == 1 and not unique[0]["variant_asin"]
    assert unique[0]["asin_role"] == "conflict"
    assert set(unique[0]["identity_candidates"]) == {a["variant_asin"], b["variant_asin"]}


def test_primary_repair_bypasses_unowned_fallback_but_never_recharges_it(tmp_path, monkeypatch):
    calls, broken = [], {"primary": True}
    def call(source, args):
        calls.append((source, args["asin"]))
        if source == "pangolinfo" and broken["primary"]:
            raise RuntimeError("old endpoint SSL error")
        result = envelope(source)
        if source == "pangolinfo":
            result["data"]["json"][0]["data"]["results"] = ([] if args["asin"] == "P" else [
                {"reviewId": "R1", "asin": "V", "content": "works", "star": 4}])
        return result
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", ["V"], 1, "critical:1")
    assert calls == [("pangolinfo", "P"), ("sellersprite", "P")]
    broken["primary"] = False
    catalog = {"source": {"sha256": "fixture"}, "products": [{"parent_asin": "P", "variant_asin": "V"}]}
    assert c.repair_attribution(catalog, ["P"], 1, "critical:1")
    assert calls == [("pangolinfo", "P"), ("sellersprite", "P"), ("pangolinfo", "P"), ("pangolinfo", "V")]
    assert not c.failed_parents
    rows = c._read_raw(c.rev_path["pangolinfo"])
    assert normalize_record(rows[0])["variant_asin"] == "V"
    before = list(calls)
    assert c.repair_attribution(catalog, ["P"], 1, "critical:1")
    assert calls == before  # Both the empty parent probe and child success are reusable.


def test_identity_repair_is_restricted_to_step18_parent_scope(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail("范围外父体不应被补抓")
    c = collector(tmp_path, monkeypatch, unexpected)
    catalog = {"source": {}, "products": [{"parent_asin": "OTHER", "variant_asin": "V"}]}
    assert not c.repair_attribution(catalog, ["P"], 1, "critical:1")


@pytest.mark.parametrize("status, expected", [("pending", True), ("running", True), ("checked", False)])
def test_interrupted_repair_remains_pending_with_partial_success(tmp_path, status, expected):
    import json
    from tools.voc.pipeline import attribution_repair_pending
    (tmp_path / "归属补采记录.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    assert attribution_repair_pending(str(tmp_path)) is expected


def test_pipeline_recovers_zero_admission_before_compatibility_llm(tmp_path, monkeypatch):
    from pathlib import Path
    from tests.test_voc_opportunity import config
    from tests.test_voc_solutions import make_catalog
    from tools.voc import pipeline as p
    make_catalog(tmp_path)
    calls = []
    class Client:
        def __init__(self, source):
            self.source = source
        def call_raw(self, tool, args):
            calls.append(self.source)
            if self.source == "pangolinfo" and calls.count("pangolinfo") == 1:
                raise RuntimeError("old endpoint SSL failure")
            result = envelope(self.source)
            if self.source == "pangolinfo":
                result["data"]["json"][0]["data"]["results"][0].update(asin="V", star=4)
            return result
    monkeypatch.setattr(p, "pango_client", lambda cfg: Client("pangolinfo"))
    monkeypatch.setattr(p, "ss_client", lambda cfg: Client("sellersprite"))
    monkeypatch.setattr(p, "resolve_parents", lambda *a, **k: (["P"], {"P": ["V"]}, {}, "fixture"))
    seen = []
    def annotate(rows, *args, **kwargs):
        seen.extend(rows)
        assert all(r["variant_asin"] == "V" and r["asin_role"] == "attributed" for r in rows)
        return [{} for _ in rows], {"failed_batches": 0, "failed_reviews": 0}
    monkeypatch.setattr(p.annotate, "run_annotation", annotate)
    monkeypatch.setattr(p.annotate, "canonicalize", lambda *a, **k: None)
    monkeypatch.setattr(p.report, "aggregate", lambda *a: {"neg_rows": [], "pos_rows": [], "safety": [], "neutral3": []})
    monkeypatch.setattr(p.report, "load_audit", lambda *a: ([], {}))
    monkeypatch.setattr(p.report, "write_summary_csv", lambda *a: None)
    monkeypatch.setattr(p.report, "build_workbook", lambda *a: Path(a[-1]).write_bytes(b"isolated"))
    cfg = config()
    for key, value in {"VOC_OPPORTUNITY_ENABLED": False, "VOC_PAGES_PER_PARENT": 1,
                       "VOC_STAR_MIX": "critical:1", "LLM_API_KEY": "fake"}.items():
        cfg.set(key, value)
    out = tmp_path / "output"
    summary = p.run_voc(str(tmp_path), cfg, out_dir=str(out), validate=False, log=lambda _: None)
    assert seen and summary["unique_reviews"] == 1
    assert calls == ["pangolinfo", "pangolinfo"]  # primary retry succeeds; no fallback credit spent
