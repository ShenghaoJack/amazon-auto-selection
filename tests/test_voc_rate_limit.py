"""Rate limiting is transient; genuine quota exhaustion still switches sources."""
from threading import Event
import time

import pytest

from core.mcp import MCPBusinessError
from tests.test_voc_asin_fallback import envelope, item, recovery, rows, seed
from tests.test_voc_mcp_recovery import collector
from tests.test_voc_opportunity import config
from tools.voc.acquire import looks_exhausted, looks_minute_limited, looks_rate_limited
from tools.voc.rate_limit import SourceThrottle
from tools.voc.tasks import TaskDeferred


MESSAGE = "执行失败: [RATE_LIMIT] Rate limit exceeded, please reduce QPS and retry"
MINUTE_CODE = "ERROR_MAXIMUM_ACCESS_PER_MINUTE"
MINUTE_MESSAGE = "每分钟访问已达上限"


class Clock:
    now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("message,code", [(MESSAGE, "isError"), ("Too many requests", 429),
                                         ("请求频率过高，请稍后重试", "RATE_LIMIT")])
def test_rate_limit_is_not_quota_exhaustion(message, code):
    assert looks_rate_limited(message, code)
    assert not looks_exhausted(message, code)


@pytest.mark.parametrize("message", ["insufficient points", "credits exhausted", "balance insufficient",
                                    "quota exceeded", "积分不足，请充值", "余额已用尽", "额度耗尽"])
def test_actual_quota_failures_still_count(message):
    assert looks_exhausted(message)


@pytest.mark.parametrize("message", ["timeout exceeded", "insufficient permissions", "invalid asin",
                                    "pointCost=5", "credit check failed", "Missing data, balance not returned"])
def test_unrelated_keywords_do_not_disable_sources(message):
    assert not looks_exhausted(message)


def test_source_cooldown_and_spacing_are_shared_but_other_source_is_independent():
    clock = Clock()
    throttle = SourceThrottle(config(), clock=clock.time, sleep=clock.sleep)
    assert throttle.limited("pangolinfo") == 5
    throttle.wait("sellersprite")
    assert clock.now == 0
    throttle.wait("pangolinfo")
    assert clock.now == 5
    throttle.wait("pangolinfo")
    assert clock.now == 7
    assert throttle.limited("pangolinfo") == 10
    throttle.wait("pangolinfo")
    assert clock.now == 17


@pytest.mark.parametrize("as_envelope", [False, True])
def test_alternate_rate_limit_recovers_without_disabling_source(tmp_path, monkeypatch, as_envelope):
    import tools.voc.rate_limit as module
    clock = Clock()
    monkeypatch.setattr(module, "SourceThrottle", lambda cfg: SourceThrottle(cfg, clock=clock.time, sleep=clock.sleep))
    calls = []

    def call(source, args):
        calls.append(source)
        if source == "pangolinfo" and calls.count(source) == 1:
            if as_envelope:
                return {"code": "RATE_LIMIT", "message": MESSAGE}
            raise MCPBusinessError("get_amazon_reviews", "isError", MESSAGE)
        return envelope(source, [item("R1", "V" if source == "pangolinfo" else None)])

    c = collector(tmp_path / "raw", monkeypatch, call)
    seed(c, "sellersprite")
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    assert calls == ["sellersprite", "pangolinfo", "pangolinfo"]
    assert not getattr(c, "exhausted_sources", set()) and not c.sources_dead
    assert r.annotate(rows(c))["pending_tasks"] == 0
    assert clock.now >= 5
    r2 = recovery(collector(tmp_path / "raw", monkeypatch, call), tmp_path / "out")
    r2.run(rows(c))
    assert len(calls) == 3


def test_persistent_rate_limit_is_bounded_and_does_not_stop_source(tmp_path, monkeypatch):
    import tools.voc.rate_limit as module
    clock = Clock()
    monkeypatch.setattr(module, "SourceThrottle", lambda cfg: SourceThrottle(cfg, clock=clock.time, sleep=clock.sleep))
    calls = []

    def call(source, args):
        calls.append(source)
        if source == "pangolinfo":
            raise MCPBusinessError("get_amazon_reviews", "isError", MESSAGE)
        return envelope(source, [item("R1", "V")])

    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 1, "critical:1")
    assert calls == ["pangolinfo"] * 3 + ["sellersprite"]
    assert c.sources[0] == "pangolinfo"
    assert not getattr(c, "exhausted_sources", set()) and not c.sources_dead
    assert not c._switch_source(MCPBusinessError("review", "isError", MESSAGE), "pangolinfo")


