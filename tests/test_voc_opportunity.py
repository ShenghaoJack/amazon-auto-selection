"""Evidence validation, coverage, stable cache, long text and incremental lifecycle."""
import json
from threading import Event

import pytest

from core.config import Config
from tools.voc.opportunity import Analyzer, IncrementalAnalyzer, prepare_reviews, validate_parts, windows
from tools.voc.annotate import build_prompt, run_annotation
from tools.voc.dedupe import dedupe_reviews
from tools.voc.normalize import fallback_key


def row(ident="R1", body="Fast but noisy.", **extra):
    return {"review_id": ident, "review_body": body, "review_title": "", "star": 5,
            "source": "pangolinfo", "parent_asin": "P", "variant_asin": "V", **extra}


def aspect(quote, polarity="positive"):
    return {"feature": "速度" if polarity == "positive" else "噪音", "polarity": polarity,
            "quote_field": "text", "quote": quote, "interpretation": "使用体验",
            "scene": "", "user_object": "", "need_outcome": "有效完成任务且可接受",
            "evidence_role": "experienced_benefit" if polarity == "positive" else "reported_failure",
            "target_scope": "review_product"}


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def chat_json(self, prompt, **kwargs):
        self.calls += 1
        payload = json.loads(prompt.split("\n")[-1])
        if isinstance(payload, list):
            return {"parts": [{"part_id": part["part_id"], "reason": "已读", "aspects":
                               [aspect("Fast"), aspect("noisy", "negative")] if "Fast but noisy" in part["text"]
                               else [aspect(part["text"][-min(10, len(part["text"])):], "negative")] if part["text"] else []}
                              for part in payload]}
        if payload.get('task') == 'voc_classify':
            return {'items': [{'id': a['id'], 'existing_attribute_id': '', 'category': '使用体验',
                               'attribute': '任务表现', 'experience': a['interpretation'], 'evidence_kind': 'experienced',
                               'scene': a['scene'], 'user_object': a['user_object'], 'conditions': [],
                               'need_result': a['need_outcome'], 'reason': '原句表达使用体验'} for a in payload['evidence']]}
        if payload.get('task') == 'voc_consolidate':
            return {'groups': [{'category': '使用体验', 'attribute': '任务表现',
                                'members': [a['id'] for a in payload['attributes']]}]}
        if payload.get('task') == 'voc_classification_review':
            return {'decisions': [{'id': a['id'], 'status': 'supported', 'reason': '原句有依据'} for a in payload['evidence']]}
        refs = [a["evidence_id"] for a in payload["evidence"]]
        return {"groups": [{"existing_need_id": payload["catalog"][0]["need_id"] if payload["catalog"] else "",
                            "outcome": "有效完成任务且可接受", "scene": "", "user_object": "", "evidence_ids": refs,
                            "open": {"remaining_gap": "噪音仍影响体验", "gap_type": "both",
                                     "support_ids": refs, "counter_ids": [], "examined_solutions": "未知",
                                     "preserve": "速度", "mechanisms": ["降低传声"], "alternatives": ["缓启动"],
                                     "tradeoffs": ["效率可能下降"], "hypotheses": ["需验证同工况噪音与效率"]}}]}


def config():
    return Config({"CAT": "test", "SITE": "US", "DRY_RUN": False, "LLM_MODEL": "fake", "VOC_LLM_BATCH": 3})


def test_mixed_review_keeps_both_polarities_and_exact_quotes(tmp_path):
    analyzer = Analyzer(config(), tmp_path, "US", llm=FakeLLM(), log=lambda _: None)
    summary = analyzer.finalize([row()])
    chain = json.loads((tmp_path / "chain.json").read_text(encoding="utf-8"))
    assert summary["positive_aspects"] == summary["negative_aspects"] == 1
    for a in chain["aspects"]:
        assert chain["reviews"][0][a["quote_field"]][a["start"]:a["end"]] == a["quote"]
    assert chain["needs"][0]["review_count"] == 1
    assert chain["needs"][0]["positive_review_count"] == chain["needs"][0]["negative_review_count"] == 1
    assert chain["opportunities"][0]["status"] == "candidate"
    assert chain["opportunities"][0]["actual_result"] is None


