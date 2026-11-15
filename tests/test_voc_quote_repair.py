"""Failure-only source-boundary repair, semantic gating, budgets and resume."""
import json

import pytest

from core.errors import LLMError
from tests.test_voc_opportunity import FakeLLM, aspect, config, row
from tools.voc.extraction_budget import ExtractionBudget, estimate_tokens
from tools.voc.opportunity import Analyzer, prepare_reviews, validate_parts, windows
from tools.voc.quote_repair import token_table, resolve_selection, validate_review


def select(part, quote, polarity="positive", field="text", occurrence=0):
    _, locations = token_table(part)
    start = -1
    for _ in range(occurrence + 1):
        start = part[field].find(quote, start + 1)
    assert start >= 0
    first = next(k for k, (f, a, b) in locations.items() if f == field and a == start)
    last = next(k for k, (f, a, b) in locations.items() if f == field and b == start + len(quote))
    result = aspect("MODEL_REWRITE_IS_IGNORED", polarity)
    result.update(quote_field=field, first_token=first, last_token=last)
    return result


class RepairLLM(FakeLLM):
    def __init__(self, choices=None):
        super().__init__()
        self.requests = []
        self.choices = choices or (lambda p: [select(p, "Fast"), select(p, "noisy", "negative")])
        self.review_status = "supported"

    def chat_json(self, prompt, **kwargs):
        self.calls += 1
        self.requests.append((prompt, kwargs))
        payload = json.loads(prompt.split("\n")[-1])
        if isinstance(payload, list):
            return {"parts": [{"part_id": p["part_id"], "reason": "已读", "aspects": [aspect("invented words")]} for p in payload]}
        if payload["task"] == "voc_quote_repair":
            part = payload["part"]
            return {"parts": [{"part_id": part["part_id"], "reason": "原文无实质方面", "aspects": self.choices(part)}]}
        assert payload["task"] == "voc_quote_repair_review"
        return {"coverage_status": "complete", "coverage_reason": "已逐项检查原文及上下文", "zero_aspects_supported": True,
                "decisions": [{"evidence_id": a["evidence_id"], "status": self.review_status,
                               "reason": "原文支持" if self.review_status == "supported" else "省略否定/条件"}
                              for a in payload["aspects"]]}


def engine(tmp_path, llm=None, **settings):
    cfg = config()
    cfg.set("VOC_RETRY_ROUNDS", 0)
    for k, v in settings.items():
        cfg.set(k, v)
    return Analyzer(cfg, tmp_path, "US", llm=llm or RepairLLM(), log=lambda _: None)


def test_invalid_quote_is_repaired_reviewed_audited_and_cached(tmp_path):
    llm = RepairLLM()
    a = engine(tmp_path, llm)
    result = a.extract([row()])
    assert result["coverage"][0]["status"] == "reviewed"
    assert {x["quote"] for x in result["aspects"]} == {"Fast", "noisy"}
    assert llm.calls == 4  # two normal attempts, selection, independent review
    assert all(x["quote_repair"]["semantic_review"] == "supported" for x in result["aspects"])
    files = list((tmp_path / "引文修复记录").glob("*.json"))
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    assert any(x.get("stage") == "normal" and x["model_response"]["parts"][0]["aspects"][0]["quote"] == "invented words" for x in receipts)
    assert any(x.get("semantic_review", {}).get("coverage_status") == "complete" for x in receipts)
    resumed = engine(tmp_path, llm).extract([row()])
    assert resumed["aspects"] == result["aspects"] and llm.calls == 4


def test_overlap_is_replaced_by_disjoint_exact_source_ranges(tmp_path):
    class Overlap(RepairLLM):
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            if isinstance(payload, list):
                self.calls += 1
                return {"parts": [{"part_id": p["part_id"], "reason": "", "aspects":
                                   [aspect("Fast but noisy."), aspect("noisy", "negative")]} for p in payload]}
            return super().chat_json(prompt, **kwargs)
    result = engine(tmp_path, Overlap()).extract([row()])
    assert result["coverage"][0]["status"] == "reviewed" and not result["span_conflicts"]
    assert len(result["aspects"]) == 2


@pytest.mark.parametrize("text,quote", [("Der Körper ist ausgehölt.", "ausgehölt"),
                                       ("a\u00a0b", "a\u00a0b"), ("操作方便但清洁困难🙂", "清洁困难")])
def test_spelling_spaces_and_multilingual_characters_are_program_owned(tmp_path, text, quote):
    llm = RepairLLM(lambda p: [select(p, quote, "negative")])
    result = engine(tmp_path, llm).extract([row(body=text)])
    evidence = result["aspects"][0]
    assert text[evidence["start"]:evidence["end"]] == evidence["quote"] == quote


def test_selected_repeated_phrase_uses_selected_occurrence_not_find_first():
    review = prepare_reviews([row(body="quiet quiet")], "US")[0]
    part = windows(review)[0]
    _, locations = token_table(part)
    response = {"parts": [{"part_id": part["part_id"], "reason": "", "aspects": [select(part, "quiet", occurrence=1)]}]}
    converted, spans = resolve_selection(response, part, locations)
    evidence = validate_parts(converted, [part], {review["review_key"]: review}, quote_locations=spans)[part["part_id"]]["aspects"][0]
    assert evidence["start"] == 6


@pytest.mark.parametrize("fault", ["unknown", "field", "reverse", "foreign_part"])
def test_invalid_selection_is_rejected(fault):
    review = prepare_reviews([row()], "US")[0]
    part = windows(review)[0]
    _, locations = token_table(part)
    choice = select(part, "Fast")
    item = {"part_id": part["part_id"], "reason": "", "aspects": [choice]}
    if fault == "unknown":
        choice["first_token"] = "B999999"
    elif fault == "field":
        choice["quote_field"] = "title"
    elif fault == "reverse":
        choice.update(first_token="B3", last_token="B1")
    else:
        item["part_id"] = "other_review@0"
    with pytest.raises(ValueError):
        resolve_selection({"parts": [item]}, part, locations)