def test_quota_failure_is_not_retried_by_rate_throttle():
    clock, calls = Clock(), []
    throttle = SourceThrottle(config(), clock=clock.time, sleep=clock.sleep)

    def call():
        calls.append(True)
        raise MCPBusinessError("review", 402, "insufficient points")

    with pytest.raises(MCPBusinessError):
        throttle.call("pangolinfo", call, lambda _: None)
    assert len(calls) == 1 and clock.now == 0


@pytest.mark.parametrize("message,code", [(MINUTE_MESSAGE, MINUTE_CODE), ("", MINUTE_CODE),
                                         (MINUTE_MESSAGE, "isError"), ("maximum access per minute exceeded", 429)])
def test_per_minute_limit_is_temporary_and_recognized_by_code_or_message(message, code):
    assert looks_minute_limited(message, code)
    assert looks_rate_limited(message, code)
    assert not looks_exhausted(message, code)


@pytest.mark.parametrize("source,other", [("sellersprite", "pangolinfo"), ("pangolinfo", "sellersprite")])
def test_minute_cooldown_coalesces_parallel_errors_and_spaces_recovery(source, other):
    clock = Clock()
    throttle = SourceThrottle(config(), clock=clock.time, sleep=clock.sleep)
    for _ in range(4):
        assert throttle.limited(source, minute=True) == 65
    assert throttle.sources[source]["interval"] == 5
    throttle.wait(other)
    assert clock.now == 0
    throttle.wait(source)
    assert clock.now == 65
    throttle.wait(source)
    assert clock.now == 70
    throttle.limited(source, minute=True)
    assert throttle.sources[source]["interval"] == 10
    throttle.wait(source)
    assert clock.now == 135


def test_late_success_does_not_clear_other_workers_minute_cooldown():
    clock = Clock()
    throttle = SourceThrottle(config(), clock=clock.time, sleep=clock.sleep)

    def late_success():
        # Another worker gets limited while this already-started call succeeds.
        throttle.limited("sellersprite", minute=True)
        return {"code": "OK", "data": {"items": []}}

    throttle.call("sellersprite", late_success, lambda _: None)
    throttle.wait("sellersprite")
    assert clock.now == 65


def test_minute_cooldown_has_full_window_even_with_short_qps_configuration():
    cfg, clock = config(), Clock()
    cfg.set("VOC_MCP_MINUTE_COOLDOWN_SECONDS", 1)
    cfg.set("VOC_MCP_RATE_BACKOFF_SECONDS", 0.1)
    throttle = SourceThrottle(cfg, clock=clock.time, sleep=clock.sleep)
    assert throttle.limited("sellersprite", minute=True) == 65


@pytest.mark.parametrize("as_envelope", [False, True])
@pytest.mark.parametrize("source", ["pangolinfo", "sellersprite"])
def test_source_minute_limit_retries_after_cooldown_and_preserves_cache(tmp_path, monkeypatch, as_envelope, source):
    import tools.voc.rate_limit as module
    clock, calls, logs = Clock(), [], []
    monkeypatch.setattr(module, "SourceThrottle", lambda cfg: SourceThrottle(cfg, clock=clock.time, sleep=clock.sleep))

    def call(request_source, args):
        calls.append((request_source, clock.now))
        if len(calls) == 1:
            if as_envelope:
                return {"code": MINUTE_CODE, "message": MINUTE_MESSAGE}
            raise MCPBusinessError("review", MINUTE_CODE, MINUTE_MESSAGE)
        return envelope(request_source, [item("R1", "V")])

    c = collector(tmp_path, monkeypatch, call)
    c.log = logs.append
    c._collect_call(source, "P", "P", "critical", "critical", [1, 2, 3], 1)
    assert calls == [(source, 0), (source, 65)]
    assert not getattr(c, "exhausted_sources", set()) and not c.sources_dead
    assert any("每分钟访问已达上限" in log and "同源统一冷却65秒" in log for log in logs)
    resumed = collector(tmp_path, monkeypatch, call)
    resumed._collect_call(source, "P", "P", "critical", "critical", [1, 2, 3], 1)
    assert len(calls) == 2


