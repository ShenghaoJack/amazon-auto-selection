"""Exercise actual concurrent calls, durable checkpoints and retry ordering."""
import copy
import json
import threading
import time
from collections import Counter

import pytest

from core.errors import LLMError
from tests.test_voc_classification import TaxonomyLLM, evidence
from tests.test_voc_mcp_recovery import collector, envelope
from tests.test_voc_opportunity import FakeLLM, config, row
from tools.voc.classification import Classifier
from tools.voc.opportunity import Analyzer
from tools.voc.tasks import BatchQueue, worker_count


class ConcurrencyProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.03)

    def leave(self):
        with self.lock:
            self.active -= 1


@pytest.mark.parametrize("workers", [1, 3])
def test_parent_parallelism_and_restart_do_not_repeat_paid_calls(tmp_path, monkeypatch, workers):
    probe = ConcurrencyProbe()
    calls = []
    def call(source, args):
        probe.enter()
        try:
            calls.append((source, args["asin"], args["filterByStar"]))
            result = envelope(source)
            result["data"]["json"][0]["data"]["results"][0]["reviewId"] = args["asin"] + args["filterByStar"]
            return result
        finally:
            probe.leave()
    c = collector(tmp_path, monkeypatch, call)
    c.config.set("VOC_FETCH_WORKERS", workers)
    parents = [f"P{i}" for i in range(9)]
    c.collect_parents(parents + parents[:1], {}, 2, "critical:1,five_star:1")
    assert probe.peak == workers
    assert len(calls) == 18 and c.points == c._spent_points() == 90
    assert not c.failed_parents
    assert len(c._read_raw(c.rev_path["pangolinfo"])) == 18
    # Instantiate a new collector, repairing derived data from durable receipts.
    resumed = collector(tmp_path, monkeypatch, call)
    resumed.config.set("VOC_FETCH_WORKERS", workers)
    resumed.collect_parents(parents, {}, 2, "critical:1,five_star:1")
    assert len(calls) == 18
    assert resumed.points == 90
    assert json.loads((tmp_path / "采集补试队列.json").read_text(encoding="utf-8"))["pending_parents"] == []


def test_parent_failures_are_isolated_and_retry_only_missing_requests(tmp_path, monkeypatch):
    calls, broken = [], {"P0"}
    def call(source, args):
        calls.append((source, args["asin"]))
        time.sleep(0.01)
        if args["asin"] in broken:
            raise LLMError("network error")
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.config.set("VOC_FETCH_WORKERS", 3)
    parents = ["P0", "P1", "P2", "P3"]
    c.collect_parents(parents, {}, 1, "critical:1")
    assert set(c.failed_parents) == {"P0"}
    broken.clear()
    c.retry_failed()
    assert not c.failed_parents
    assert Counter(asin for _, asin in calls) == {"P0": 3, "P1": 1, "P2": 1, "P3": 1}
    assert c.points == c._spent_points() == 20


