import json

import pytest

from tools.voc.classification import Classifier
from tools.voc.solution_retry import install_solution_retry, solution_request, validate_conditional_boundaries
from tools.voc.solutions import require_refs


def _payload():
    return {
        "subject_id": "ASIN1",
        "evidence": [
            {"evidence_id": "Epositive", "polarity": "positive"},
            {"evidence_id": "Enegative", "polarity": "negative"},
        ],
        "cells": [{"cell_id": "SCattribute"}],
    }


def test_solution_prompt_explains_polarity_and_empty_reference_sets():
    request = "指令\n" + json.dumps({**_payload(), "validation_feedback": "上次编号错误"})
    enhanced = solution_request(request, "已有解法核查")
    parsed = json.loads(enhanced.rsplit("\n", 1)[1])
    assert parsed["evidence"] == _payload()["evidence"]
    assert parsed["cells"] == _payload()["cells"]
    feedback = parsed["validation_feedback"]
    assert "上次编号错误" in feedback
    assert '"support_ids":["Epositive"]' in feedback
    assert '"counter_ids":["Enegative"]' in feedback
    assert '"boundary_ids":[]' in feedback
    assert '"cell_ids":["SCattribute"]' in feedback
    assert "不能放评论证据字段" in feedback
    assert solution_request(request, "Need功能字段关联") == request
    assert solution_request("not-json", "已有解法核查") == "not-json"


def test_solution_retry_keeps_strict_validation_and_reuses_original_cache(tmp_path):
    class Engine:
        def __init__(self):
            self.directory = tmp_path
            self.config = {"LLM_MODEL": "test", "LLM_BASE_URL": "local"}
            self.requests = []
            self.responses = [
                {"boundary_ids": ["SCattribute"]},
                {"boundary_ids": []},
            ]

        def call(self, prompt, *, stage="其他"):
            self.requests.append((stage, prompt))
            return self.responses.pop(0)

        def log(self, message):
            pass

    engine = Engine()
    install_solution_retry(engine)
    def validate(response):
        require_refs(response.get("boundary_ids"), set(), "boundary_ids")

    result = Classifier(engine).ask("已有解法核查", "核查\n", _payload(), validate)
    assert result == {"boundary_ids": []}
    assert len(engine.requests) == 2
    first = json.loads(engine.requests[0][1].rsplit("\n", 1)[1])
    retry = json.loads(engine.requests[1][1].rsplit("\n", 1)[1])
    assert '"boundary_ids":[]' in first["validation_feedback"]
    assert "boundary_ids引用越界/重复" in retry["validation_feedback"]

    # Same original prompt and payload must hit the success cache without a call.
    later = Engine()
    install_solution_retry(later)
    assert Classifier(later).ask("已有解法核查", "核查\n", _payload(), validate) == result
    assert later.requests == []


def _strict_solution_validate(response):
    require_refs(response.get("support_ids"), {"Epositive"}, "support_ids")
    require_refs(response.get("counter_ids"), {"Enegative"}, "counter_ids")
    require_refs(response.get("boundary_ids"), {"Euncertain"}, "boundary_ids")
    require_refs(response.get("cell_ids"), {"SCattribute"}, "cell_ids")
    if response.get("status") == "部分解决" and not response["support_ids"]:
        raise ValueError("部分解决须有本变体同Need正向体验")


def _conditional_payload():
    payload = _payload()
    payload["evidence"][0]["conditions"] = ["需忍受异味"]
    payload["evidence"].append({"evidence_id": "Euncertain", "polarity": "uncertain"})
    return payload


def _boundary_response():
    return {"status": "待验证", "support_ids": [], "counter_ids": [],
            "boundary_ids": ["Epositive", "Euncertain"], "cell_ids": ["SCattribute"]}


def test_positive_review_condition_can_be_cited_without_becoming_support():
    payload, response = _conditional_payload(), _boundary_response()
    validate_conditional_boundaries(payload, response, _strict_solution_validate)
    assert response["boundary_ids"] == ["Epositive", "Euncertain"]
    assert response["support_ids"] == []
    request = solution_request("核查\n" + json.dumps(payload), "已有解法核查")
    feedback = json.loads(request.rsplit("\n", 1)[1])["validation_feedback"]
    assert '"boundary_ids":["Epositive","Euncertain"]' in feedback


@pytest.mark.parametrize("boundary,status,condition", [
    (["Epositive", "Epositive"], "待验证", ["条件"]),
    (["Epositive", "Eother"], "待验证", ["条件"]),
    (["Epositive", "SCattribute"], "待验证", ["条件"]),
    (["Epositive"], "待验证", []),
    (["Epositive"], "部分解决", ["条件"]),
])
def test_conditional_boundary_does_not_weaken_other_guards(boundary, status, condition):
    payload, response = _conditional_payload(), _boundary_response()
    payload["evidence"][0]["conditions"] = condition
    response.update(boundary_ids=boundary, status=status)
    with pytest.raises(ValueError):
        validate_conditional_boundaries(payload, response, _strict_solution_validate)


def test_saved_failed_conditional_result_is_revalidated_and_reused_without_llm(tmp_path):
    from tools.voc.classification import CACHE_VERSION
    from tools.voc.opportunity import digest
    class Engine:
        directory = tmp_path
        config = {"LLM_MODEL": "test", "LLM_BASE_URL": "local"}
        calls = 0
        def call(self, prompt, *, stage="其他"):
            self.calls += 1
            raise AssertionError("a valid saved failure should not call the model")
        def log(self, message):
            pass
    engine = Engine()
    payload, response = _conditional_payload(), _boundary_response()
    prompt, task = "核查\n", "已有解法核查"
    key = digest([CACHE_VERSION, prompt, "test", "local", payload])
    failed = tmp_path / (task + "-失败-" + key + ".json")
    failed.write_text(json.dumps(response), encoding="utf-8")
    install_solution_retry(engine)
    result = Classifier(engine).ask(task, prompt, payload, _strict_solution_validate)
    assert result == response and engine.calls == 0
    assert json.loads((tmp_path / (task + "-" + key + ".json")).read_text(encoding="utf-8")) == response
    assert failed.exists()
