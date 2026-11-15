"""Batching, bounded candidates, paid-result migration and independent receipts."""
import copy
import json

import pytest

from tests.test_voc_classification import TaxonomyLLM, engine, evidence
from tools.voc.classification import Classifier, CLASSIFY
from tools.voc.classification_fast import (ContextIndex, ProgressStore, import_legacy, make_task,
                                          canonical_items, apply_items, CatalogCandidates)
from tools.voc.extraction_budget import estimate_tokens
from tools.voc.opportunity import save


def data(count=96):
    aspects, reviews = [], []
    for i in range(count):
        a = evidence("E" + str(i), "quiet " + str(i))
        a["review_key"] = "US::R" + str(i // 6)
        aspects.append(a)
    for key in dict.fromkeys(a["review_key"] for a in aspects):
        reviews.append({"review_key": key, "title": "体验", "text": "quiet and useful"})
    return {"aspects": aspects, "reviews": reviews}


def seed_legacy(a, extracted, waves=1, *, stop_after=None):
    c, catalog, index = Classifier(a), {}, ContextIndex(extracted)
    batches = [extracted["aspects"][i:i + 24] for i in range(0, len(extracted["aspects"]), 24)]
    saved = 0
    for wave_start in range(0, min(len(batches), waves * 4), 4):
        wave = [make_task(b, catalog, index) for b in batches[wave_start:wave_start + 4]]
        completed = []
        for aliases, cats, payload in wave:
            if stop_after is not None and saved >= stop_after:
                break
            response = c.ask("方面分类", CLASSIFY, payload, lambda r: c.validate_items(r, aliases, cats))
            completed.extend(canonical_items(response, aliases, cats, catalog))
            saved += 1
        # Test fixtures use invariant spellings, and otherwise mirror old waves.
        apply_items(completed, catalog, {})
    return saved


def test_legacy_paid_responses_import_without_model_calls(tmp_path):
    extracted, llm = data(), TaxonomyLLM()
    a = engine(tmp_path, llm)
    assert seed_legacy(a, extracted) == 4
    count = llm.calls
    c = Classifier(a)
    store = ProgressStore(c, extracted, CLASSIFY)
    import_legacy(c, extracted, ContextIndex(extracted), store, CLASSIFY)
    assert len(store.assignments) == 96 and llm.calls == count
    assert store.metrics["legacy_cache_requests"] == 4
    reloaded = ProgressStore(c, copy.deepcopy(extracted), CLASSIFY)
    assert len(reloaded.assignments) == 96 and reloaded.import_done


def test_partial_legacy_wave_imports_siblings_and_does_not_guess_later_aliases(tmp_path):
    extracted, llm = data(), TaxonomyLLM()
    a = engine(tmp_path, llm)
    seed_legacy(a, extracted, stop_after=2)
    before = llm.calls
    c = Classifier(a)
    store = ProgressStore(c, extracted, CLASSIFY)
    import_legacy(c, extracted, ContextIndex(extracted), store, CLASSIFY)
    assert len(store.assignments) == 48 and llm.calls == before


def test_large_catalog_candidates_are_bounded_and_allow_new_labels():
    candidates = CatalogCandidates()
    catalog = {str(i): {"category": "类别" + str(i), "attribute": "属性" + str(i)} for i in range(2000)}
    catalog["target"] = {"category": "使用体验", "attribute": "噪音与声音表现"}
    selected = candidates.select(catalog, [dict(evidence("E", "原句"), feature="声音表现")], 64)
    assert len(selected) == 64 and "target" in selected
    assert len(candidates.select(catalog, [evidence("E", "new topic")], 0)) == 0


def test_context_index_deduplicates_evidence_but_retains_other_review_aspects():
    extracted = data(6)
    index = ContextIndex(extracted)
    batch = extracted["aspects"][:3]
    compact = index.context(batch)
    assert len(compact) == 1 and len(compact[0]["other_aspects"]) == 3
    assert compact[0]["text_windows"][0]["text"] == extracted["reviews"][0]["text"]
    assert len(index.context(batch, compact=False)[0]["all_aspects"]) == 6


def test_current_batch_results_are_reused_across_worker_and_batch_changes(tmp_path):
    extracted, llm = data(72), TaxonomyLLM()
    a = engine(tmp_path, llm)
    Classifier(a).run(copy.deepcopy(extracted))
    before = len([p for p in llm.payloads if p["task"] == "voc_classify"])
    a.config.set("VOC_LLM_WORKERS", 1)
    a.config.set("VOC_CLASSIFY_BATCH", 12)
    a.config.set("VOC_CLASSIFY_CATALOG_LIMIT", 8)
    Classifier(a).run(copy.deepcopy(extracted))
    assert len([p for p in llm.payloads if p["task"] == "voc_classify"]) == before


def test_new_classification_uses_real_batches_and_enforces_full_prompt_budget(tmp_path):
    llm, extracted = TaxonomyLLM(), data(96)
    a = engine(tmp_path, llm)
    Classifier(a).run(extracted)
    calls = [p for p in llm.payloads if p["task"] == "voc_classify"]
    assert len(calls) == 4 and all(len(p["evidence"]) == 24 for p in calls)
    assert all(len(p["catalog"]) <= 64 for p in calls)
    assert all(estimate_tokens(CLASSIFY + json.dumps(p, ensure_ascii=False)) + 2048 <= 48000 for p in calls)


def test_overbudget_single_evidence_is_never_sent(tmp_path):
    llm = TaxonomyLLM()
    a = engine(tmp_path, llm)
    a.config.set("VOC_RETRY_ROUNDS", 0)
    extracted = {"aspects": [evidence("E1", "原文" * 30000)], "reviews": []}
    with pytest.raises(RuntimeError, match="待重试"):
        Classifier(a).run(extracted)
    assert llm.calls == 0


def test_corrupt_or_other_source_receipts_do_not_count_as_coverage(tmp_path):
    extracted, llm = data(6), TaxonomyLLM()
    a = engine(tmp_path, llm)
    c = Classifier(a)
    store = ProgressStore(c, extracted, CLASSIFY)
    store.directory.mkdir(parents=True, exist_ok=True)
    save(store.directory / "wrong.json", {"fingerprint": "other", "items": [], "items_digest": "wrong"})
    (store.directory / "partial.json").write_text('{"items":', encoding="utf-8")
    assert not ProgressStore(c, extracted, CLASSIFY).assignments


def test_semantics_still_require_complete_independent_review(tmp_path):
    class ReviewFails(TaxonomyLLM):
        def chat_json(self, prompt, **kwargs):
            p = json.loads(prompt.split("\n")[-1])
            if p["task"] == "voc_classification_review":
                return {"decisions": []}
            return super().chat_json(prompt, **kwargs)
    a = engine(tmp_path, ReviewFails())
    a.config.set("VOC_RETRY_ROUNDS", 0)
    with pytest.raises(RuntimeError):
        Classifier(a).run(data(6))


def test_model_change_invalidates_evidence_receipts(tmp_path):
    extracted, llm = data(24), TaxonomyLLM()
    a = engine(tmp_path, llm)
    Classifier(a).run(copy.deepcopy(extracted))
    first = len([p for p in llm.payloads if p["task"] == "voc_classify"])
    a.config.set("LLM_MODEL", "new-model")
    Classifier(a).run(copy.deepcopy(extracted))
    assert len([p for p in llm.payloads if p["task"] == "voc_classify"]) == first + 1


def test_unpublished_paid_batch_receipt_recovers_without_reclassification(tmp_path, monkeypatch):
    from pathlib import Path
    from tools.voc import storage
    extracted, llm = data(24), TaxonomyLLM()
    a = engine(tmp_path, llm)
    original = storage.atomic_replace

    def blocked(source, target):
        if Path(target).parent.parent.name == "分类逐批断点":
            raise PermissionError("receipt publication locked")
        return original(source, target)

    monkeypatch.setattr(storage, "atomic_replace", blocked)
    with pytest.raises(PermissionError):
        Classifier(a).run(copy.deepcopy(extracted))
    assert len([p for p in llm.payloads if p["task"] == "voc_classify"]) == 1
    monkeypatch.setattr(storage, "atomic_replace", original)
    result = copy.deepcopy(extracted)
    Classifier(a).run(result)
    assert result["classification"]["classified_aspects"] == 24
    assert len([p for p in llm.payloads if p["task"] == "voc_classify"]) == 1
def test_group_review_batches_does_not_increase_calls_and_keeps_coverage():
    from tools.voc.classification_fast import group_review_batches
    aspects = [{"review_key": key, "evidence_id": str(i)} for i, key in enumerate(["a", "b", "a", "b", "c", "c"])]
    batches = group_review_batches(aspects, 3)
    assert len(batches) == 2
    assert sorted(a["evidence_id"] for batch in batches for a in batch) == [str(i) for i in range(6)]
    assert all(len(batch) <= 3 for batch in batches)
