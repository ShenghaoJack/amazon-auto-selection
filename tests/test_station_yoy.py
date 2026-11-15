# 单元测试: 站内同比(严格单月同比: 目标月 vs 去年同月, 由月度序列自算)
from core.mcp import _normalize_trend


def _rows():
    """构造 2024-01 ~ 2026-08 的月度序列(每年 7 月搜索量不同)。"""
    rows = []
    for year in (2024, 2025, 2026):
        for m in range(1, 13):
            if year == 2026 and m > 8:
                break
            base = {"search": 100, "purchase": 30, "purchaseRate": 30.0,
                    "chainGrowth": 0.05, "threeMonthGrowth": 0.03}
            if m == 7:
                base["search"] = {2024: 100, 2025: 110, 2026: 132}[year]
            base["time"] = f"{year}年{m:02d}月"
            rows.append(base)
    return rows


def test_station_yoy_self_computed():
    out = _normalize_trend(_rows(), "kw", "202607")
    # 2026-07(132) vs 2025-07(110) = +20%; 而非接口 yearlyGrowth(缺省 None)
    assert out["mom_yoy"] == 20.0


def test_station_yoy_target_missing_uses_last_month():
    out = _normalize_trend(_rows(), "kw", "202601")
    # 目标 2026-01 不在序列(序列只到 2026-08, 2026-01 存在) -> 用 2026-01
    # 2026-01(100) vs 2025-01(100) = 0.0
    assert out["mom_yoy"] == 0.0


def test_series_yoy_self_computed():
    out = _normalize_trend(_rows(), "kw", "202607")
    by_month = {r["month"]: r["yoy"] for r in out["monthly_series"]}
    # 2026-07(132) vs 2025-07(110) = +20%
    assert by_month["2026-07"] == 20.0
    # 2025-07(110) vs 2024-07(100) = +10%
    assert by_month["2025-07"] == 10.0


def test_station_yoy_fallback_when_prev_missing():
    rows = [{"time": "2026年07月", "search": 200, "yearlyGrowth": 0.1}]
    out = _normalize_trend(rows, "kw", "202607")
    # 只有 2026-07, 无 2025-07 -> 兜底接口 yearlyGrowth 10%
    assert out["mom_yoy"] == 10.0
