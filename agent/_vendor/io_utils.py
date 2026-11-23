"""agent 层文件 I/O 工具(自 `core/io_utils.py` 复制改造: 只读为主)。

与主流水线 `core/io_utils.py` 的区别:
- 仅保留读取(JSON/JSONL/TXT/Excel)与 `ensure_dir`; 移除合并/去重/原子写导出类(本层用不到);
- 保留 `write_json`/`atomic_write_text` 供需要缓存/临时落盘时使用。

⚠️ 这是 `core/io_utils.py` 的独立精简拷贝; 改动这里不影响主流水线, 反之亦然。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .errors import StepDataError


def read_jsonl(path: str) -> list[dict]:
    """读取 JSONL, 容忍坏行(跳过解析失败行, 不整体失败)。"""
    records: list[dict] = []
    if not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except (json.JSONDecodeError, ValueError):
                continue
    return records


def read_json(path: str, default=None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError):
        return default


def read_txt_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh]
    return [ln for ln in lines if ln]


def read_excel_sheet(path: str, sheet: int | str = 0) -> pd.DataFrame:
    if not os.path.exists(path):
        raise StepDataError(f"Excel 文件不存在: {path}")
    return pd.read_excel(path, sheet_name=sheet)


def read_excel_all_sheets(path: str) -> dict[str, pd.DataFrame]:
    if not os.path.exists(path):
        raise StepDataError(f"Excel 文件不存在: {path}")
    return pd.read_excel(path, sheet_name=None)


def write_json(path: str, obj: Any) -> None:
    """原子写 JSON(临时文件+改名)。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def atomic_write_text(path: str, content: str) -> None:
    """原子写文本(UTF-8, 临时文件+改名), 文件被占用时换时间戳名。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    target = _unique_if_locked(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        shutil.move(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _unique_if_locked(path: str) -> str:
    try:
        with open(path, "a", encoding="utf-8"):
            return path
    except (PermissionError, OSError):
        base, ext = os.path.splitext(path)
        return f"{base}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"


def ensure_dir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path