def test_both_sources_cool_down_independently_when_both_are_limited():
    clock = Clock()
    throttle = SourceThrottle(config(), clock=clock.time, sleep=clock.sleep)
    throttle.limited("pangolinfo", minute=True)
    clock.now = 10
    throttle.limited("sellersprite", minute=True)
    throttle.wait("pangolinfo")
    assert clock.now == 65
    assert throttle.sources["sellersprite"]["next"] == 75
    throttle.wait("sellersprite")
    assert clock.now == 75


def test_persistent_minute_limit_is_bounded_and_pending_then_resumes(tmp_path, monkeypatch):
    import tools.voc.rate_limit as module
    clock, calls, broken = Clock(), [], {"yes"}
    monkeypatch.setattr(module, "SourceThrottle", lambda cfg: SourceThrottle(cfg, clock=clock.time, sleep=clock.sleep))

    def call(source, args):
        calls.append((source, clock.now))
        if source == "pangolinfo" and broken:
            raise MCPBusinessError("get_amazon_reviews", MINUTE_CODE, MINUTE_MESSAGE)
        return envelope(source, [item("R1", "V" if source == "pangolinfo" else None)])

    c = collector(tmp_path / "raw", monkeypatch, call)
    c.config.set("VOC_RETRY_ROUNDS", 0)
    seed(c, "sellersprite")
    r = recovery(c, tmp_path / "out")
    r.run(rows(c))
    assert r.annotate(rows(c))["pending_tasks"] == 1
    assert [s for s, _ in calls] == ["sellersprite"] + ["pangolinfo"] * 3
    assert [t for s, t in calls if s == "pangolinfo"] == [0, 65, 130]
    assert not getattr(c, "exhausted_sources", set())
    broken.clear()
    resumed = collector(tmp_path / "raw", monkeypatch, call)
    r2 = recovery(resumed, tmp_path / "out")
    r2.run(rows(resumed))
    assert r2.annotate(rows(resumed))["pending_tasks"] == 0
    assert [s for s, _ in calls].count("sellersprite") == 1


def test_deadline_during_minute_cooldown_keeps_one_request_and_other_source_runs(tmp_path, monkeypatch):
    import tools.voc.rate_limit as module
    clock, release, waiting, calls = Clock(), Event(), Event(), []

    def sleep(seconds):
        waiting.set()
        assert release.wait(2)
        clock.sleep(seconds)

    monkeypatch.setattr(module, "SourceThrottle", lambda cfg: SourceThrottle(cfg, clock=clock.time, sleep=sleep))

    def call(source, args):
        calls.append(source)
        if source == "sellersprite" and calls.count(source) == 1:
            raise MCPBusinessError("review", MINUTE_CODE, MINUTE_MESSAGE)
        return envelope(source, [item("R1", "V")])

    c = collector(tmp_path, monkeypatch, call)
    c.config.set("VOC_MCP_TIMEOUT", 0.02)
    try:
        with pytest.raises(TaskDeferred):
            c._tool(c.ss, "sellersprite", "review", {"asin": "P"})
        assert waiting.is_set()
        with pytest.raises(TaskDeferred, match="同一请求仍在执行"):
            c._tool(c.ss, "sellersprite", "review", {"asin": "P"})
        assert calls == ["sellersprite"]
        c._tool(c.pango, "pangolinfo", "get_amazon_reviews", {"asin": "P"})
        assert calls == ["sellersprite", "pangolinfo"]
        release.set()
        deadline = time.monotonic() + 1
        while any(not box["done"].is_set() for box in c.deadlines.active.values()) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert all(box["done"].is_set() for box in c.deadlines.active.values())
        c._tool(c.ss, "sellersprite", "review", {"asin": "P"})  # consume saved late reply
        assert calls == ["sellersprite", "pangolinfo", "sellersprite"]
    finally:
        release.set()
