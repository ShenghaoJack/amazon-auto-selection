"""Observable export lifecycle; OS lock distinguishes active work from crash residue."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path

from core.run_lock import _try_lock, _unlock
from .spec_io import write_json_atomic

BUSY_MESSAGE = "步骤18面板正在导出交付，请等待导出完成提示后再继续运行步骤18。"


def paths(root):
    store = Path(root) / '18_标准化' / '版本数据'
    return store / 'export-operation.lock', store / 'export-operation.json'


def export_status(root):
    lock_path, state_path = paths(root)
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {'status': 'idle'}
    if not lock_path.exists():
        return state if state.get('status') != 'running' else {**state, 'status': 'interrupted'}
    try:
        with lock_path.open('r+b') as fh:
            held = _try_lock(fh)
            if held is True:
                _unlock(fh)
                try:
                    state = json.loads(state_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    state = {'status': 'idle'}
            elif held is False:
                return {**state, 'status': 'running', 'message': BUSY_MESSAGE}
            else:
                return {**state, 'status': 'unknown', 'message': '无法确认导出状态，请刷新后重试。'}
    except OSError:
        return {**state, 'status': 'unknown', 'message': '无法读取导出状态，请刷新后重试。'}
    return state if state.get('status') != 'running' else {**state, 'status': 'interrupted'}


@contextmanager
def reset_guard(root):
    """Exclude exports for the entire Step 18 reset, without changing its status."""
    lock_path, _ = paths(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as fh:
        if fh.seek(0, 2) == 0:
            fh.write(b'\0')
            fh.flush()
        if _try_lock(fh) is not True:
            raise ValueError(BUSY_MESSAGE)
        try:
            yield
        finally:
            _unlock(fh)


@contextmanager
def export_operation(root, version):
    lock_path, state_path = paths(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as fh:
        if fh.seek(0, 2) == 0:
            fh.write(b'\0')
            fh.flush()
        if _try_lock(fh) is not True:
            raise ValueError(BUSY_MESSAGE)
        state = {'status': 'running', 'version': version,
                 'started_at': datetime.now(timezone.utc).isoformat()}
        try:
            write_json_atomic(state_path, state)
            yield
        except BaseException as exc:
            write_json_atomic(state_path, {**state, 'status': 'failed', 'error': str(exc)[:500],
                                          'finished_at': datetime.now(timezone.utc).isoformat()})
            raise
        else:
            write_json_atomic(state_path, {**state, 'status': 'complete',
                                          'finished_at': datetime.now(timezone.utc).isoformat()})
        finally:
            _unlock(fh)