def test_semantically_unsupported_repair_stays_pending(tmp_path):
    llm = RepairLLM()
    llm.review_status = "unsupported"
    a = engine(tmp_path, llm, VOC_QUOTE_REPAIR_ATTEMPTS=1)
    result = a.extract([row()])
    assert result["coverage"][0]["status"] == "pending" and not a.cache
    assert llm.calls == 4
    assert not list((tmp_path / "引文修复断点").glob("*.json"))


def test_valid_normal_output_does_not_call_repair(tmp_path):
    llm = FakeLLM()
    result = engine(tmp_path, llm).extract([row()])
    assert result["coverage"][0]["status"] == "reviewed" and llm.calls == 1
    assert not (tmp_path / "引文修复记录").exists()


def test_repair_zero_aspects_requires_independent_confirmation(tmp_path):
    llm = RepairLLM(lambda p: [])
    result = engine(tmp_path, llm).extract([row(body="Fine.")])
    assert result["coverage"][0]["status"] == "reviewed" and not result["aspects"]
    with pytest.raises(ValueError):
        validate_review({"coverage_status": "complete", "coverage_reason": "泛评", "decisions": [],
                         "zero_aspects_supported": False}, [])


def test_network_timeout_does_not_trigger_quote_repair(tmp_path):
    class Timeout(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            self.calls += 1
            raise LLMError("LLM 请求超时")
    llm = Timeout()
    result = engine(tmp_path, llm).extract([row()])
    assert llm.calls == 1 and result["coverage"][0]["status"] == "pending"


def test_disk_failure_does_not_resend_repair_or_mark_success(tmp_path, monkeypatch):
    import tools.voc.opportunity as module
    original = module.save
    def save(path, data):
        if path.parent.name == "引文修复断点":
            raise OSError("disk full")
        return original(path, data)
    monkeypatch.setattr(module, "save", save)
    llm = RepairLLM()
    a = engine(tmp_path, llm)
    with pytest.raises(OSError, match="disk full"):
        a.extract([row()])
    assert llm.calls == 4 and not a.cache


def test_repair_checkpoint_resumes_without_new_model_calls(tmp_path):
    llm = RepairLLM()
    a = engine(tmp_path, llm)
    review = prepare_reviews([row()], "US")[0]
    part = windows(review)[0]
    value = a.quote_repair.run(part, review, "key", ExtractionBudget(), "引用错误", [])
    assert llm.calls == 2
    again = engine(tmp_path, llm).quote_repair.run(part, review, "key", ExtractionBudget(), "引用错误", [])
    assert again == value and llm.calls == 2


def test_repair_call_budget_stops_unbounded_retry(tmp_path):
    llm = RepairLLM()
    llm.review_status = "unsupported"
    result = engine(tmp_path, llm, VOC_QUOTE_REPAIR_MAX_CALLS=2).extract([row()])
    assert llm.calls == 4 and result["coverage"][0]["status"] == "pending"


def test_expanded_token_payload_is_split_within_budget_and_cached(tmp_path):
    llm = RepairLLM(lambda p: [select(p, "word")])
    a = engine(tmp_path, llm)
    review = prepare_reviews([row(body="word " * 1000)], "US")[0]
    part = windows(review)[0]
    value = a.quote_repair.run(part, review, "big", ExtractionBudget(), "引用错误", [])
    assert len(value["aspects"]) >= 2
    assert all(estimate_tokens(prompt) <= 24000 for prompt, _ in llm.requests)
    before = llm.calls
    assert a.quote_repair.run(part, review, "big", ExtractionBudget(), "引用错误", []) == value
    assert llm.calls == before


def test_cross_window_conflict_is_repaired_in_same_run(tmp_path):
    class AcrossWindows(RepairLLM):
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            if isinstance(payload, list):
                self.calls += 1
                result = []
                for p in payload:
                    aspects = []
                    if "Fast but noisy" in p["text"]:
                        aspects = [aspect("Fast but noisy")] if p["pass"] == "primary" and p["offset"] == 0 else [aspect("noisy", "negative")]
                    result.append({"part_id": p["part_id"], "reason": "片段无额外观点", "aspects": aspects})
                return {"parts": result}
            return super().chat_json(prompt, **kwargs)

    llm = AcrossWindows()
    result = engine(tmp_path, llm, VOC_EXTRACT_WINDOW_CHARS=1024, VOC_EXTRACT_OVERLAP_CHARS=512).extract(
        [row(body="x " * 350 + "Fast but noisy " + "y " * 350)])
    assert result["coverage"][0]["status"] == "reviewed" and not result["span_conflicts"]
    assert {a["quote"] for a in result["aspects"]} == {"Fast", "noisy"}
    assert all(a["quote_repair"]["semantic_review"] == "supported" for a in result["aspects"])
    audit = json.loads((tmp_path / "提取请求审计.json").read_text(encoding="utf-8"))
    assert any(e.get("action") == "coordinated_repair" and e.get("status") == "validated" for e in audit["events"])


def test_repair_selection_must_fit_allowed_original_window():
    review = prepare_reviews([row(body="Fast but noisy.")], "US")[0]
    part = windows(review)[0]
    part["allowed_windows"] = [{"field": "text", "start": 0, "end": 4}]
    _, locations = token_table(part)
    choice = select(part, "Fast but noisy.")
    with pytest.raises(ValueError, match="跨越原提取窗口"):
        resolve_selection({"parts": [{"part_id": part["part_id"], "aspects": [choice]}]}, part, locations)
