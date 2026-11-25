"""Recover auditable trend tables from the deterministic step-15 evidence."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from core.step15_display import format_text

_PART = re.compile(r"^\s*(.+?)[：:]\s*(20\d{2}-(?:0[1-9]|1[0-2]).*)$")
_MONTH_VALUE = re.compile(r"(20\d{2}-(?:0[1-9]|1[0-2]))\s*([+-]?\d+(?:\.\d+)?)\s*[%％]")
_SUMMARY = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*[%％]\s*$")
_YM_RE = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")
_CUTOFF_RE = re.compile(r"截至\s*(20\d{2}-(?:0[1-9]|1[0-2]))")
_SEASON_WORD_RE = re.compile(r"^\s*(.+?)（参考）[：:]")


def trend_detail(row):
    evidence = str(row.get("证据与限制") or "")
    segments = []
    for part in evidence.split("；"):
        match = _PART.match(part)
        if not match:
            continue
        values = _MONTH_VALUE.findall(match.group(2))
        if len(values) == 3:
            segments.append((match.group(1).strip(), values))
    totals = [values for name, values in segments if name in ("合计", "各词加总")]
    words = [(name, values) for name, values in segments if name not in ("合计", "各词加总")]
    raw_summary = row.get("实际值")
    if isinstance(raw_summary, (int, float)) and not isinstance(raw_summary, bool) and math.isfinite(raw_summary):
        summary = f"{raw_summary:+.12f}%"
    else:
        summary = str(raw_summary if raw_summary is not None else "").strip()
    if len(totals) != 1 or not words or not _SUMMARY.fullmatch(summary):
        return None
    months = [m for m, _ in totals[0]]
    ordinals = [int(m[:4]) * 12 + int(m[-2:]) for m in months]
    if any(b - a != 1 for a, b in zip(ordinals, ordinals[1:])):
        return None
    if any([month for month, _ in values] != months for _, values in words):
        return None
    if len({name.casefold() for name, _ in words}) != len(words):
        return None
    display = lambda values: [format_text(value + "%") for _, value in values]
    return {"months": months, "words": [{"name": name, "values": display(values)} for name, values in words],
            "total": display(totals[0]), "mean": format_text(summary)}


def _shift_ym(ym: str, offset: int) -> str:
    year, month = int(ym[:4]), int(ym[5:7])
    total = year * 12 + (month - 1) + offset
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _clean_google_series(raw) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        ym = str(k or "").strip()[:7]
        if not _YM_RE.fullmatch(ym):
            continue
        if isinstance(v, bool) or v is None:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num) and num >= 0:
            out[ym] = round(num, 2)
    return out


def _extract_target_keywords(rows) -> list[str]:
    by_name = {r.get("指标"): r for r in (rows or []) if isinstance(r, dict)}
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(word: str) -> None:
        w = str(word or "").strip()
        if w and w.casefold() not in seen:
            seen.add(w.casefold())
            ordered.append(w)

    # 1. 优先取「季节波动系数」证据里的有效核心词
    season_row = by_name.get("季节波动系数")
    if season_row:
        for part in str(season_row.get("证据与限制") or "").split("；"):
            m = _SEASON_WORD_RE.match(part)
            if m:
                _add(m.group(1))

    # 2. 其次取「趋势方向（Google）」/「趋势方向（站内）」明细中的核心词
    if not ordered:
        for ind in ("趋势方向（Google）", "趋势方向（站内）"):
            r = by_name.get(ind)
            if not r:
                continue
            detail = trend_detail(r)
            if detail:
                for w in detail.get("words") or []:
                    _add(w.get("name", ""))
            if ordered:
                break

    # 3. 再次取「核心词搜索量」实际值里的逐词列表（格式 "word: 50000；..."）
    if not ordered:
        vol_row = by_name.get("核心词搜索量")
        if vol_row:
            raw_val = str(vol_row.get("实际值") or "")
            for seg in re.split(r"[；;]", raw_val):
                if ":" in seg or "：" in seg:
                    kw = re.split(r"[:：]", seg, maxsplit=1)[0].strip()
                    if kw and "无符合" not in kw:
                        _add(kw)
    return ordered


def _extract_cutoff_month(rows) -> str | None:
    by_name = {r.get("指标"): r for r in (rows or []) if isinstance(r, dict)}
    for ind in ("季节波动系数", "趋势方向（Google）", "趋势方向（站内）"):
        r = by_name.get(ind)
        if not r:
            continue
        m = _CUTOFF_RE.search(str(r.get("证据与限制") or ""))
        if m:
            return m.group(1)
    return None


def season_google_trend_detail(root, rows, *, months_window: int = 36):
    """Build 3-year monthly Google Trends line-chart payload for the 季节波动系数 row."""
    root_path = Path(root)
    kw_data_path = root_path / f"{root_path.name}-amazon-keyword-data.json"
    if not kw_data_path.is_file():
        return None
    try:
        raw_records = json.loads(kw_data_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw_records, list):
        return None

    records_by_kw: dict[str, tuple[str, dict[str, float], float]] = {}
    for rec in raw_records:
        if not isinstance(rec, dict):
            continue
        kw = str(rec.get("keyword") or "").strip()
        if not kw:
            continue
        g_series = _clean_google_series(rec.get("google_monthly_index"))
        if not g_series:
            continue
        try:
            vol = float(rec.get("monthly_search_volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        records_by_kw[kw.casefold()] = (kw, g_series, vol)

    if not records_by_kw:
        return None

    targets = _extract_target_keywords(rows)
    selected: list[tuple[str, dict[str, float]]] = []
    if targets:
        for kw in targets:
            hit = records_by_kw.get(kw.casefold())
            if hit:
                selected.append((hit[0], hit[1]))
    if not selected:
        ranked = sorted(records_by_kw.values(), key=lambda x: (-x[2], x[0].casefold()))[:8]
        selected = [(kw, g_series) for kw, g_series, _ in ranked]

    cutoff = _extract_cutoff_month(rows)
    all_months = [m for _, g in selected for m in g if (cutoff is None or m <= cutoff)]
    if not all_months:
        all_months = [m for _, g in selected for m in g]
    if not all_months:
        return None

    end_month = max(all_months)
    window = max(1, int(months_window))
    months = [_shift_ym(end_month, i) for i in range(-(window - 1), 1)]

    # Trim leading empty months if all selected series start later than months[0]
    first_non_empty = None
    for idx, m in enumerate(months):
        if any(m in g for _, g in selected):
            first_non_empty = idx
            break
    if first_non_empty is None:
        return None
    months = months[first_non_empty:]

    series = []
    for kw, g in selected:
        vals = [g.get(m) for m in months]
        if any(v is not None for v in vals):
            series.append({"name": kw, "values": vals})

    if not series:
        return None

    return {
        "months": months,
        "series": series,
        "start_month": months[0],
        "end_month": months[-1],
        "source": "Google Trends 月度搜索热度指数（各词独立 0–100 归一化）",
    }
