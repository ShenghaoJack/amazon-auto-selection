"""Whitespace recovery preserves exact raw evidence; model edits stay rejected."""
import json

import pytest

from tests.test_voc_opportunity import FakeLLM, aspect, config, row
from tests.test_voc_quote_fields import validate
from tools.voc.opportunity import Analyzer, prepare_reviews, windows
from tools.voc.quote_spans import whitespace_quote_span


@pytest.mark.parametrize("source,quote", [
    ("The\u00a0lifetime warranty is a lie.", "The lifetime warranty is a lie."),
    ("its manufacturer\u00a0box.", "its manufacturer box."),
    ("tall.\u00a0 Which is good", "tall.  Which is good"),
    ("困る\n\t 非常に困る🙂", "困る 非常に困る🙂"),
])
def test_whitespace_only_mapping_preserves_raw_characters(source, quote):
    review = prepare_reviews([row(body="prefix " + source + " tail")], "US")[0]
    result = validate(review, windows(review)[0], [aspect(quote, "negative")])[0]
    assert result["quote"] == source
    assert result["start"] == 7
    assert review["text"][result["start"]:result["end"]] == source
    assert result["quote_text_correction"] == {"method": "whitespace_only", "model_quote": quote}


@pytest.mark.parametrize("source,quote", [
    ("nicht effektiv ausgehölt.", "nicht effektiv ausgehöhlt."),
    ("are the only indicator.", "are only indicator."),
    ("goes around versus up and down", "goes around versus up" + " to match"),
    ("did not work", "did work"),
    ("It works!", "It works."),
    ("a b", "ab"),
])
def test_spelling_edits_omissions_polarity_changes_and_punctuation_are_not_repaired(source, quote):
    review = prepare_reviews([row(body=source)], "US")[0]
    with pytest.raises(ValueError, match="连续原文"):
        validate(review, windows(review)[0], [aspect(quote)])


def test_repeated_normalized_phrase_is_ambiguous_and_not_guessed():
    assert whitespace_quote_span("a\u00a0b then a\nb", "a b") is None


def test_exact_quote_keeps_original_location_when_normalized_text_repeats():
    review = prepare_reviews([row(body="a\u00a0b then a b")], "US")[0]
    result = validate(review, windows(review)[0], [aspect("a b")])[0]
    assert result["start"] == 9 and "quote_text_correction" not in result


def test_whitespace_mapping_combines_with_title_field_correction():
    review = prepare_reviews([row(body="Different text", review_title="Very\u00a0good🙂")], "US")[0]
    result = validate(review, windows(review)[0], [aspect("Very good🙂")])[0]
    assert result["quote_field"] == "title" and result["quote"] == "Very\u00a0good🙂"
    assert result["quote_field_correction"]["from"] == "text"


def test_whitespace_mapping_preserves_nonzero_window_offset():
    review = prepare_reviews([row(body="0123456789a\u00a0b rest")], "US")[0]
    part = windows(review, size=10, overlap=2)[1]
    result = validate(review, part, [aspect("a b")])[0]
    assert result["start"] == 10 and result["end"] == 13
    assert review["text"][10:13] == result["quote"]


def test_whitespace_mapping_does_not_bypass_current_window_or_context_boundary():
    review = prepare_reviews([row(body="123456789012345 Outside\u00a0window")], "US")[0]
    part = windows(review, size=10, overlap=2)[0]
    part["context_only"] = "Outside window"
    with pytest.raises(ValueError, match="连续原文"):
        validate(review, part, [aspect("Outside window")])


def test_whitespace_recovery_still_enforces_opposite_polarity_span_conflicts():
    review = prepare_reviews([row(body="Fast\u00a0but noisy")], "US")[0]
    with pytest.raises(ValueError, match="正负原句交叠") as error:
        validate(review, windows(review)[0], [aspect("Fast but noisy"), aspect("noisy", "negative")])
    assert '"polarity": "positive"' in str(error.value)
    assert '"polarity": "negative"' in str(error.value)


def test_whitespace_recovery_needs_one_model_call_and_survives_resume(tmp_path):
    class WhitespaceLLM(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            self.calls += 1
            parts = json.loads(prompt.split("\n")[-1])
            return {"parts": [{"part_id": p["part_id"], "reason": "已读",
                               "aspects": [aspect("The lifetime warranty is a lie.", "negative")]} for p in parts]}

    llm, logs = WhitespaceLLM(), []
    rows = [row(body="The\u00a0lifetime warranty is a lie.")]
    result = Analyzer(config(), tmp_path, "US", llm=llm, log=logs.append).extract(rows)
    assert llm.calls == 1 and result["coverage"][0]["status"] == "reviewed"
    assert result["aspects"][0]["quote"] == rows[0]["review_body"]
    assert any("空白字符差异" in message for message in logs)
    audit = json.loads((tmp_path / "提取请求审计.json").read_text(encoding="utf-8"))
    assert audit["events"][0]["quote_text_corrections"][0]["method"] == "whitespace_only"
    resumed = Analyzer(config(), tmp_path, "US", llm=llm, log=logs.append).extract(rows)
    assert resumed["aspects"] == result["aspects"] and llm.calls == 1


def test_bad_quote_does_not_expand_into_recursive_window_calls(tmp_path):
    class InvalidLLM(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            self.calls += 1
            parts = json.loads(prompt.split("\n")[-1])
            return {"parts": [{"part_id": p["part_id"], "reason": "已读",
                               "aspects": [aspect("invented quotation")]} for p in parts]}

    cfg, llm = config(), InvalidLLM()
    cfg.set("VOC_RETRY_ROUNDS", 0)
    cfg.set("VOC_QUOTE_REPAIR_ENABLED", False)  # isolate the normal no-recursive-window path
    result = Analyzer(cfg, tmp_path, "US", llm=llm, log=lambda _: None).extract([row(body="source " * 600)])
    assert llm.calls == 2
    assert result["coverage"][0]["status"] == "pending" and not result["aspects"]
    audit = json.loads((tmp_path / "提取请求审计.json").read_text(encoding="utf-8"))
    assert not any(e.get("action") == "split_window" for e in audit["events"])