def test_long_multilingual_review_has_full_window_coverage(tmp_path):
    body = "没有停止工作🙂" * 2500 + "尾部：但清洁很难。"
    review = prepare_reviews([row(body=body)], "US")[0]
    parts = windows(review)
    covered = set()
    for part in parts:
        covered.update(range(part["offset"], part["offset"] + len(part["text"])))
    assert len(covered) == len(body)
    analyzer = Analyzer(config(), tmp_path, "US", llm=FakeLLM(), log=lambda _: None)
    data = analyzer.extract([row(body=body)])
    assert data["coverage"][0]["status"] == "reviewed"
    assert any(a["end"] == len(body) for a in data["aspects"])
    assert body in build_prompt([row(body=body)])


@pytest.mark.parametrize("fault", ["missing", "duplicate", "quote", "empty"])
def test_invalid_or_missing_model_output_cannot_be_reviewed(fault):
    review = prepare_reviews([row()], "US")[0]
    parts = windows(review)
    item = {"part_id": parts[0]["part_id"], "reason": "", "aspects": [aspect("Fast")]}
    payload = {"parts": [item]}
    if fault == "missing":
        payload["parts"] = []
    elif fault == "duplicate":
        payload["parts"].append(item)
    elif fault == "quote":
        item["aspects"][0]["quote"] = "fabricated text"
    else:
        item["aspects"] = []
    with pytest.raises(ValueError):
        validate_parts(payload, parts, {review["review_key"]: review})


def test_whole_mixed_body_cannot_serve_as_praise_and_pain():
    review = prepare_reviews([row()], "US")[0]
    parts = windows(review)
    payload = {"parts": [{"part_id": parts[0]["part_id"], "reason": "已读",
                          "aspects": [aspect("Fast but noisy."), aspect("noisy", "negative")]}]}
    with pytest.raises(ValueError, match="正负原句交叠"):
        validate_parts(payload, parts, {review["review_key"]: review})


def test_legacy_cached_mixed_spans_are_pending_and_recover_on_reextract(tmp_path):
    analyzer = Analyzer(config(), tmp_path, "US", llm=FakeLLM(), log=lambda _: None)
    analyzer.extract([row()])
    cached = next(iter(analyzer.cache.values()))['aspects'][0]
    cached.update(quote="Fast but noisy.", start=0, end=len("Fast but noisy."))
    result = analyzer.extract([row()])
    assert result['coverage'][0]['status'] == 'pending'
    assert result['span_conflicts'] and result['span_pending_aspects']
    assert result['aspects'] == [] and not analyzer.cache
    assert analyzer.extract([row()])['coverage'][0]['status'] == 'reviewed'


def test_cache_reuses_by_content_not_order_and_invalidates_changed_text(tmp_path):
    llm = FakeLLM()
    a = Analyzer(config(), tmp_path, "US", llm=llm, log=lambda _: None)
    a.extract([row("R1"), row("R2")])
    calls = llm.calls
    a.extract([row("R2"), row("R1")])
    assert llm.calls == calls
    a.extract([row("R2", "new evidence"), row("R1")])
    assert llm.calls == calls + 1
    b = Analyzer(config(), tmp_path, "DE", llm=llm, log=lambda _: None)
    b.extract([row("R1")])
    assert llm.calls == calls + 2


def test_conflicting_same_id_is_held_for_reconciliation():
    with pytest.raises(ValueError, match="冲突"):
        prepare_reviews([row(), row(body="Changed")], "US")


def test_open_cross_need_reference_is_rejected_and_retried_with_exact_allowed_ids(tmp_path):
    class RetryLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.synthesis_calls = 0

        def chat_json(self, prompt, **kwargs):
            result = super().chat_json(prompt, **kwargs)
            if '"catalog"' in prompt and '"task": "voc_classify"' not in prompt:
                self.synthesis_calls += 1
                if self.synthesis_calls == 1:
                    result['groups'][0]['open']['counter_ids'] = ['E_wrong_need']
                else:
                    assert 'E_wrong_need' in prompt and 'allowed=' in prompt
            return result

    llm = RetryLLM()
    analyzer = Analyzer(config(), tmp_path, "US", llm=llm, log=lambda _: None)
    analyzer.finalize([row()])
    assert llm.synthesis_calls == 2
    chain = json.loads((tmp_path / "chain.json").read_text(encoding="utf-8"))
    assert chain['opportunities'][0]['counter_ids'] == []


