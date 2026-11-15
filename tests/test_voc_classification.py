"""Hierarchy coverage, synonym consolidation, qualifiers and downstream gating."""
import copy
import json

import pytest

from core.config import Config
from tools.voc.classification import Classifier
from tools.voc.opportunity import Analyzer


def evidence(ident, quote, polarity="positive", kind="experienced", scene=""):
    return {"evidence_id": ident, "review_key": "US::R1", "feature": quote, "quote": quote,
            "interpretation": quote, "polarity": polarity, "scene": scene, "user_object": "",
            "need_outcome": "可接受的使用体验", "evidence_role": "experienced_benefit", "target_scope": "review_product",
            "admissibility": "usable", "quote_field": "text", "start": 0, "end": len(quote), "test_kind": kind}


class TaxonomyLLM:
    def __init__(self, kinds=None):
        self.calls, self.kinds, self.payloads = 0, kinds or {}, []

    def chat_json(self, prompt, **kwargs):
        self.calls += 1
        p = json.loads(prompt.split("\n")[-1])
        self.payloads.append(p)
        if p["task"] == "voc_classification_review":
            return {"decisions": [{"id": a["id"], "status": "supported", "reason": "原句有依据"} for a in p["evidence"]]}
        if p["task"] == "voc_classify":
            return {"items": [{"id": a["id"], "existing_attribute_id": "", "category": "体验" if i % 2 else "使用体验",
                               "attribute": "噪音" if i % 2 else "声音表现", "experience": a["interpretation"],
                               "evidence_kind": self.kinds.get(a["quote"], "experienced"), "scene": a["scene"],
                               "user_object": "", "conditions": [a["scene"]] if a["scene"] else [],
                               "need_result": a["need_outcome"], "reason": "按完整上下文标记证据性质"}
                              for i, a in enumerate(p["evidence"])]}
        assert p["task"] == "voc_consolidate"
        return {"groups": [{"category": "使用体验", "attribute": "噪音表现", "members": [a["id"] for a in p["attributes"]]}]}


def engine(tmp_path, llm):
    cfg = Config({"LLM_MODEL": "fake", "SITE": "US", "DRY_RUN": False})
    return Analyzer(cfg, tmp_path, "US", llm=llm, log=lambda _: None)


def test_large_catalog_is_bounded_complete_and_resumes_from_cache(tmp_path):
    llm = TaxonomyLLM()
    classifier = Classifier(engine(tmp_path, llm))
    catalog = {f"F{i}": {"category": "使用体验", "attribute": f"属性{i:04d}"} for i in range(250)}
    aliases = {f"T{i}": ident for i, ident in enumerate(catalog, 1)}
    first = classifier.consolidate_catalog(catalog, aliases)
    classifier.validate_groups(first, aliases)
    assert llm.calls == 3
    assert all(len(p["attributes"]) <= 100 for p in llm.payloads)
    second = classifier.consolidate_catalog(catalog, aliases)
    assert second == first and llm.calls == 3


def test_classifier_supports_analyzer_from_running_older_process(tmp_path):
    class OlderAnalyzer:
        directory = tmp_path
        config = Config({"LLM_MODEL": "fake"})
        def call(self, prompt):
            return {"groups": [{"category": "体验", "attribute": "声音", "members": ["T1"]}]}
        def log(self, _message):
            pass
    classifier = Classifier(OlderAnalyzer())
    result = classifier.consolidate_catalog({"F1": {"category": "体验", "attribute": "声音"}}, {"T1": "F1"})
    assert result["groups"][0]["members"] == ["T1"]


def test_catalog_validation_failure_splits_and_reuses_success(tmp_path):
    class IncompleteLLM:
        def __init__(self): self.sizes = []
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            ids = [a["id"] for a in payload["attributes"]]
            self.sizes.append(len(ids))
            if len(ids) > 2:
                ids = ids[:-1]
            return {"groups": [{"category": "体验", "attribute": "属性", "members": ids}]}
    llm = IncompleteLLM()
    analyzer = engine(tmp_path, llm)
    analyzer.config.set("VOC_CONSOLIDATE_BATCH", 4)
    analyzer.config.set("VOC_RETRY_ROUNDS", 0)
    classifier = Classifier(analyzer)
    catalog = {f"F{i}": {"category": "体验", "attribute": f"属性{i}"} for i in range(4)}
    aliases = {f"T{i}": ident for i, ident in enumerate(catalog, 1)}
    result = classifier.consolidate_catalog(catalog, aliases)
    classifier.validate_groups(result, aliases)
    assert llm.sizes == [4, 4, 4, 2, 2]
    count = len(llm.sizes)
    # Successful child batches are reused without retrying the invalid parent.
    classifier.consolidate_catalog(catalog, aliases)
    assert llm.sizes[count:] == []


