import csv
from copy import deepcopy
import json

import openpyxl
import pytest

from tests.test_voc_opportunity import config, row
from tests.test_voc_solutions import FormalLLM, make_catalog
from tools.voc.identity import admit_reviews, product_identity, validate_identities
from tools.voc.opportunity import Analyzer
from tools.voc.solutions import load_catalog


class Recorder(FormalLLM):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def chat_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return super().chat_json(prompt, **kwargs)


def test_outside_variant_is_excluded_before_any_extraction(tmp_path):
    catalog = make_catalog(tmp_path)
    llm = Recorder()
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=catalog, llm=llm, log=lambda _: None)
    engine.finalize([row("IN"), row("OUTSIDE-ID", body="NEVER_SEND_THIS_BODY_TO_LLM", variant_asin="OUTSIDE")])
    assert all("NEVER_SEND_THIS_BODY_TO_LLM" not in prompt and "OUTSIDE-ID" not in prompt for prompt in llm.prompts)
    chain = json.loads((engine.directory / "chain.json").read_text(encoding="utf-8"))
    assert [r["review_id"] for r in chain["reviews"]] == ["IN"]
    assert chain["admission_summary"]["excluded_reviews"] == 1
    excluded = json.loads((engine.directory / "排除评论原文.json").read_text(encoding="utf-8"))
    assert excluded[0]["review_body"] == "NEVER_SEND_THIS_BODY_TO_LLM"
    assert excluded[0]["extraction_status"] == "not_extracted"


def test_direct_extract_uses_same_admission_gate(tmp_path):
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=Recorder(), log=lambda _: None)
    result = engine.extract([row("IN"), row("OUT", variant_asin="OUTSIDE")])
    assert [r["review_id"] for r in result["reviews"]] == ["IN"]


def test_all_excluded_stops_without_model_and_preserves_audit(tmp_path):
    llm = Recorder()
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=llm, log=lambda _: None)
    with pytest.raises(RuntimeError, match="准入后无可分析"):
        engine.finalize([row(variant_asin="OUTSIDE")])
    assert not llm.prompts
    audit = json.loads((engine.directory / "评论准入.json").read_text(encoding="utf-8"))
    assert audit["records"][0]["reason"] == "variant_not_in_step18"


def two_brand_catalog(tmp_path):
    catalog = make_catalog(tmp_path)
    wb = openpyxl.load_workbook(catalog["source"]["path"])
    for name in ("属性标注", "来源", "依据"):
        wb[name]["B3"] = "PB"
        wb[name]["C3"] = "Brand B"
    wb.save(catalog["source"]["path"])
    wb.close()
    return load_catalog(tmp_path, "test", "US")


def test_shared_need_is_scoped_per_variant_with_parent_brand_and_step18(tmp_path):
    catalog = two_brand_catalog(tmp_path)
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=catalog, llm=Recorder(), log=lambda _: None)
    engine.finalize([row("A", variant_asin="V", parent_asin="P"), row("B", variant_asin="B", parent_asin="PB")])
    chain = json.loads((engine.directory / "chain.json").read_text(encoding="utf-8"))
    assert len(chain["need_groups"]) == 1 and len(chain["needs"]) == 2
    assert {n["variant_asin"] for n in chain["needs"]} == {"V", "B"}
    assert len({n["need_id"] for n in chain["needs"]}) == 2
    assert len({n["need_group_id"] for n in chain["needs"]}) == 1
    assert {o["variant_asin"] for o in chain["opportunities"]} == {"V", "B"}
    assert {p["variant_asin"] for p in chain["proposals"]} == {"V", "B"}
    for nodes in (chain["aspects"], chain["needs"], chain["solutions"], chain["opportunities"], chain["proposals"]):
        assert nodes
        for node in nodes:
            assert all(key in node for key in ("variant_asin", "parent_asin", "brand", "step18_link"))
            if node["variant_asin"] == "B":
                assert node["parent_asin"] == "PB" and node["brand"] == "Brand B"
            if node["variant_asin"] == "V":
                assert node["parent_asin"] == "P" and node["brand"] == "Brand"
    validate_identities(chain)
    with (engine.directory / "待验证方案.csv").open(encoding="utf-8-sig", newline="") as stream:
        proposals = list(csv.DictReader(stream))
    assert {p["brand"] for p in proposals} == {"Brand", "Brand B"}
    view = json.loads((engine.directory / "逐条评论分类.json").read_text(encoding="utf-8"))
    assert {r["brand"] for r in view} == {"Brand", "Brand B"}
    assert all(r["步骤18关联"]["source"]["sha256"] == catalog["source"]["sha256"] for r in view)
    comparison = [s for s in chain["solutions"] if s["solution_role"] == "comparison"]
    assert comparison and all(s["variant_asin"] != s["target_variant_asin"] for s in comparison)
    assert all(not s["support_ids"] and not s["counter_ids"] for s in comparison)


