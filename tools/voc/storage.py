"""VOC atomic JSON writes tolerate temporary Windows read/scan locks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def atomic_replace(source, target, *, attempts=9, sleep=time.sleep):
    """Retry only denied/sharing errors; never resubmit a model/network request."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (None, 5, 32, 33) or attempt + 1 == attempts:
                raise
            sleep(min(0.05 * 2 ** attempt, 0.5))


def save_json(path, data, *, default=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary, complete = None, False
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".pending", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False, default=default)
            complete = True
        atomic_replace(temporary, path)
    except OSError as exc:
        if complete:
            # Retain the complete response if publishing remains blocked. It is
            # not a success marker; classifier cache recovery validates it first.
            exc.add_note(f"VOC未发布数据已保留，可恢复文件: {temporary}")
            temporary = None
        raise
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def pending_json(path):
    """Newest complete pending JSON for this exact target; callers must validate."""
    path = Path(path)
    for pending in sorted(path.parent.glob(path.name + ".*.pending"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            with pending.open(encoding="utf-8") as stream:
                yield pending, json.load(stream)
        except (ValueError, UnicodeError):
            continue  # Incomplete writes are never treated as success.
