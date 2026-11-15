"""Exact quote-field recovery must avoid retries without widening evidence scope."""
import json

import pytest

from tests.test_voc_opportunity import FakeLLM, aspect, config, row
from tools.voc.opportunity import Analyzer, prepare_reviews, validate_parts, windows


TITLE = "Arrived damaged and kind of cheaply made"


def validate(review, part, aspects):
    payload = {"parts": [{"part_id": part["part_id"], "reason": "已读", "aspects": aspects}]}
    return validate_parts(payload, [part], {review["review_key"]: review})[part["part_id"]]["aspects"]


@pytest.mark.parametrize("quote", [TITLE, "kind of cheaply made"])
def test_real_incident_title_mislabeled_as_body_is_recovered(quote):
    review = prepare_reviews([row(body="The top was damaged in shipping.", review_title=TITLE)], "US")[0]
    supplied = aspect(quote, "negative")
    result = validate(review, windows(review)[0], [supplied])[0]
    assert result["quote_field"] == "title"
    assert result["quote"] == quote
    assert review["title"][result["start"]:result["end"]] == quote
    assert result["quote_field_correction"] == {
        "from": "text", "to": "title", "method": "exact_current_window"}
    assert supplied["quote_field"] == "text"  # preserve original response


@pytest.mark.parametrize("field", ["text", "title"])
def test_corrected_fields_keep_offsets_in_later_multilingual_windows(field):
    text = "0123456789很难清洁🙂但效果好"
    review = prepare_reviews([row(body=text if field == "text" else "",
                                  review_title=text if field == "title" else "短标题")], "US")[0]
    part = next(p for p in windows(review, size=10, overlap=2)
                if p.get("title_offset", p["offset"]) == 8 and "很难" in p[field])
    supplied = dict(aspect("很难", "negative"), quote_field="title" if field == "text" else "text")
    result = validate(review, part, [supplied])[0]
    assert result["quote_field"] == field
    assert result["start"] == 10
    assert review[field][result["start"]:result["end"]] == "很难"


@pytest.mark.parametrize("supplied_field", ["text", "title"])
def test_matching_declared_field_is_preserved_even_when_both_contain_quote(supplied_field):
    review = prepare_reviews([row(body="Fast", review_title="Fast")], "US")[0]
    result = validate(review, windows(review)[0], [dict(aspect("Fast"), quote_field=supplied_field)])[0]
    assert result["quote_field"] == supplied_field
    assert "quote_field_correction" not in result


@pytest.mark.parametrize("quote", ["Arrived damaged. Kind of cheaply made", "Arrived damaged cheaply made",
                                   "Arrived damaged and kind of cheaply made!", "Another review"])
def test_paraphrase_splicing_and_other_review_quotes_still_fail(quote):
    review = prepare_reviews([row(body="The top was damaged.", review_title=TITLE)], "US")[0]
    with pytest.raises(ValueError, match="连续原文"):
        validate(review, windows(review)[0], [aspect(quote)])


def test_recovery_cannot_use_full_review_or_context_outside_current_window():
    review = prepare_reviews([row(body="0123456789 OUTSIDE", review_title="Title")], "US")[0]
    part = windows(review, size=10, overlap=2)[0]
    part["context_only"] = "OUTSIDE"
    with pytest.raises(ValueError, match="连续原文"):
        validate(review, part, [dict(aspect("OUTSIDE"), quote_field="title")])


def test_field_correction_does_not_bypass_polarity_conflict_validation():
    review = prepare_reviews([row(body="Other text", review_title="Fast but noisy")], "US")[0]
    with pytest.raises(ValueError, match="正负原句交叠"):
        validate(review, windows(review)[0], [aspect("Fast but noisy"), aspect("noisy", "negative")])


def test_swapped_field_is_validated_in_one_call_audited_and_resumed(tmp_path):
    class SwappedLLM(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            self.calls += 1
            parts = json.loads(prompt.split("\n")[-1])
            return {"parts": [{"part_id": p["part_id"], "reason": "已读",
                               "aspects": [aspect(p["title"], "negative")]} for p in parts]}

    llm, logs = SwappedLLM(), []
    rows = [row("R2MH430536R3QG", body="The top was damaged in shipping.", review_title=TITLE)]
    engine = Analyzer(config(), tmp_path, "US", llm=llm, log=logs.append)
    result = engine.extract(rows)
    assert llm.calls == 1
    assert result["coverage"][0]["status"] == "reviewed"
    assert result["aspects"][0]["quote_field"] == "title"
    audit = json.loads((tmp_path / "提取请求审计.json").read_text(encoding="utf-8"))
    assert len(audit["events"]) == 1
    event = audit["events"][0]
    assert event["status"] == "validated"
    assert event["quote_field_corrections"][0]["from"] == "text"
    assert event["quote_field_corrections"][0]["to"] == "title"
    assert any("无需模型重试" in message for message in logs)
    resumed = Analyzer(config(), tmp_path, "US", llm=llm, log=logs.append).extract(rows)
    assert llm.calls == 1
    assert resumed["aspects"] == result["aspects"]
