"""Step 19's independent clients share the category meter, including worker threads."""
from core.usage import UsageMeter, load_records, summarize_category
from tests.test_voc_opportunity import config, row
from tools.voc import annotate, opportunity


def test_voc_clients_record_all_stages_and_worker_calls(tmp_path, monkeypatch):
    path = tmp_path / "llm-usage.jsonl"
    meter = UsageMeter(jsonl_path=str(path), run_id="run-voc", cat="category")
    meter.begin_step(19)

    class Client:
        def __init__(self, cfg, *, meter):
            self.meter = meter

        def chat_json(self, prompt, **kwargs):
            self.meter.record({"prompt_tokens": 100, "completion_tokens": 10},
                              channel="text", model="deepseek-flash")
            return {"issues": {"噪音": "噪音过大"}, "scenes": {"厨房": "厨房"}}

    monkeypatch.setattr(annotate, "LLMClient", Client)
    monkeypatch.setattr(opportunity, "LLMClient", Client)
    def classify(client, rows, batch_no, log=None, **kwargs):
        client.chat_json("annotation")
        return [{"i": i, "issue": "噪音", "scene": "厨房"} for i in range(len(rows))]
    monkeypatch.setattr(annotate, "classify_batch", classify)

    reviews = [row("R%d" % i, "comment %d" % i) for i in range(4)]
    labels, stats = annotate.run_annotation(reviews, config(), batch=1, workers=4,
        cache_path=str(tmp_path / "annotations.jsonl"), log=lambda _: None, meter=meter)
    assert stats["failed_batches"] == 0
    annotate.canonicalize(labels, reviews, config(), cache_path=str(tmp_path / "labels.json"),
                          log=lambda _: None, meter=meter)
    worker = opportunity.IncrementalAnalyzer(config(), tmp_path / "chain", "US",
                                              log=lambda _: None, meter=meter)
    worker.analyzer.call("aspects and classification")
    worker.close()
    meter.finish_step(19, "DONE")

    records = load_records(str(path))
    calls = [r for r in records if r.get("type") == "call"]
    assert len(calls) == 6
    assert all(r["step"] == 19 and r["cat"] == "category" for r in calls)
    totals = summarize_category(records)["steps"][19]
    assert totals["total_tokens"] == 660
    assert totals["cost_cny"] > 0


def test_missing_usage_is_visible_even_before_step_finishes(tmp_path):
    from webui.server import cat_usage
    folder = tmp_path / "monitor"
    meter = UsageMeter(jsonl_path=str(folder / "llm-usage.jsonl"), cat="category")
    meter.begin_step(19)
    meter.record(None, channel="text", model="deepseek-flash")
    usage = cat_usage(folder)
    assert usage["calls"] == 1
    assert usage["no_usage_calls"] == 1
    assert usage["steps"]["19"]["no_usage_calls"] == 1
    assert "未返回用量" in meter.step_line(19)
