"""Late timeout failures must not silently consume the only real retry."""
from threading import Event
import time
from types import SimpleNamespace

import pytest

from core.errors import LLMError
from tests.test_voc_opportunity import config
from tools.voc.tasks import BatchQueue, DeadlineCalls, TaskDeferred


def wait_done(deadlines, key):
    deadline = time.monotonic() + 1
    while not deadlines.active[key]["done"].is_set() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert deadlines.active[key]["done"].is_set()


def test_completed_late_llm_timeout_really_sends_retry():
    calls, release = [], Event()
    deadlines = DeadlineCalls(0.02, 2)

    def request():
        calls.append(True)
        if len(calls) == 1:
            assert release.wait(1)
            raise LLMError("LLM 请求超时(>120s, 服务端无响应/挂起)")
        return {"result": "recovered"}

    try:
        with pytest.raises(TaskDeferred):
            deadlines.call("batch", request)
        release.set()
        wait_done(deadlines, "batch")
        assert deadlines.call("batch", request) == {"result": "recovered"}
        assert len(calls) == 2 and not deadlines.active
    finally:
        release.set()


def test_still_running_request_is_not_resent_and_late_success_is_reused():
    calls, release = [], Event()
    deadlines = DeadlineCalls(0.02, 2)

    def request():
        calls.append(True)
        assert release.wait(1)
        return {"result": "late success"}

    try:
        with pytest.raises(TaskDeferred):
            deadlines.call("batch", request)
        with pytest.raises(TaskDeferred, match="同一请求仍在执行"):
            deadlines.call("batch", request)
        assert len(calls) == 1
        release.set()
        wait_done(deadlines, "batch")
        assert deadlines.call("batch", request) == {"result": "late success"}
        assert len(calls) == 1
    finally:
        release.set()


@pytest.mark.parametrize("error", [LLMError("invalid API key"), RuntimeError("insufficient points")])
def test_late_non_timeout_errors_are_consumed_without_automatic_new_request(error):
    calls, release = [], Event()
    deadlines = DeadlineCalls(0.02, 2)

    def request():
        calls.append(True)
        assert release.wait(1)
        raise error

    try:
        with pytest.raises(TaskDeferred):
            deadlines.call("batch", request)
        release.set()
        wait_done(deadlines, "batch")
        with pytest.raises(type(error), match=str(error)):
            deadlines.call("batch", request)
        assert len(calls) == 1 and not deadlines.active
    finally:
        release.set()


@pytest.mark.parametrize("recover", [True, False])
def test_one_queue_retry_round_really_reissues_completed_timeout_and_is_bounded(tmp_path, recover):
    deadlines, calls, release = DeadlineCalls(0.02, 2), [], Event()
    engine = SimpleNamespace(config=config(), directory=tmp_path, log=lambda _: None)
    queue = BatchQueue(engine, "timeout-retry", [["batch"]], split=False)

    def request():
        calls.append(True)
        if len(calls) == 1:
            assert release.wait(1)
        if len(calls) == 1 or not recover:
            raise LLMError("LLM 请求超时(>120s, 服务端无响应/挂起)")
        return {"result": "recovered"}

    try:
        while queue:
            batch = queue.pop()
            try:
                result = deadlines.call(batch[0], request)
            except TaskDeferred as exc:
                release.set()
                wait_done(deadlines, batch[0])
                queue.fail(batch, exc)
            except LLMError as exc:
                queue.fail(batch, exc)
            else:
                assert result == {"result": "recovered"}
                queue.record(batch, "complete")
        assert len(calls) == 2
        if recover:
            queue.finish()
            assert [e["status"] for e in queue.events] == ["deferred", "complete"]
        else:
            assert len(queue.pending) == 1
            with pytest.raises(RuntimeError, match="待重试"):
                queue.finish()
    finally:
        release.set()