def test_parallel_collection_preserves_primary_then_fallback_priority(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append((args["asin"], source))
        time.sleep(0.01)
        if args["asin"] == "P0" and source == "pangolinfo":
            raise LLMError("primary timeout")
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.config.set("VOC_FETCH_WORKERS", 3)
    c.collect_parents(["P0", "P1", "P2"], {}, 1, "critical:1")
    assert [source for parent, source in calls if parent == "P0"] == ["pangolinfo", "sellersprite"]
    assert [source for parent, source in calls if parent == "P1"] == ["pangolinfo"]
    assert [source for parent, source in calls if parent == "P2"] == ["pangolinfo"]
    before = list(calls)
    c.retry_failed()
    assert calls == before  # Fallback success is a paid checkpoint too.


def test_resume_uses_index_without_reparsing_raw_for_each_parent(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append(args["asin"])
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    parents = [f"P{i}" for i in range(15)]
    c.collect_parents(parents, {}, 2, "critical:1,five_star:1")
    assert len(calls) == 30
    resumed = collector(tmp_path, monkeypatch, call)
    def forbidden(*args):
        raise AssertionError("索引完成后，断点命中不应再次读取完整raw历史")
    monkeypatch.setattr(resumed, "_read_raw", forbidden)
    resumed.collect_parents(parents, {}, 2, "critical:1,five_star:1")
    assert len(calls) == 30 and not resumed.failed_parents


def test_mcp_sessions_are_exclusively_leased_until_network_returns(tmp_path, monkeypatch):
    from tools.voc import pipeline
    probe = ConcurrencyProbe()
    active_clients, clients = set(), []
    class Client:
        def __init__(self):
            clients.append(self)
        def call_raw(self, tool, args):
            with probe.lock:
                assert id(self) not in active_clients
                active_clients.add(id(self))
            probe.enter()
            try:
                return envelope("pangolinfo")
            finally:
                probe.leave()
                with probe.lock:
                    active_clients.remove(id(self))
    monkeypatch.setattr(pipeline, "pango_client", lambda cfg: Client())
    monkeypatch.setattr(pipeline, "ss_client", lambda cfg: Client())
    c = pipeline.Collector(str(tmp_path), config(), ["pangolinfo"], log=lambda _: None)
    c.config.set("VOC_FETCH_WORKERS", 3)
    c.collect_parents([f"P{i}" for i in range(12)], {}, 1, "critical:1")
    assert probe.peak == 3
    assert len(clients) == 4  # Three leased primary sessions + one unused fallback.
    assert not active_clients and c.rows == 12


@pytest.mark.parametrize("workers", [1, 3])
def test_aspect_batches_parallel_cache_merge_and_restart(tmp_path, workers):
    probe = ConcurrencyProbe()
    class Slow(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            probe.enter()
            try:
                return super().chat_json(prompt, **kwargs)
            finally:
                probe.leave()
    cfg = config()
    cfg.set("VOC_LLM_WORKERS", workers)
    cfg.set("VOC_LLM_BATCH", 1)
    reviews = [row(f"R{i}", variant_asin="V") for i in range(12)]
    a = Analyzer(cfg, tmp_path, "US", llm=Slow(), log=lambda _: None)
    result = a.extract(reviews)
    assert probe.peak == workers and a.calls == 12
    assert len(a.cache) == 12 and len(result["aspects"]) == 24
    assert all(c["status"] == "reviewed" for c in result["coverage"])
    resumed = Analyzer(cfg, tmp_path, "US", llm=Slow(), log=lambda _: None)
    rerun = resumed.extract(reviews)
    assert resumed.calls == 0 and result["aspects"] == rerun["aspects"]
    audit = json.loads((tmp_path / "方面提取-任务队列.json").read_text(encoding="utf-8"))
    assert not audit["pending"] and not audit["in_flight"]


def test_classification_and_semantic_review_are_parallel_and_complete(tmp_path):
    probes = {stage: ConcurrencyProbe() for stage in ("voc_classify", "voc_classification_review")}
    class Slow(TaxonomyLLM):
        def chat_json(self, prompt, **kwargs):
            task = json.loads(prompt.split("\n")[-1])["task"]
            probe = probes.get(task)
            if probe:
                probe.enter()
            try:
                return super().chat_json(prompt, **kwargs)
            finally:
                if probe:
                    probe.leave()
    cfg = config()
    cfg.set("VOC_LLM_WORKERS", 3)
    llm = Slow()
    a = Analyzer(cfg, tmp_path, "US", llm=llm, log=lambda _: None)
    source = {"aspects": [evidence(f"E{i}", f"quiet {i}") for i in range(72)], "reviews": []}
    data = copy.deepcopy(source)
    Classifier(a).run(data)
    assert all(p.peak == 3 for p in probes.values())
    assert data["classification"]["classified_aspects"] == 72
    assert data["classification"]["attribute_count"] == 1
    assert all(x["semantic_review_status"] == "supported" for x in data["aspects"])
    before = a.calls
    Classifier(a).run(copy.deepcopy(source))
    assert a.calls == before


def test_parallel_queue_stable_commits_deferred_retry_and_durable_success(tmp_path):
    a = Analyzer(config(), tmp_path, "US", llm=FakeLLM(), log=lambda _: None)
    a.config.set("VOC_LLM_WORKERS", 3)
    calls, probe = [], ConcurrencyProbe()
    def work(batch):
        probe.enter()
        try:
            calls.append(batch[0])
            if batch == [0] and calls.count(0) == 1:
                raise LLMError("connection lost")
            return batch[0]
        finally:
            probe.leave()
    q = BatchQueue(a, "并行测试", [[i] for i in range(6)])
    results = [result for _, result in q.parallel(work)]
    q.finish()
    assert probe.peak == 3 and results == [1, 2, 3, 4, 5, 0]
    assert set(calls[:6]) == set(range(6)) and calls[6:] == [0]
    audit = json.loads((tmp_path / "并行测试-任务队列.json").read_text(encoding="utf-8"))
    assert not audit["pending"] and not audit["in_flight"]
    assert [x["status"] for x in audit["events"]].count("complete") == 6


def test_independent_queue_refills_slot_while_another_job_waits(tmp_path):
    a = Analyzer(config(), tmp_path, "US", llm=FakeLLM(), log=lambda _: None)
    a.config.set("VOC_LLM_WORKERS", 2)
    reached = threading.Event()
    def work(batch):
        if batch == [0]:
            assert reached.wait(1), "slow peer must not block the third independent task"
        elif batch == [2]:
            reached.set()
        return batch[0]
    q = BatchQueue(a, "连续调度", [[i] for i in range(4)])
    results = [result for _, result in q.parallel(work, ordered=False)]
    q.finish()
    assert sorted(results) == list(range(4))


def test_solution_and_open_jobs_use_model_parallelism(tmp_path):
    from tests.test_voc_solutions import make_catalog
    from tools.voc.solutions import SolutionChain
    probe = ConcurrencyProbe()
    class Client:
        def chat_json(self, prompt, **kwargs):
            probe.enter()
            try:
                data = json.loads(prompt.split("\n")[-1])
                return {"id": data["id"]}
            finally:
                probe.leave()
    cfg = config()
    cfg.set("VOC_LLM_WORKERS", 3)
    a = Analyzer(cfg, tmp_path / "out", "US", llm=Client(), catalog=make_catalog(tmp_path), log=lambda _: None)
    jobs = [{"id": i} for i in range(9)]
    def validate(job, response):
        assert response["id"] == job["id"]
    chain = SolutionChain(a)
    for stage in ("已有解法核查", "剩余Open", "Open独立复查"):
        assert len(chain.jobs(stage, jobs, "test\n", lambda j: j, validate)) == 9
    assert probe.peak == 3 and a.calls == 27
    before = a.calls
    chain.jobs("剩余Open", jobs, "test\n", lambda j: j, validate)
    assert a.calls == before


@pytest.mark.parametrize("raw, expected", [(0, 1), (-3, 1), (100, 16), (3, 3)])
def test_worker_limits(raw, expected):
    cfg = config()
    cfg.set("VOC_LLM_WORKERS", raw)
    assert worker_count(cfg) == expected
