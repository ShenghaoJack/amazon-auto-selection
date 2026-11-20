"""Recover auditable trend tables from the deterministic step-15 evidence."""
from __future__ import annotations

import re
import math

from core.step15_display import format_text

_PART = re.compile(r"^\s*(.+?)[：:]\s*(20\d{2}-(?:0[1-9]|1[0-2]).*)$")
_MONTH_VALUE = re.compile(r"(20\d{2}-(?:0[1-9]|1[0-2]))\s*([+-]?\d+(?:\.\d+)?)\s*[%％]")
_SUMMARY = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*[%％]\s*$")


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
