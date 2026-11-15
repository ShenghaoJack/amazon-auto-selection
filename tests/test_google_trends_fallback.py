import pytest
from core.google_trends_fallback import resolve_google_trend


def test_complete_weekly_mean_and_prior_year_are_not_summed():
    weekly = dict(zip(["2026-03-01", "2026-03-08", "2026-03-15", "2026-03-22"], [20, 40, 60, 80]))
    weekly.update(dict.fromkeys(["2025-03-02", "2025-03-09", "2025-03-16", "2025-03-23"], 25))
    result = resolve_google_trend({}, weekly, "202603")
    assert result["index"] == 50 and result["yoy"] == 100
    assert result["period"] == "2026-03-01~2026-03-28"
    assert result["kind"] == "complete_weeks_mean"


def test_incomplete_week_uses_previous_month_not_zero_or_future():
    result = resolve_google_trend({"2026-09": 40, "2025-09": 20, "2026-11": 99},
                                  {"2026-10-04": 40}, "202610")
    assert result == {"kind": "previous_month", "index": 40, "yoy": 100, "period": "2026-09"}


def test_target_month_with_missing_previous_year_has_no_yoy():
    result = resolve_google_trend({"2026-10": 50}, {}, "202610")
    assert result["kind"] == "target_month" and result["yoy"] is None


def test_no_matching_prior_week_data_does_not_fabricate_yoy():
    weekly = dict.fromkeys(["2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25"], 50)
    result = resolve_google_trend({}, weekly, "202610")
    assert result["index"] == 50 and result["yoy"] is None


def test_three_week_month_uses_four_week_window_and_labels_range():
    weekly = dict.fromkeys(["2025-09-28", "2025-10-05", "2025-10-12", "2025-10-19"], 40)
    result = resolve_google_trend({}, weekly, "202510")
    assert result["kind"] == "recent_four_weeks_mean"
    assert result["period"] == "2025-09-28~2025-10-25"
    assert result["index"] == 40
