# 单元测试: Google 同比(单月同比: 目标月 vs 去年同月)
from steps.step07_screen import _compute_google_yoy, _single_month_yoy


def _series():
    return {f"2024-{m:02d}": 50 for m in range(1, 13)} | {f"2025-{m:02d}": 50 for m in range(1, 13)} | {f"2026-{m:02d}": 50 for m in range(1, 9)}


def test_single_month_yoy_positive():
    data = _series()
    data["2026-07"] = 60
    data["2025-07"] = 50
    assert _single_month_yoy(data, "2026-07") == 20.0


def test_single_month_yoy_negative():
    data = _series()
    data["2026-07"] = 40
    data["2025-07"] = 50
    assert _single_month_yoy(data, "2026-07") == -20.0


def test_compute_uses_target_month():
    data = _series()
    data["2026-07"] = 60
    data["2025-07"] = 50
    data["2026-06"] = 30
    data["2025-06"] = 50
    # 目标月 2026-07 vs 2025-07 = +20%; 而非最近月 2026-06(-40%)
    assert _compute_google_yoy(data, "202607") == 20.0


def test_compute_fallback_rolling_when_target_missing():
    # 目标月 2026-09 不在序列中 -> 回退滚动同比
    data = _series()
    data["2026-08"] = 70
    data["2025-08"] = 50
    val = _compute_google_yoy(data, "202609")
    assert val is not None


def test_compute_none_on_too_little():
    assert _compute_google_yoy({}, "202607") is None
    assert _compute_google_yoy({"2026-01": 10}, "202607") is None
