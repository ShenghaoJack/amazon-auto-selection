"""Persistent, fail-closed human approval gates shared by UI and runner."""
from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()
_EXPORT_CACHE = {}


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _stat(path):
    if path is None:
        return None
    p = Path(path)
    try:
        s = p.stat()
        return str(p), s.st_mtime_ns, s.st_size
    except OSError:
        return str(p), None, None


def _export_confirmed(root: Path, spec_path: Path):
    from tools.attr_stability.step18_workspace import Workspace, OUTPUT
    workspace = Workspace(root, spec_path=spec_path)
    pointer = workspace.pointer
    exported = workspace.store / 'export.json'
    output = root / '18_标准化' / OUTPUT.format(cat=root.name)
    if not pointer.is_file() or not exported.is_file() or not output.is_file():
        return False
    try:
        version = str(_load(pointer)['version'])
        meta = _load(exported)
        if meta.get('version') != version or meta.get('filename') != output.name:
            return False
        files = [pointer, exported, output, spec_path, workspace.rules_path,
                 root/'17_属性标注/input.json',root/'17_属性标注/稳定性结果.json',
                 root/'17_属性标注/视觉补充.json']
        files += sorted((workspace.store/'versions').glob('V*.json'))
        fingerprint = tuple(_stat(p) for p in files)
        key = str(root)
        with _LOCK:
            entry = _EXPORT_CACHE.get(key)
            if entry and entry[0] == fingerprint:
                return entry[1]
        state = workspace.open()
        okay = state['version'] == version and workspace.sources() == state['sources']
        # Do not cache an answer if input files changed during verification.
        if tuple(_stat(p) for p in files) == fingerprint:
            with _LOCK:
                if len(_EXPORT_CACHE) >= 64:
                    _EXPORT_CACHE.clear()
                _EXPORT_CACHE[key] = (fingerprint, okay)
        return okay
    except (OSError, ValueError, KeyError, TypeError):
        return False


def pending_gates(cat_root, statuses):
    """Return required gates in order, without changing any files."""
    from core import keyword_confirmation as kc
    from tools.attr_stability.catalog import resolve_spec_path
    root = Path(cat_root)
    status = lambda n: str(statuses.get(n, 'PENDING'))
    result = []
    try:
        gate = kc.read(root)
        pending = bool(gate and gate.get('status') == 'pending')
        if status(8) == 'DONE':
            if gate and gate.get('status') == 'confirmed' and gate.get('confirmed_words'):
                pending = kc.revision(kc.current(root.name, root.parent)) != gate.get('confirmed_revision')
            if gate is None and status(9) != 'DONE':
                pending = True
    except (OSError, ValueError, TypeError, KeyError):
        pending = status(8) == 'DONE'
    if pending:
        return [{'kind':'keywords','after':8,'message':'请核对并确认关键词清单后继续。'}]
    spec = resolve_spec_path(root.parent, root.name)
    if status(16) == 'DONE':
        try:
            confirmed = _load(spec).get('status') == 'confirmed'
        except (OSError, ValueError, AttributeError):
            confirmed = False
        if not confirmed:
            return [{'kind':'attributes','after':16,'message':'请核对并确认步骤16属性集后继续。'}]
    if status(17) == 'DONE' and not _export_confirmed(root, spec):
        result.append({'kind':'standardization','after':17,'message':'请确认当前标准化结果并导出交付后继续。'})
    return result


def blocking_gate(cat_root, statuses, target):
    # A caller cannot jump directly past an incomplete prerequisite step.
    if target <= 8:
        return None
    pending = pending_gates(cat_root, statuses)
    for step in (8, 16, 17, 18):
        if target > step and statuses.get(step) != 'DONE':
            return {'kind':'prerequisite','after':step,'message':f'请先完成步骤 {step}，再进行后续步骤。'}
        if target > step and pending and pending[0]['after'] == step:
            return pending[0]
    return None