def test_multilingual_synonyms_share_attribute_without_losing_polarity_or_conditions(tmp_path):
    aspects = [evidence("E1", "quiet", scene="低档"), evidence("E2", "高档很吵", "negative", scene="高档")]
    original = copy.deepcopy(aspects)
    llm = TaxonomyLLM()
    data = {"aspects": aspects, "reviews": [{"review_key": "US::R1", "title": "体验", "text": "quiet; 高档很吵"}]}
    summary = Classifier(engine(tmp_path, llm)).run(data)
    assert summary["raw_feature_labels"] == 2 and summary["attribute_count"] == 1
    assert aspects[0]["feature_id"] == aspects[1]["feature_id"]
    assert aspects[0]["conditions"] == ["低档"] and aspects[1]["conditions"] == ["高档"]
    for before, after in zip(original, aspects):
        for key in before:
            assert before[key] == after[key]
    feature = data["features"][0]
    assert feature["review_count"] == feature["positive_review_count"] == feature["negative_review_count"] == 1
    assert feature["experienced_positive_review_count"] == 1
    assert set(feature["source_labels"]) == {"quiet", "高档很吵"}
    assert llm.payloads[0]["review_context"][0]["text_windows"][0]["text"] == "quiet; 高档很吵"
    assert llm.payloads[0]["review_context"][0]["other_aspects"] == []
    assert {a["quote"] for a in llm.payloads[0]["evidence"]} == {"quiet", "高档很吵"}


def test_generic_speculative_and_hearsay_remain_but_do_not_feed_need(tmp_path):
    kinds = {"great": "general", "seems durable": "expectation", "supposedly": "reported_claim"}
    aspects = [evidence("E" + str(i), quote) for i, quote in enumerate([*kinds, "quiet"])]
    data = {"aspects": aspects, "reviews": []}
    summary = Classifier(engine(tmp_path, TaxonomyLLM(kinds))).run(data)
    assert summary["classified_aspects"] == 4 and summary["need_eligible_aspects"] == 1
    assert [a["need_eligible"] for a in aspects] == [False, False, False, True]
    assert all(a.get("unresolved_reason") for a in aspects[:3])
    assert data["features"][0]["experienced_positive_review_count"] == 1


@pytest.mark.parametrize("bad", ["missing", "duplicate", "unknown", "kind"])
def test_incomplete_or_invalid_classification_cannot_pass(bad):
    llm = TaxonomyLLM()
    p = {"task": "voc_classify", "catalog": [], "evidence": [dict(evidence("E1", "quiet"), id="A1")]}
    response = llm.chat_json("\n" + json.dumps(p))
    if bad == "missing":
        response["items"] = []
    elif bad == "duplicate":
        response["items"] *= 2
    elif bad == "unknown":
        response["items"][0]["existing_attribute_id"] = "T_unknown"
    else:
        response["items"][0]["evidence_kind"] = "verified_by_test"
    with pytest.raises(ValueError):
        Classifier.validate_items(response, {"A1": "E1"}, {})


def test_catalog_review_must_cover_all_source_attributes_once():
    with pytest.raises(ValueError):
        Classifier.validate_groups({"groups": [{"category": "体验", "attribute": "噪音", "members": ["T1", "T1"]}]},
                                   {"T1": "x", "T2": "y"})


def test_classification_cache_reuses_complete_input_and_model_change_invalidates(tmp_path):
    data = {"aspects": [evidence("E1", "quiet")], "reviews": []}
    llm = TaxonomyLLM()
    a = engine(tmp_path, llm)
    Classifier(a).run(copy.deepcopy(data))
    calls = llm.calls
    Classifier(a).run(copy.deepcopy(data))
    assert llm.calls == calls
    a.config.set("LLM_MODEL", "other-model")
    Classifier(a).run(copy.deepcopy(data))
    assert llm.calls == calls + 3


def test_no_need_or_open_from_ineligible_evidence(tmp_path):
    a = evidence("E1", "great")
    a.update(need_eligible=False, classification_kind="general", unresolved_reason="泛评无明确功能")
    llm = TaxonomyLLM()
    needs, opens = engine(tmp_path, llm).synthesize({"aspects": [a], "reviews": []})
    assert needs == opens == [] and llm.calls == 0
    assert a["need_id"] is None and a["unresolved_reason"] == "泛评无明确功能"


def test_independent_review_blocks_unsupported_causal_inference(tmp_path):
    class Reviewer(TaxonomyLLM):
        def chat_json(self, prompt, **kwargs):
            p = json.loads(prompt.split("\n")[-1])
            if p['task'] == 'voc_classification_review':
                return {'decisions': [{'id': a['id'], 'status': 'needs_review',
                                       'reason': '时间上的同时发生不证明产品功能或因果'} for a in p['evidence']]}
            return super().chat_json(prompt, **kwargs)
    data = {'aspects': [evidence('E1', 'No longer using the other device')], 'reviews': []}
    analyzer = engine(tmp_path, Reviewer())
    summary = Classifier(analyzer).run(data)
    assert summary['semantic_held_aspects'] == 1 and summary['need_eligible_aspects'] == 0
    assert data['aspects'][0]['semantic_review_status'] == 'needs_review'
    assert analyzer.synthesize(data) == ([], [])
