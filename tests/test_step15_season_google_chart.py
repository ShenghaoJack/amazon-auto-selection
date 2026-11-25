"""Unit tests for Step 15 季节波动系数 3-year Google Trends line chart payload and UI wiring."""
from __future__ import annotations

import json
from pathlib import Path

from core.step15_trend_detail import season_google_trend_detail


def _write_kw_data(root: Path, records: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{root.name}-amazon-keyword-data.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def _ym_range(start_year: int, start_month: int, count: int) -> list[str]:
    base = start_year * 12 + (start_month - 1)
    return [f"{(base + i) // 12:04d}-{(base + i) % 12 + 1:02d}" for i in range(count)]


def test_season_google_trend_detail_extracts_36_months_for_qualified_words(tmp_path):
    root = tmp_path / "CatFeeder-US-202610"
    all_months = _ym_range(2021, 11, 61)  # 2021-11 .. 2026-11
    _write_kw_data(root, [
        {
            "keyword": "cat feeder",
            "monthly_search_volume": 60000,
            "google_monthly_index": {m: float((i % 12) * 5 + 10) for i, m in enumerate(all_months)},
        },
        {
            "keyword": "automatic cat feeder",
            "monthly_search_volume": 45000,
            "google_monthly_index": {m: float((i % 12) * 4 + 20) for i, m in enumerate(all_months) if m != "2025-02"},
        },
        {
            "keyword": "unqualified word",
            "monthly_search_volume": 1000,
            "google_monthly_index": {m: 50.0 for m in all_months},
        },
    ])
    rows = [
        {
            "指标": "季节波动系数",
            "证据与限制": (
                "有效核心词 2/2，截至2026-10；"
                "cat feeder（参考）：1.50（峰2025-12 / 谷2025-04）；"
                "automatic cat feeder（参考）：1.40（峰2025-12 / 谷2025-05）"
            ),
        }
    ]
    detail = season_google_trend_detail(root, rows)
    assert detail is not None
    assert len(detail["months"]) == 36
    assert detail["start_month"] == "2023-11"
    assert detail["end_month"] == "2026-10"
    assert [s["name"] for s in detail["series"]] == ["cat feeder", "automatic cat feeder"]
    # Missing month 2025-02 must be None (not 0)
    idx_missing = detail["months"].index("2025-02")
    assert detail["series"][1]["values"][idx_missing] is None
    assert detail["series"][0]["values"][idx_missing] is not None


def test_season_google_trend_detail_fallback_and_missing_file(tmp_path):
    empty_root = tmp_path / "NoData-US-202610"
    empty_root.mkdir()
    assert season_google_trend_detail(empty_root, []) is None

    root = tmp_path / "Fallback-US-202610"
    months = _ym_range(2025, 1, 12)
    _write_kw_data(root, [
        {"keyword": "kw a", "monthly_search_volume": 30000, "google_monthly_index": {m: 35.0 for m in months}},
    ])
    detail = season_google_trend_detail(root, [{"指标": "季节波动系数", "证据与限制": ""}])
    assert detail is not None
    assert detail["start_month"] == "2025-01"
    assert detail["end_month"] == "2025-12"
    assert len(detail["months"]) == 12
    assert detail["series"][0]["name"] == "kw a"


def test_webui_wires_season_google_chart():
    html = Path("webui/index.html").read_text(encoding="utf-8")
    assert 'data["季节Google趋势"]' in html
    assert "function renderSeasonGoogleChart(" in html
    assert "季节波动系数 · 近三年 Google 趋势" in html


def test_pdf_renders_season_google_svg_in_appendix():
    from core.step15_pdf import render_html

    html = render_html("CatFeeder-US-202610", {
        "指标": [
            {"板块": "市场需求", "指标": "季节波动系数", "实际值": 1.2, "单位": "倍", "原表标准": "各词合计每年峰谷比 ≤ 2.5", "判定": "达标", "证据与限制": "有效核心词 2/2"},
        ],
        "建议": [],
        "趋势明细": {},
        "季节Google趋势": {
            "months": ["2024-01", "2024-02", "2024-03"],
            "start_month": "2024-01",
            "end_month": "2024-03",
            "series": [
                {"name": "cat feeder", "values": [20.0, 25.0, 30.0]},
                {"name": "automatic cat feeder", "values": [15.0, None, 22.0]},
            ],
            "source": "Google Trends 月度搜索热度指数（各词独立 0–100 归一化）",
        },
    })
    assert "季节波动系数 · 近三年 Google 趋势（2024-01 ~ 2024-03）" in html
    assert "<svg " in html
    assert "<path d=" in html
    assert "cat feeder" in html
    assert "automatic cat feeder" in html
