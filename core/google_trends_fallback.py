"""Choose traceable Google Trends evidence without treating an index as volume."""
from __future__ import annotations

import calendar
import math
from datetime import date, timedelta


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
        return n if math.isfinite(n) and 0 <= n <= 100 else None
    except (TypeError, ValueError):
        return None


def _weeks(year, month):
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    start = first + timedelta(days=(6 - first.weekday()) % 7)
    out = []
    while start + timedelta(days=6) <= last:
        out.append(start)
        start += timedelta(days=7)
    return out


def resolve_google_trend(monthly, weekly, target):
    monthly = monthly if isinstance(monthly, dict) else {}
    weekly = weekly if isinstance(weekly, dict) else {}
    target = str(target or "").replace("-", "")
    if len(target) != 6 or not target.isdigit() or not 1 <= int(target[4:]) <= 12:
        return {"kind": "unavailable", "index": None, "yoy": None, "period": ""}
    year, month = int(target[:4]), int(target[4:])
    key = f"{year:04d}-{month:02d}"
    def yoy(current, previous):
        return (current - previous) / previous * 100 if current is not None and previous is not None and previous > 0 else None
    current = _num(monthly.get(key))
    if current is not None:
        return {"kind": "target_month", "index": current,
                "yoy": yoy(current, _num(monthly.get(f"{year-1:04d}-{month:02d}"))), "period": key}
    weeks, prior = _weeks(year, month), _weeks(year - 1, month)
    # A calendar month can contain only three complete Sunday-to-Saturday
    # weeks. In that case use the last four complete weeks ending by month-end
    # in both years, and expose the actual (possibly cross-month) ranges.
    kind = "complete_weeks_mean"
    if len(weeks) < 4:
        last = date(year, month, calendar.monthrange(year, month)[1])
        last_saturday = last - timedelta(days=(last.weekday() - 5) % 7)
        weeks = [last_saturday - timedelta(days=27 - 7 * i) for i in range(4)]
        prev_last = date(year - 1, month, calendar.monthrange(year - 1, month)[1])
        prev_saturday = prev_last - timedelta(days=(prev_last.weekday() - 5) % 7)
        prior = [prev_saturday - timedelta(days=27 - 7 * i) for i in range(4)]
        kind = "recent_four_weeks_mean"
    def collect(days):
        vals = [_num(weekly.get(d.isoformat())) for d in days]
        return sum(vals) / len(vals) if vals and all(v is not None for v in vals) else None
    current = collect(weeks)
    if current is not None:
        previous = collect(prior) if len(prior) == len(weeks) else None
        return {"kind": kind, "index": current, "yoy": yoy(current, previous),
                "period": f"{weeks[0]}~{weeks[-1] + timedelta(days=6)}", "weeks": len(weeks),
                "prior_period": f"{prior[0]}~{prior[-1] + timedelta(days=6)}" if previous is not None else ""}
    # Restrict fallback to prior months; never borrow a later month.
    choices = sorted((k for k in monthly if isinstance(k, str) and len(k) == 7 and k < key and _num(monthly[k]) is not None), reverse=True)
    if choices:
        chosen = choices[0]
        current = _num(monthly[chosen])
        previous = _num(monthly.get(f"{int(chosen[:4])-1:04d}{chosen[4:]}"))
        return {"kind": "previous_month", "index": current, "yoy": yoy(current, previous), "period": chosen}
    return {"kind": "unavailable", "index": None, "yoy": None, "period": ""}
