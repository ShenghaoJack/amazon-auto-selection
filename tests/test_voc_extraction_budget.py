"""Risk controls: bounded requests, fault recovery, source coverage and resumption.

Model doubles exercise failure handling; these tests do not measure LLM accuracy.
"""
import json

import pytest

from core.errors import LLMError, LLMTruncatedError
from core.llm import LLMClient, MAX_TOKENS_CAP
from tests.test_voc_opportunity import FakeLLM, aspect, config, row
from tools.voc.extraction_budget import ExtractionBudget, covers_source, estimate_tokens
from tools.voc.opportunity import Analyzer, EXTRACT, prepare_reviews, windows


class Recorder(FakeLLM):
    def __init__(self):
        super().__init__()
        self.requests = []

    def chat_json(self, prompt, **kwargs):
        self.requests.append((prompt, kwargs))
        return super().chat_json(prompt, **kwargs)


def run(tmp_path, rows, llm=None, **settings):
    cfg = config()
    for key, value in settings.items():
        cfg.set(key, value)
    llm = llm or Recorder()
    engine = Analyzer(cfg, tmp_path, "US", llm=llm, log=lambda _: None)
    return engine, llm, engine.extract(rows)


@pytest.mark.parametrize("text", ["English " * 2500, "日本語和中文🙂" * 2500, "عربي " * 5000],
                         ids=["english", "cjk_emoji", "arabic"])
def test_first_oversized_multilingual_part_is_budgeted_and_source_is_intact(tmp_path, text):
    _, llm, result = run(tmp_path, [row(body=text)])
    assert result["reviews"][0]["text"] == text
    assert result["coverage"][0]["status"] == "reviewed"
    assert result["coverage"][0]["source_char_coverage"]
    assert result["coverage"][0]["recall_check_parts"] > 0
    for prompt, kwargs in llm.requests:
        assert estimate_tokens(prompt) <= 24000
        assert kwargs["max_tokens"] == kwargs["output_token_cap"] == 16000
    assert len(llm.requests) > 1


def test_title_only_windows_keep_exact_source_offsets(tmp_path):
    class TitleLLM(Recorder):
        def chat_json(self, prompt, **kwargs):
            self.requests.append((prompt, kwargs))
            parts = json.loads(prompt.split("\n")[-1])
            return {"parts": [{"part_id": p["part_id"], "reason": "已读", "aspects":
                               [dict(aspect(p["title"][-10:]), quote_field="title")] if p["title"] else []}
                              for p in parts]}
    title = "更新：用了以后很好🙂" * 2000 + "最后的结论。"
    _, llm, result = run(tmp_path, [row(body="", review_title=title)], llm=TitleLLM())
    assert result["coverage"][0]["status"] == "reviewed"
    assert any(a["end"] == len(title) for a in result["aspects"])
    assert all(title[a["start"]:a["end"]] == a["quote"] for a in result["aspects"])
    assert all(estimate_tokens(prompt) <= 24000 for prompt, _ in llm.requests)


@pytest.mark.parametrize("fault", ["truncation", "missing", "invalid_quote"])
def test_bad_batch_is_split_until_every_review_is_validated(tmp_path, fault):
    class SmallOnly(Recorder):
        def chat_json(self, prompt, **kwargs):
            parts = json.loads(prompt.split("\n")[-1])
            result = super().chat_json(prompt, **kwargs)
            if len(parts) > 1:
                if fault == "truncation":
                    raise LLMTruncatedError("output truncated")
                if fault == "missing":
                    result["parts"].pop()
                else:
                    result["parts"][0]["aspects"][0]["quote"] = "invented"
            return result
    _, llm, result = run(tmp_path, [row(str(i)) for i in range(3)], llm=SmallOnly())
    assert all(c["status"] == "reviewed" for c in result["coverage"])
    assert not result["errors"]
    audit = json.loads((tmp_path / "提取请求审计.json").read_text(encoding="utf-8"))
    assert any(e.get("action") == "split_batch" for e in audit["events"])
    assert all(len({p["variant_asin"] for p in json.loads(prompt.split("\n")[-1])}) == 1
               for prompt, _ in llm.requests)


def test_single_window_recovers_with_overlap_and_reuses_successful_children(tmp_path):
    class SmallOnly(Recorder):
        block_tail = True

        def chat_json(self, prompt, **kwargs):
            parts = json.loads(prompt.split("\n")[-1])
            self.requests.append((prompt, kwargs))
            if len(parts[0]["text"]) > 1300:
                raise LLMTruncatedError("too long")
            if self.block_tail and parts[0]["offset"] >= 1000:
                raise LLMError("connection lost")
            return FakeLLM.chat_json(self, prompt, **kwargs)
    engine, llm, first = run(tmp_path, [row(body="abcdefgh🙂" * 450)], llm=SmallOnly())
    assert first["coverage"][0]["status"] == "pending"
    successful = {key for key, value in engine.cache.items() if value["aspects"]}
    assert successful
    valid_before = {a["evidence_id"] for key in successful for a in engine.cache[key]["aspects"]}
    llm.block_tail = False
    before = len(llm.requests)
    second = engine.extract([row(body="abcdefgh🙂" * 450)])
    assert second["coverage"][0]["status"] == "reviewed"
    assert valid_before <= {a["evidence_id"] for a in second["aspects"]}
    # Previously successful slices are not sent again while retrying failed siblings.
    first_slice_ids = {json.loads(p.split("\n")[-1])[0]["part_id"] for p, _ in llm.requests[:before]
                       if len(json.loads(p.split("\n")[-1])[0]["text"]) <= 1300
                       and json.loads(p.split("\n")[-1])[0]["offset"] < 1000}
    retried_ids = {part["part_id"] for p, _ in llm.requests[before:]
                   for part in json.loads(p.split("\n")[-1])}
    assert first_slice_ids.isdisjoint(retried_ids)


