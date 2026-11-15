"""文件 I/O 工具。

覆盖需求文档的数据保存要求:
- JSONL 追加写 + 坏行容忍读取 (7.6 数据恢复)
- 原子写(临时文件+改名), 输出被占用换时间戳名 (7.6 输出防覆盖)
- 多 sheet Excel 写出 / 读取
- FR-11/FR-13 的"所有数据按行合并"(列名检查, 不一致告警仍对齐合并)
- FR-14 的按业务主键去重保留信息最全行
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
    """读取 JSONL, 容忍坏行(跳过解析失败行, 不整体失败)。

    2026-09-11: 读取统一用 utf-8-sig —— 文件若被记事本 / PowerShell 5.1
    `Set-Content -Encoding utf8` 加过 BOM, 按 utf-8 读时**首行**会带 `\\ufeff`,
    json.loads 失败并被当成"坏行"静默跳过(丢第一条记录); utf-8-sig 自动吃掉 BOM。
    """
    records: list[dict] = []
    if not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8-sig") as fh:
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


def append_jsonl(path: str, records: Iterable[dict], dedup_key: str | None = None) -> tuple[int, int]:
    """追加写 JSONL。

    dedup_key 非空时按该字段去重(已存在的同一主体跳过), 返回 (追加数, 跳过数)。
    """
    appended = 0
    skipped = 0
    existing: set[str] = set()
    if dedup_key:
        for rec in read_jsonl(path):
            val = rec.get(dedup_key)
            if val is not None:
                existing.add(str(val))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            if dedup_key:
                val = rec.get(dedup_key)
                if val is not None and str(val) in existing:
                    skipped += 1
                    continue
                existing.add(str(val))
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            appended += 1
    return appended, skipped


def read_json(path: str, default=None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        # utf-8-sig: 带 BOM 的 JSON 用 utf-8 读会解析失败并静默回落 default(见 read_jsonl 说明)
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError):
        return default


def write_json(path: str, obj: Any) -> None:
    """原子写 JSON(临时文件+改名)。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_txt_lines(path: str) -> list[str]:
    """读取文本清单, 去空行。

    2026-09-11: 用 utf-8-sig —— 这些清单(人工关键词清单 `filter-<CAT>-core-words.txt` /
    `filter-<CAT>-longtail-words.txt`、ASIN 清单 parents/variants 等)**可能被人用记事本编辑**,
    加过 BOM 时按 utf-8 读会让**第一条**带上 `\\ufeff`(关键词永远匹配不上 / ASIN 被平台判无效),
    utf-8-sig 自动去掉。
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as fh:
        lines = [ln.strip() for ln in fh]
    return [ln for ln in lines if ln]


def write_txt_lines(path: str, lines: Iterable[str]) -> None:
    atomic_write_text(path, "\n".join(str(ln).strip() for ln in lines) + "\n")


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


def df_to_excel(path: str, sheets: dict[str, pd.DataFrame]) -> str:
    """多 sheet 写出; 文件被占用时换时间戳名, 返回实际写入路径。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    target = _unique_if_locked(path)
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=str(name)[:31], index=False)
    return target


def read_excel_sheet(path: str, sheet: int | str = 0) -> pd.DataFrame:
    if not os.path.exists(path):
        raise StepDataError(f"Excel 文件不存在: {path}")
    return pd.read_excel(path, sheet_name=sheet)


def read_excel_all_sheets(path: str) -> dict[str, pd.DataFrame]:
    if not os.path.exists(path):
        raise StepDataError(f"Excel 文件不存在: {path}")
    return pd.read_excel(path, sheet_name=None)


def merge_excel_by_rows(
    directory: str,
    out_path: str,
    exclude_names: Iterable[str] = (),
    sheet_index: int = 0,
) -> pd.DataFrame | None:
    """FR-11/FR-13 自动合并: 目录内全部 xlsx 按行合并。

    每个文件取第一个 sheet; 列名全部一致直接合并;
    不一致时告警但仍按列名对齐合并(缺失列填空)。
    无数据文件时返回 None(调用方负责告警跳过)。
    """
    files = sorted(
        p
        for p in Path(directory).glob("*.xlsx")
        if p.name not in set(exclude_names) and not p.name.startswith("~$")
    )
    if not files:
        return None
    frames: list[pd.DataFrame] = []
    first_cols: list[str] | None = None
    for f in files:
        try:
            df = pd.read_excel(f, sheet_name=sheet_index)
        except Exception:
            continue
        cols = list(df.columns)
        if first_cols is None:
            first_cols = cols
        elif cols != first_cols:
            print(f"[merge] 警告: {f.name} 列名与首个文件不一致, 按列名对齐合并")
        frames.append(df)
    if not frames:
        return None
    merged = pd.concat(frames, axis=0, ignore_index=True, sort=False)
    if first_cols:
        missing = [c for c in first_cols if c not in merged.columns]
        for c in missing:
            merged[c] = None
        merged = merged[[c for c in first_cols if c in merged.columns]]
    # 2026-09-13: 与同模块其它写出路径统一 —— 经 _unique_if_locked 落盘(原子写 + 目标被 Excel 等
    # 占用时换时间戳名)。原来直接 merged.to_excel(out_path) 绕过该保护: 用户在 Excel 里打开交付物
    # (步骤11/13 的合并文件就写在被扫描目录里)时重跑, openpyxl 以 "wb" 截断写入 → PermissionError
    # 冒到 orchestrator 被标 FAILED 阻断整轮, 且非原子写中途被杀会毁掉上一版交付物。
    target = _unique_if_locked(out_path)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    merged.to_excel(target, index=False)
    return merged


def dedup_keep_best(df: pd.DataFrame, keys: list[str], weight_cols: list[str] | None = None) -> pd.DataFrame:
    """按 keys 去重, 保留信息最全行(FR-14/5.2 唯一性要求)。

    信息最全 = 非空字段数 + 标题/五点等长文本字段长度加权。
    weight_cols 指定长度加权的文本列。
    """
    if df.empty:
        return df
    weight_cols = weight_cols or []

    def score(row: pd.Series) -> float:
        s = 0.0
        for col in df.columns:
            val = row.get(col)
            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                s += 1.0
                if col in weight_cols:
                    s += len(str(val)) * 0.01
        return s

    df = df.copy()
    df["__score"] = df.apply(score, axis=1)
    result = df.sort_values("__score", ascending=False).drop_duplicates(subset=keys, keep="first")
    result = result.drop(columns=["__score"])
    return result.reset_index(drop=True)


def ensure_dir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path