def test_same_text_different_known_ids_are_not_merged():
    a, b = row("R1"), row("R2")
    a["fallback_key"] = b["fallback_key"] = "same"
    unique, _ = dedupe_reviews([a, b])
    assert len(unique) == 2
    assert fallback_key("A", "T", "x" * 400 + "A") != fallback_key("A", "T", "x" * 400 + "B")


def test_catalog_reuse_does_not_inherit_a_different_user_object(tmp_path):
    class ContextLLM(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            result = super().chat_json(prompt, **kwargs)
            payload = json.loads(prompt.split("\n")[-1])
            result['groups'][0]['user_object'] = payload['evidence'][0]['user_object']
            return result

    evidence = [dict(aspect('Fast'), evidence_id=f'E{i}', review_key=f'US::R{i}',
                     admissibility='usable', user_object='object A' if i < 40 else 'object B') for i in range(41)]
    analyzer = Analyzer(config(), tmp_path, "US", llm=ContextLLM(), log=lambda _: None)
    needs, _ = analyzer.synthesize({'aspects': evidence})
    assert len(needs) == 2
    assert {n['user_object'] for n in needs} == {'object A', 'object B'}
    assert evidence[0]['need_id'] != evidence[-1]['need_id']
    assert needs[1]['merge_notes'][0]['resolution'] == 'split_due_to_context_difference'


def test_repeated_synthesis_failure_splits_batches_without_losing_evidence(tmp_path):
    class SmallBatchLLM(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            result = super().chat_json(prompt, **kwargs)
            payload = json.loads(prompt.split("\n")[-1])
            if len(payload['evidence']) > 2:
                result['groups'][0]['evidence_ids'].pop()
            return result

    evidence = [dict(aspect('Fast'), evidence_id=f'E{i}', review_key=f'US::R{i}',
                     admissibility='usable') for i in range(5)]
    logs = []
    analyzer = Analyzer(config(), tmp_path, "US", llm=SmallBatchLLM(), log=logs.append)
    needs, _ = analyzer.synthesize({'aspects': evidence})
    assert {ref for need in needs for ref in need['evidence_ids']} == {f'E{i}' for i in range(5)}
    assert any('拆为更小批次' in line for line in logs)


def test_failed_calls_keep_pending_and_never_finalize_as_success(tmp_path):
    class Bad:
        def chat_json(self, *args, **kwargs):
            return {"parts": []}
    analyzer = Analyzer(config(), tmp_path, "US", llm=Bad(), log=lambda _: None)
    with pytest.raises(RuntimeError, match="未全覆盖"):
        analyzer.finalize([row()])
    data = json.loads((tmp_path / "增量方面进度.json").read_text(encoding="utf-8"))
    assert data["coverage"][0]["status"] == "pending"
    assert not (tmp_path / "chain.json").exists()


def test_incremental_worker_does_not_block_producer_and_finishes_latest_snapshot(tmp_path):
    entered, released = Event(), Event()
    class Blocking(FakeLLM):
        def chat_json(self, *args, **kwargs):
            entered.set()
            assert released.wait(5)
            return super().chat_json(*args, **kwargs)
    worker = IncrementalAnalyzer(config(), tmp_path, "US", llm=Blocking(), log=lambda _: None)
    try:
        worker.submit(lambda: [row("R1")])
        assert entered.wait(5)
        worker.submit(lambda: [row("R1"), row("R2")])
    finally:
        released.set()
        worker.close()
    summary = worker.finalize([row("R1"), row("R2")])
    assert summary["reviews"] == 2


def test_legacy_cache_reorder_remaps_to_current_index(tmp_path, monkeypatch):
    import tools.voc.annotate as legacy
    monkeypatch.setattr(legacy, "LLMClient", lambda cfg: object())
    calls = []
    def classify(llm, rows, *args, **kwargs):
        calls.append(True)
        return [{"i": i, "quote": r["review_body"]} for i, r in enumerate(rows)]
    monkeypatch.setattr(legacy, "classify_batch", classify)
    path = str(tmp_path / "legacy.jsonl")
    run_annotation([row("R1", "one"), row("R2", "two")], config(), cache_path=path, log=lambda _: None)
    labels, _ = run_annotation([row("R2", "two"), row("R1", "one")], config(), cache_path=path, log=lambda _: None)
    assert len(calls) == 1
    assert [x["quote"] for x in labels] == ["two", "one"]


def test_collection_callback_only_observes_persisted_rows(tmp_path):
    from tools.voc.pipeline import Collector
    class Pango:
        def call_raw(self, *args, **kwargs):
            return {"code": 0, "pointCost": 5, "data": {"json": [{"data": {"results": [{"reviewId": "R1", "content": "Fast but noisy."}]}}]}}
    col = Collector.__new__(Collector)
    col.raw_path = {"pangolinfo": str(tmp_path / "raw.jsonl")}
    col.rev_path = {"pangolinfo": str(tmp_path / "reviews.jsonl")}
    col.site, col.calls, col.points, col.rows, col.done = "amz_us", 0, 0, 0, set()
    col.pango, col.log = Pango(), lambda _: None
    observed = []
    def callback():
        observed.append(json.loads((tmp_path / "reviews.jsonl").read_text(encoding="utf-8").splitlines()[0]))
        raise RuntimeError("consumer notification failure")
    col.on_reviews = callback
    receipt = col.one_call("pangolinfo", "P", "P", "critical", "critical", [1, 2, 3], 1)
    assert receipt["status"] == "ok"
    assert observed[0]["reviewId"] == "R1"
    assert col.points == 5


def test_pipeline_defers_variant_extraction_until_collection_finishes(tmp_path, monkeypatch):
    from tools.voc import pipeline as p
    from tools.voc import opportunity
    from tests.test_voc_solutions import FormalLLM, make_catalog
    make_catalog(tmp_path)
    class LocalCollector:
        def __init__(self, directory, *args, **kwargs):
            self.sources, self.done, self.points, self.stopped_reason = ["pangolinfo"], set(), 0, ""
            self.rev_path = {s: str(tmp_path / (s + "-reviews.jsonl")) for s in ("pangolinfo", "sellersprite")}
            self.raw_path = {s: str(tmp_path / (s + "-raw.jsonl")) for s in ("pangolinfo", "sellersprite")}
        def exhausted(self):
            return False
        def all_sources_dead(self):
            return False
        def collect_parents(self, parents, kids, pages, mix):
            for parent in parents:
                self.collect_parent(parent, kids.get(parent, []), pages, mix)
        def collect_parent(self, *args):
            p.append_jsonl(self.rev_path["pangolinfo"], [{"reviewId": "R1", "content": "Fast but noisy.", "star": 5,
                "asin": "V", "_source": "pangolinfo", "_request_asin": "P"}])
            self.on_reviews()
            assert llm.calls == 0
        def rebuild_audit(self, *args):
            return [], {"records": 1, "pages": 1, "points": 5}
    monkeypatch.setattr(p, "Collector", LocalCollector)
    monkeypatch.setattr(p, "resolve_parents", lambda *args, **kwargs: (["P"], {}, {}, "test"))
    llm = FormalLLM()
    meter = object()
    received = []
    def client(cfg, **kwargs):
        received.append(kwargs.get("meter"))
        return llm
    def annotate(*args, **kwargs):
        received.append(kwargs.get("meter"))
        return [{}], {"failed_batches": 0, "failed_reviews": 0}
    def canonicalize(*args, **kwargs):
        received.append(kwargs.get("meter"))
    monkeypatch.setattr(opportunity, "LLMClient", client)
    monkeypatch.setattr(p.annotate, "run_annotation", annotate)
    monkeypatch.setattr(p.annotate, "canonicalize", canonicalize)
    monkeypatch.setattr(p.report, "aggregate", lambda *args: {"neg_rows": [], "pos_rows": [], "safety": [], "neutral3": []})
    monkeypatch.setattr(p.report, "load_audit", lambda *args: ([], {}))
    monkeypatch.setattr(p.report, "write_summary_csv", lambda *args: None)
    def build(*args):
        from pathlib import Path
        Path(args[-1]).write_bytes(b"compatibility-workbook")
    monkeypatch.setattr(p.report, "build_workbook", build)
    cfg = config()
    cfg.set("LLM_API_KEY", "not-a-real-key")
    summary = p.run_voc(str(tmp_path), cfg, out_dir=str(tmp_path / "output"), validate=False, log=lambda _: None, meter=meter)
    assert received == [meter, meter, meter]
    assert summary["opportunity_chain"]["status"] == "complete"
    assert summary["opportunity_chain"]["reviews"] == 1
    assert (tmp_path / "output/2_需求机会链/chain.json").exists()
