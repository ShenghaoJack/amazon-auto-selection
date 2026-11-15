"""Strict complete-week keyword evidence for step 4."""
from __future__ import annotations

import calendar
import math
from datetime import date, timedelta


def week_window(month: str, start_weekday: int):
    year, num = int(month[:4]), int(month[4:])
    first = date(year, num, 1)
    last = date(year, num, calendar.monthrange(year, num)[1])
    start = first + timedelta(days=(start_weekday - first.weekday()) % 7)
    weeks = []
    while start + timedelta(days=6) <= last:
        weeks.append(start)
        start += timedelta(days=7)
    if len(weeks) < 4:
        last_start = last - timedelta(days=(last.weekday() - start_weekday) % 7)
        if last_start + timedelta(days=6) > last:
            last_start -= timedelta(days=7)
        weeks = [last_start - timedelta(days=7 * i) for i in reversed(range(4))]
    return weeks


def complete_sif_rows(result, words, month):
    starts = week_window(month, 6)  # SIF labels weeks by Sunday start.
    expected = [d.isoformat() for d in starts]
    if not isinstance(result, dict) or not isinstance(result.get("keywords"), list):
        raise ValueError("SIF 周数据响应格式不完整")
    wanted = {str(w).strip().casefold(): w for w in words}
    found = {}
    for item in result["keywords"]:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("keyword", "")).strip().casefold()
        if ident not in wanted or ident in found:
            continue
        dates, values = item.get("dates"), item.get("volumes")
        if not isinstance(dates, list) or not isinstance(values, list) or len(dates) != len(values):
            continue
        data = {}
        for day, volume in zip(dates, values):
            if day in data:
                data[day] = None
            else:
                data[day] = volume
        vals = [data.get(day) for day in expected]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in vals):
            found[ident] = sum(vals)
    return found, {"source": "SIF", "period_start": starts[0].isoformat(),
                   "period_end": (starts[-1] + timedelta(days=6)).isoformat(),
                   "weeks": len(starts), "kind": "complete_weeks" if starts[0].strftime("%Y%m") == month else "recent_four_weeks"}


def complete_sellersprite_rows(result, word, month):
    starts = week_window(month, 6)
    expected = [(d + timedelta(days=6)).strftime("%Y%m%d") for d in starts]
    if not isinstance(result, list):
        raise ValueError("卖家精灵周数据响应格式不完整")
    data = {}
    for item in result:
        if isinstance(item, dict):
            day = str(item.get("label") or "")
            data[day] = item.get("searches") if day not in data else None
    vals = [data.get(day) for day in expected]
    found = {}
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in vals):
        found[str(word).strip().casefold()] = sum(vals)
    return found, {"source": "卖家精灵", "period_start": starts[0].isoformat(),
                   "period_end": (starts[-1] + timedelta(days=6)).isoformat(),
                   "weeks": len(starts), "kind": "complete_weeks" if starts[0].strftime("%Y%m") == month else "recent_four_weeks"}