def test_identity_validator_rejects_cross_variant_proposal(tmp_path):
    catalog = two_brand_catalog(tmp_path)
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=catalog, llm=Recorder(), log=lambda _: None)
    engine.finalize([row()])
    chain = json.loads((engine.directory / "chain.json").read_text(encoding="utf-8"))
    wrong = deepcopy(chain)
    product = next(p for p in catalog["products"] if p["variant_asin"] == "B")
    wrong["proposals"][0].update(product_identity(product, catalog))
    with pytest.raises(ValueError, match="方案归属"):
        validate_identities(wrong)


def test_step18_fills_parent_brand_and_case_normalization_is_consistent(tmp_path):
    catalog = make_catalog(tmp_path)
    accepted, excluded, _, _ = admit_reviews([row(variant_asin=" v ", parent_asin=None, brand=None),
                                            row("B", brand=" brand ")], catalog, "US")
    assert not excluded
    assert all(r["variant_asin"] == "V" and r["parent_asin"] == "P" and r["brand"] == "Brand" for r in accepted)
    assert accepted[0]["_voc_original_identity"]["parent_asin"] is None


@pytest.mark.parametrize("outside_only", [False, True])
def test_pipeline_filters_compatibility_annotation_and_empty_scope(tmp_path, monkeypatch, outside_only):
    from tools.voc import pipeline as p
    make_catalog(tmp_path)
    class Collector:
        def __init__(self, directory, *args, **kwargs):
            self.sources, self.done, self.points, self.stopped_reason = ["pangolinfo"], set(), 0, ""
            self.rev_path = {s: str(tmp_path / f"{s}-reviews.jsonl") for s in ("pangolinfo", "sellersprite")}
            self.raw_path = {s: str(tmp_path / f"{s}-raw.jsonl") for s in ("pangolinfo", "sellersprite")}
        def exhausted(self):
            return False
        def all_sources_dead(self):
            return False
        def collect_parents(self, parents, kids, pages, mix):
            for parent in parents:
                self.collect_parent(parent, kids.get(parent, []), pages, mix)
        def collect_parent(self, *args):
            raw = [{"reviewId": "OUT", "content": "OUTSIDE_SENTINEL", "asin": "OUTSIDE", "star": 5}]
            if not outside_only:
                raw.append({"reviewId": "IN", "content": "Fast but noisy.", "asin": "V", "star": 5})
            p.append_jsonl(self.rev_path["pangolinfo"], [dict(r, _source="pangolinfo", _request_asin="P") for r in raw])
        def rebuild_audit(self, *args):
            return [], {"records": 1, "pages": 1, "points": 0}
    monkeypatch.setattr(p, "Collector", Collector)
    monkeypatch.setattr(p, "resolve_parents", lambda *args, **kwargs: (["P"], {}, {}, "test"))
    called = []
    def annotate(rows, *args, **kwargs):
        called.extend(rows)
        return [{} for _ in rows], {"failed_batches": 0, "failed_reviews": 0}
    monkeypatch.setattr(p.annotate, "run_annotation", annotate)
    monkeypatch.setattr(p.annotate, "canonicalize", lambda *args, **kwargs: None)
    monkeypatch.setattr(p.report, "aggregate", lambda *args: {"neg_rows": [], "pos_rows": [], "safety": [], "neutral3": []})
    monkeypatch.setattr(p.report, "load_audit", lambda *args: ([], {}))
    monkeypatch.setattr(p.report, "write_summary_csv", lambda *args: None)
    def build(*args):
        from pathlib import Path
        Path(args[-1]).write_bytes(b"compatibility")
    monkeypatch.setattr(p.report, "build_workbook", build)
    cfg = config()
    cfg.set("VOC_OPPORTUNITY_ENABLED", False)
    cfg.set("LLM_API_KEY", "fake")
    out = tmp_path / "pipeline"
    if outside_only:
        with pytest.raises(RuntimeError, match="准入后无可分析"):
            p.run_voc(str(tmp_path), cfg, out_dir=str(out), validate=False, log=lambda _: None)
        assert not called
        summary = json.loads((out / "VOC流水线摘要.json").read_text(encoding="utf-8"))
        assert summary["unique_reviews"] == 0
    else:
        summary = p.run_voc(str(tmp_path), cfg, out_dir=str(out), validate=False, log=lambda _: None)
        assert [r["variant_asin"] for r in called] == ["V"]
        assert summary["raw_unique_reviews"] == 2 and summary["unique_reviews"] == 1 and summary["excluded_reviews"] == 1
