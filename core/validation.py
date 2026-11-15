"""上游产物校验 (7.3 硬性)。

下游步骤读取上游产物前必须校验存在性; 缺失时告警或阻断, 禁止静默降级。
"""
from __future__ import annotations

import os

from .errors import StepDataError


def require_file(path: str, label: str = "") -> str:
    if not path or not os.path.exists(path):
        raise StepDataError(f"上游产物缺失: {label or path}")
    return path


def require_files(paths: list[tuple[str, str]]) -> None:
    missing = [label for path, label in paths if not os.path.exists(path)]
    if missing:
        raise StepDataError("上游产物缺失: " + ", ".join(missing))


def warn_missing(path: str, label: str = "") -> bool:
    """存在性检查, 缺失只告警返回 False, 由调用方决定降级方式。"""
    ok = bool(path) and os.path.exists(path)
    if not ok:
        print(f"[validation] 告警: 上游产物缺失 {label or path}")
    return ok


def ensure_non_empty(path: str, label: str = "") -> None:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        raise StepDataError(f"上游产物为空: {label or path}")
