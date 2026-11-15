import json
import threading

import pytest

from tools.attr_stability.spec_io import spec_lock, spec_revision, write_json_atomic
from tools.attr_stability.spec_sync import SpecConcurrentChange, sync_typical_values, strip_all_contradictions


def test_sync_waits_for_other_writer_and_rejects_stale_revision(tmp_path):
    path = tmp_path / "spec.json"
    old = {"attributes": [{"name": "材质", "hint": "典型取值 木 / 金属"}]}
    write_json_atomic(path, old)
    started = threading.Event()
    completed = threading.Event()
    errors = []

    def sync():
        started.set()
        try:
            sync_typical_values(path, {"材质": ["塑料"]}, expected_revision=spec_revision(old))
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    with spec_lock(tmp_path):
        worker = threading.Thread(target=sync)
        worker.start()
        assert started.wait(2)
        assert not completed.wait(.1)
        updated = {"attributes": [{"name": "材质", "hint": "人工修改的判定规则"}]}
        write_json_atomic(path, updated)
    worker.join(5)
    assert completed.is_set()
    assert len(errors) == 1 and isinstance(errors[0], SpecConcurrentChange)
    assert json.loads(path.read_text(encoding="utf-8")) == updated


def test_strip_and_sync_reject_outdated_spec_without_writing(tmp_path):
    path = tmp_path / "spec.json"
    spec = {"attributes": [{"name": "材质", "hint": "典型取值 木 / 金属"}]}
    write_json_atomic(path, spec)
    for operation in (lambda: strip_all_contradictions(path, expected_revision="old"),
                      lambda: sync_typical_values(path, {"材质": ["塑料"]}, expected_revision="old")):
        with pytest.raises(SpecConcurrentChange):
            operation()
        assert json.loads(path.read_text(encoding="utf-8")) == spec
