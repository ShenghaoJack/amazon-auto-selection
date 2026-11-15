"""Windows publication locks must not discard or rebill valid model responses."""
import json
import os
import sys
from threading import Event, Thread, Timer

import pytest

from core.config import Config
from tools.voc import storage
from tools.voc.classification import CACHE_VERSION, Classifier
from tools.voc.opportunity import Analyzer, digest, save


def test_temporary_publication_denial_retries_only_rename(tmp_path, monkeypatch):
    target = tmp_path / "queue.json"
    target.write_text('{"old":true}', encoding="utf-8")
    actual, calls, sleeps = os.replace, [], []

    def replace(source, destination):
        calls.append(source)
        if len(calls) < 3:
            assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
            error = PermissionError("temporary reader lock")
            error.winerror = 5
            raise error
        return actual(source, destination)

    monkeypatch.setattr(storage.os, "replace", replace)
    real_atomic = storage.atomic_replace
    monkeypatch.setattr(storage, "atomic_replace", lambda src, dst: real_atomic(src, dst, sleep=sleeps.append))
    save(target, {"new": True})
    assert len(calls) == 3 and len(set(calls)) == 1 and sleeps == [0.05, 0.1]
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert not list(tmp_path.glob("*.pending"))


def test_permanent_denial_preserves_old_file_and_full_pending_response(tmp_path, monkeypatch):
    target = tmp_path / "response.json"
    target.write_text('{"old":true}', encoding="utf-8")
    attempts = []

    def replace(source, destination):
        attempts.append(True)
        raise PermissionError("locked")

    monkeypatch.setattr(storage.os, "replace", replace)
    real_atomic = storage.atomic_replace
    monkeypatch.setattr(storage, "atomic_replace", lambda src, dst: real_atomic(src, dst, sleep=lambda _: None))
    with pytest.raises(PermissionError):
        save(target, {"paid_response": [1, 2, 3]})
    assert len(attempts) == 9
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    pending = list(storage.pending_json(target))
    assert len(pending) == 1 and pending[0][1] == {"paid_response": [1, 2, 3]}


def test_other_io_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []

    def replace(*args):
        calls.append(True)
        raise OSError(28, "disk full")

    monkeypatch.setattr(storage.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        save(tmp_path / "file.json", {"valid": True})
    assert len(calls) == 1


class Model:
    calls = 0

    def chat_json(self, *args, **kwargs):
        self.calls += 1
        return {"items": [{"id": "A1", "value": "valid"}]}


def classifier(tmp_path, model):
    return Classifier(Analyzer(Config({"LLM_MODEL": "fake"}), tmp_path, "US", llm=model, log=lambda _: None))


def validate(response):
    if response != {"items": [{"id": "A1", "value": "valid"}]}:
        raise ValueError("bad response")


def test_valid_cache_hit_is_read_only_and_does_not_touch_locked_destination(tmp_path, monkeypatch):
    import tools.voc.classification as module
    model = Model()
    c = classifier(tmp_path, model)
    expected = c.ask("方面分类", "prompt", {"x": 1}, validate)

    def fail(*args):
        pytest.fail("valid cached response must not be rewritten")

    monkeypatch.setattr(module, "save", fail)
    assert c.ask("方面分类", "prompt", {"x": 1}, validate) == expected
    assert model.calls == 1


def test_unpublished_valid_response_is_recovered_without_new_model_call(tmp_path, monkeypatch):
    model = Model()
    c = classifier(tmp_path, model)
    original = storage.atomic_replace
    monkeypatch.setattr(storage, "atomic_replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        c.ask("方面分类", "prompt", {"x": 1}, validate)
    assert model.calls == 1 and len(list(tmp_path.glob("*.pending"))) == 1
    monkeypatch.setattr(storage, "atomic_replace", original)
    result = classifier(tmp_path, model).ask("方面分类", "prompt", {"x": 1}, validate)
    validate(result)
    assert model.calls == 1


def test_pending_invalid_or_partial_json_is_never_accepted_as_cache(tmp_path):
    model = Model()
    c = classifier(tmp_path, model)
    payload = {"x": 1}
    key = digest([CACHE_VERSION, "prompt", "fake", c.engine.config.get("LLM_BASE_URL"), payload])
    target = tmp_path / ("方面分类-" + key + ".json")
    target.with_name(target.name + ".partial.pending").write_text('{"items":', encoding="utf-8")
    target.with_name(target.name + ".invalid.pending").write_text('{"items":[]}', encoding="utf-8")
    validate(c.ask("方面分类", "prompt", payload, validate))
    assert model.calls == 1


def test_valid_pending_response_recovers_after_invalid_canonical_cache(tmp_path):
    model = Model()
    c = classifier(tmp_path, model)
    payload = {"x": 1}
    key = digest([CACHE_VERSION, "prompt", "fake", c.engine.config.get("LLM_BASE_URL"), payload])
    target = tmp_path / ("方面分类-" + key + ".json")
    target.write_text('{"items":[]}', encoding="utf-8")
    target.with_name(target.name + ".valid.pending").write_text(
        '{"items":[{"id":"A1","value":"valid"}]}', encoding="utf-8")
    validate(c.ask("方面分类", "prompt", payload, validate))
    assert model.calls == 0


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows sharing semantics")
def test_real_windows_reader_without_delete_share_can_close_and_publication_resumes(tmp_path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    target = tmp_path / "real-lock.json"
    target.write_text('{"old":true}', encoding="utf-8")
    handle = create(str(target), 0x80000000, 3, None, 3, 128, None)
    assert handle != ctypes.c_void_p(-1).value
    release = Event()

    def unlock():
        release.wait(1)
        close(handle)

    thread = Thread(target=unlock)
    thread.start()
    timer = Timer(0.12, release.set)
    timer.start()
    try:
        save(target, {"new": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    finally:
        release.set()
        timer.cancel()
        thread.join(1)