def test_permanent_truncation_is_bounded_and_cannot_complete(tmp_path):
    class Broken(Recorder):
        def chat_json(self, prompt, **kwargs):
            self.requests.append((prompt, kwargs))
            raise LLMTruncatedError("output truncated")
    engine, llm, result = run(tmp_path, [row(body="a" * 4000)], llm=Broken(), VOC_EXTRACT_SPLIT_DEPTH=1, VOC_RETRY_ROUNDS=0)
    assert result["coverage"][0]["status"] == "pending"
    assert len(llm.requests) == 3  # one parent plus two children, then stop
    assert not engine.cache
    with pytest.raises(RuntimeError, match="未全覆盖"):
        engine.finalize([row(body="a" * 4000)])
    assert not (tmp_path / "chain.json").exists()


def test_connection_failure_does_not_fan_out(tmp_path):
    class Broken(Recorder):
        def chat_json(self, prompt, **kwargs):
            self.requests.append((prompt, kwargs))
            raise LLMError("connection lost")
    _, llm, result = run(tmp_path, [row(str(i)) for i in range(3)], llm=Broken(), VOC_RETRY_ROUNDS=0)
    assert len(llm.requests) == 1
    assert all(c["status"] == "pending" for c in result["coverage"])


def test_recall_failure_cannot_be_hidden_by_successful_primary_pass(tmp_path):
    class RecallFails(Recorder):
        def chat_json(self, prompt, **kwargs):
            parts = json.loads(prompt.split("\n")[-1])
            if parts[0]["pass"] == "recall_check":
                raise LLMError("connection lost")
            return super().chat_json(prompt, **kwargs)
    _, _, result = run(tmp_path, [row(body="a" * 11000)], llm=RecallFails())
    assert result["coverage"][0]["source_char_coverage"]
    assert result["coverage"][0]["status"] == "pending"
    assert result["aspects"] == []


def test_cache_write_failure_does_not_become_success_or_repeat_model_call(tmp_path, monkeypatch):
    import tools.voc.opportunity as module
    original_save = module.save
    def broken_save(path, data):
        if path.name == "方面缓存.json":
            raise OSError("disk write failed")
        return original_save(path, data)
    monkeypatch.setattr(module, "save", broken_save)
    llm = Recorder()
    engine = Analyzer(config(), tmp_path, "US", llm=llm, log=lambda _: None)
    with pytest.raises(OSError, match="disk write failed"):
        engine.finalize([row()])
    assert len(llm.requests) == 1
    assert not engine.cache
    summary = json.loads((tmp_path / "需求机会链摘要.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"


def test_independent_recall_can_add_primary_omission_and_retains_update_context(tmp_path):
    marker = "middle experience"
    body = "First impression. " + "a" * 11500 + marker + "b" * 11500 + "Update: this conclusion changed."
    class RecallLLM(Recorder):
        def chat_json(self, prompt, **kwargs):
            parts = json.loads(prompt.split("\n")[-1])
            self.requests.append((prompt, kwargs))
            return {"parts": [{"part_id": p["part_id"], "reason": "已读", "aspects":
                               [aspect(marker)] if p["pass"] == "recall_check" and marker in p["text"] else []}
                              for p in parts]}
    _, llm, result = run(tmp_path, [row(body=body)], llm=RecallLLM())
    assert result["coverage"][0]["status"] == "reviewed"
    assert any(a["quote"] == marker for a in result["aspects"])
    for prompt, _ in llm.requests:
        parts = json.loads(prompt.split("\n")[-1])
        assert len({p["pass"] for p in parts}) == 1
        assert all("Update:" in p["context_only"]["text"]["tail"] for p in parts)


def test_budget_change_invalidates_cache_and_cache_hit_resets_request_audit(tmp_path):
    engine, llm, _ = run(tmp_path, [row()])
    calls = len(llm.requests)
    engine.extract([row()])
    assert len(llm.requests) == calls
    assert not json.loads((tmp_path / "提取请求审计.json").read_text(encoding="utf-8"))["events"]
    engine.config.set("VOC_EXTRACT_OUTPUT_TOKENS", 8000)
    engine.extract([row()])
    assert len(llm.requests) == calls + 1


def test_feedback_and_prompt_are_inside_budget_and_source_gaps_are_detected():
    budget = ExtractionBudget()
    review = prepare_reviews([row(body="界🙂" * 6000)], "US")[0]
    parts = budget.fit_parts(EXTRACT, windows(review))
    assert covers_source(review, parts)
    assert not covers_source(review, parts[:-1])
    assert all(estimate_tokens(budget.prompt(EXTRACT, [p], "界🙂" * 10000)) <= budget.input_tokens for p in parts)


def test_small_model_context_must_fit_input_and_output_reserves():
    cfg = config()
    cfg.set("VOC_EXTRACT_CONTEXT_TOKENS", 32000)
    with pytest.raises(ValueError, match="预算无效"):
        ExtractionBudget.from_config(cfg)


def test_llm_explicit_output_cap_returns_truncation_without_global_escalation(monkeypatch):
    client = LLMClient(config())
    requests = []
    def truncated(*args, **kwargs):
        requests.append(args[5])
        return '{"parts":[]}', "length"
    monkeypatch.setattr(client, "_request", truncated)
    monkeypatch.setattr(client, "_cool_down", lambda _: None)
    with pytest.raises(LLMTruncatedError):
        client.chat_json("test", max_tokens=16000, output_token_cap=16000)
    assert requests == [16000]
    requests.clear()
    with pytest.raises(LLMTruncatedError):
        client.chat_json("test", max_tokens=16000)
    assert requests == [16000, 32000, MAX_TOKENS_CAP]
