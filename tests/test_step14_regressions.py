"""Step14 regression coverage using synthetic data, without live network/MCP calls."""
import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from core.config import Config
from core.errors import StepDataError
from core.orchestrator import PipelineRunner
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import (
    Step14TrafficSplit, _parse_price, _positive_price, _recent_months,
)


def _ctx(tmp_path, **options):
    cfg = Config({"CAT": "regression", "DATA_ROOT": str(tmp_path), "DRY_RUN": True,
                  "SITE": "美国", "LLM_PRICES_MODE": "builtin", **options})
    return StepContext(cfg, Paths(cfg))


def test_traffic_process_columns_removed_only_after_calculation(tmp_path):
    step = Step14TrafficSplit()
    frame = pd.DataFrame({"父ASIN": ["P", "P"], "变体ASIN": ["A", "B"],
                          "标签": ["x", "y"], "流量词数": [2, 6]})
    processed = step._calc_ratio(_ctx(tmp_path), step._variant_count_and_filter(_ctx(tmp_path), frame.copy()))
    assert processed["父体总流量"].tolist() == [8, 8]
    assert processed["流量占比"].tolist() == [0.25, 0.75]
    delivery = step._finalize_processed(processed)
    assert not {"标签", "流量词数", "父体总流量"} & set(delivery.columns)
    assert delivery["流量占比"].tolist() == [0.25, 0.75]
    raw = step._finalize_raw(processed)
    assert {"标签", "流量词数", "父体总流量"} <= set(raw.columns)


@pytest.mark.parametrize("value,site,expected", [
    ("€19,99", "德国", 19.99), ("19,99", "法国", 19.99),
    ("1.234,56 €", "德国", 1234.56), ("1 234,56 €", "法国", 1234.56),
    ("1\u202f234,56 €", "法国", 1234.56), ("$1,234.56", "美国", 1234.56),
    ("CA$1,234.56", "加拿大", 1234.56), ("£19.99", "英国", 19.99),
    ("￥1,234", "日本", 1234.0), ("1.234", "德国", 1234.0),
    ("19.99", "德国", 19.99), (19.99, "德国", 19.99),
    ("R$ 19,99", "巴西", 19.99), ("₹1,234.56", "印度", 1234.56),
    ("$12", "美国", 12.0), ("€0,99", "德国", .99),
])
def test_price_locale(value, site, expected):
    assert _positive_price(value, site) == expected


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf"),
                                  None, True, "-", "free", "19.99-29.99", "1,23,4", "12.3456", "19 99"])
def test_price_invalid(value):
    assert _positive_price(value, "美国") is None


def test_ambiguous_decimal_is_not_thousandfold_price():
    assert _positive_price("1,234", "德国") is None
    assert _positive_price("1.234", "美国") is None


@pytest.mark.parametrize("html,site,expected", [
    ('<span class="a-offscreen">€19,99</span>', "德国", 19.99),
    ('<span class="a-price-whole">19<span class="a-price-decimal">.</span></span>'
     '<span class="a-price-fraction">99</span>', "美国", 19.99),
    ('<span class="a-price-whole">1.234<span class="a-price-decimal">,</span></span>'
     '<span class="a-price-fraction">56</span>', "德国", 1234.56),
    ('<span class="a-price-whole">0</span><span class="a-price-fraction">99</span>', "美国", .99),
    ('<span class="a-price-whole">19</span>', "美国", None),
    ('<span class="a-offscreen">$0</span><span class="a-offscreen">$12.50</span>', "美国", 12.5),
    ('"priceAmount": "19.99"', "德国", 19.99),
    ('"priceAmount": "1.234"', "德国", None),
    ('<span class="a-offscreen">1 234,56 €</span>', "法国", 1234.56),
    ('<span class="a-offscreen">€1 234,56</span>', "法国", 1234.56),
    ('<span class="a-offscreen">CA$1,234.56</span>', "加拿大", 1234.56),
    ("<span class='a-offscreen extra'>￥1,234</span>", "日本", 1234.0),
    ('<span class="a-offscreen">$19 99</span>', "美国", None),
    ('<span class="a-offscreen">$19.99-29.99</span>', "美国", None),
    ('"displayPrice": "19,99 €"', "德国", 19.99),
    ('"displayPrice": 19.99', "德国", 19.99),
])
def test_html_price_parts(html, site, expected):
    assert _parse_price(html, site) == expected


def test_scrape_passes_site_and_price_does_not_change_count_parser(tmp_path):
    ctx = _ctx(tmp_path, SITE="德国")
    step = Step14TrafficSplit()
    step._scrape_page = Mock(return_value='<span class="a-offscreen">€19,99</span>')
    assert step._scrape_price(ctx, "A") == 19.99
    assert step._to_float(pd.Series(["1,234"])).iloc[0] == 1234


@pytest.mark.parametrize("value", ["12.3456", "1.234e5", "-19.99", "19.99garbage"])
def test_json_price_cannot_be_partially_matched(value):
    assert _parse_price(f'{{"priceAmount": "{value}"}}', "美国") is None


@pytest.mark.parametrize("invalid", [0, -5, float("inf"), float("nan"), "-"])
def test_all_price_sources_must_be_positive_finite(tmp_path, invalid):
    ctx = _ctx(tmp_path)
    step = Step14TrafficSplit()
    step._scrape_price = Mock(side_effect=[7.5, invalid])
    frame = pd.DataFrame({"变体ASIN": ["A", "B", "C"], "变体月销量": [10, 10, 10],
                          "价格": [invalid, invalid, 12], "__jsonl_price": [invalid, invalid, 99],
                          "父ASIN": ["P", "P", "P"], "配送方式": ["FBA"] * 3,
                          "新老品": ["老品"] * 3})
    out = step._fill_price(ctx, frame)
    assert out["价格"].iloc[0] == 7.5
    assert pd.isna(out["价格"].iloc[1])
    assert pd.isna(out["变体月销额"].iloc[1])
    assert out["价格"].iloc[2] == 12
    assert step._scrape_price.call_count == 2
    step._export(ctx, out, out)
    manual = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name="人工待补充表")
    assert manual["变体ASIN"].tolist() == ["B"]


def test_jsonl_invalid_first_record_does_not_mask_later_valid_price(tmp_path):
    ctx = _ctx(tmp_path, SITE="德国")
    step = Step14TrafficSplit()
    path = Path(step._results_path(ctx))
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"request_asin": "A", "price": 0},
               {"request_asin": "A", "price": "€19,99"},
               {"request_asin": "B", "variant_asins": ["B"], "price": {"B": -1}},
               {"request_asin": "B", "variant_asins": ["B"], "price": {"B": "€9,99"}}]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
    frame = pd.DataFrame({"父ASIN": ["P", "P"], "变体ASIN": ["A", "B"]})
    out = step._merge_images(ctx, frame)
    assert out["__jsonl_price"].tolist() == [19.99, 9.99]
    out["变体月销量"] = [10, 20]
    step._scrape_price = Mock(side_effect=AssertionError("unexpected web request"))
    out = step._fill_price(ctx, out)
    assert out["变体月销额"].tolist() == [199.9, 199.8]


@pytest.mark.parametrize("right", ["-", "", "N/A", float("inf"), -1])
def test_invalid_numeric_right_falls_back_before_merge(tmp_path, right):
    step = Step14TrafficSplit()
    frame = pd.DataFrame({"月销量_x": [100], "月销量_y": [right],
                          "评分_x": [4.5], "评分_y": [right],
                          "流量词数_x": [1234], "流量词数_y": [right]})
    out = step._resolve_suffixes(frame, prefer_right={"月销量", "评分", "流量词数"})
    assert out.iloc[0].to_dict() == {"月销量": 100, "评分": 4.5, "流量词数": 1234}


def test_valid_zero_preserved_and_invalid_left_falls_back(tmp_path):
    step = Step14TrafficSplit()
    frame = pd.DataFrame({"月销量_x": [100, "-"], "月销量_y": [0, "1,234"],
                          "评分_x": [4.5, "-"], "评分_y": [0, 4.8]})
    out = step._resolve_suffixes(frame, prefer_right={"月销量", "评分"})
    assert out["月销量"].tolist() == [0, 1234]
    assert out["评分"].tolist() == [0, 4.8]


def _input_frame(counts, sales=None):
    n = len(counts)
    return pd.DataFrame({"父ASIN": ["P"] * n, "变体ASIN": [f"A{i}" for i in range(n)],
                         "月销量": sales if sales is not None else [100] * n,
                         "流量词数": counts, "品牌": ["Sample"] * n,
                         "价格($)": [10] * n, "配送方式": ["FBA"] * n,
                         "上架时间": ["2020-01-01"] * n})


def _offline_step(monkeypatch, frame):
    step = Step14TrafficSplit()
    monkeypatch.setattr(step, "_load_detail", lambda ctx: frame.copy())
    for name in ("_merge_images", "_join_competitor"):
        monkeypatch.setattr(step, name, lambda ctx, df: df)
    monkeypatch.setattr(step, "_load_traffic", lambda ctx: None)
    monkeypatch.setattr(step, "_scrape_page", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(step, "_mcp_fill_logistics", Mock(side_effect=AssertionError("unexpected MCP call")))
    monkeypatch.setattr(step, "_fill_logistics", lambda ctx, df: df)
    return step


def test_full_run_parent_max_is_taken_before_zero_traffic_filter(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    step = _offline_step(monkeypatch, _input_frame([0, 5], [100, 20]))
    step.run(ctx)
    sheets = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name=None)
    assert sheets["原始表"]["月销量"].tolist() == [100, 20]
    auto = sheets["AI已补充"]
    assert auto["变体ASIN"].tolist() == ["A1"]
    assert auto["月销量"].tolist() == [100]
    assert auto["变体月销量"].tolist() == [100]


@pytest.mark.parametrize("counts", [[None, None], [0, None], [0, 0], [-1, None], [float("inf"), 0]])
def test_no_positive_multi_traffic_is_filtered_without_manual_or_blocking(tmp_path, monkeypatch, counts):
    ctx = _ctx(tmp_path)
    step = _offline_step(monkeypatch, _input_frame(counts))
    canonical = Path(ctx.resolve_artifact(step.artifacts[0]))
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"existing canonical output")
    step.run(ctx)
    assert canonical.read_bytes() != b"existing canonical output"
    pending = canonical.parent / "流量拆分数据-流量待补充.xlsx"
    assert not pending.exists()
    sheets = pd.read_excel(canonical, sheet_name=None)
    assert len(sheets["原始表"]) == 2
    assert sheets["AI已补充"].empty
    assert sheets["人工待补充表"].empty
    step._scrape_page.assert_not_called()
    step._mcp_fill_logistics.assert_not_called()


def test_no_positive_traffic_pipeline_publishes_empty_result_and_marks_done(tmp_path, monkeypatch):
    runner = PipelineRunner(_ctx(tmp_path).config, dry_run=True)
    step = _offline_step(monkeypatch, _input_frame([0, None]))
    monkeypatch.setattr(step, "validate_upstream", lambda ctx: None)
    monkeypatch.setattr("core.notify.popup_critical", Mock())
    monkeypatch.setattr("core.notify.push_wecom", Mock())
    sync = Mock()
    monkeypatch.setattr(runner, "_sync_after_step", sync)
    target = Path(runner.ctx.resolve_artifact(step.artifacts[0]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old standard output")
    assert runner._run_one(step) == 0
    assert runner.state.status_of(14) == "DONE"
    sync.assert_called_once_with(step)
    sheets = pd.read_excel(target, sheet_name=None)
    assert len(sheets["原始表"]) == 2
    assert sheets["AI已补充"].empty
    assert sheets["人工待补充表"].empty


def test_no_positive_parent_never_enters_sales_split_or_affects_other_parents(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    known = _input_frame([20, None], [20, 100])
    excluded = _input_frame([None, 0], [500, 500])
    excluded["父ASIN"] = "Q"
    excluded["变体ASIN"] = ["Q0", "Q1"]
    single = _input_frame([None], [77])
    single["父ASIN"], single["变体ASIN"] = "R", "R0"
    frame = pd.DataFrame(known.to_dict("records") + excluded.to_dict("records") + single.to_dict("records"))
    step = _offline_step(monkeypatch, frame)
    split = Mock(wraps=step._split_sales)
    monkeypatch.setattr(step, "_split_sales", split)
    step.run(ctx)
    assert split.call_count == 1
    assert split.call_args.args[1]["父ASIN"].tolist() == ["P", "R"]
    sheets = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name=None)
    assert len(sheets["原始表"]) == 5
    assert sheets["AI已补充"]["父ASIN"].tolist() == ["P", "R"]
    assert sheets["AI已补充"]["变体月销量"].tolist() == [100, 77]
    assert sheets["人工待补充表"].empty


@pytest.mark.parametrize("unknown", [None, "-", -1, float("inf"), float("nan"), 0])
def test_partial_traffic_assigns_all_parent_sales_to_known_variant(tmp_path, monkeypatch, unknown):
    ctx = _ctx(tmp_path)
    # Parent maximum must include the excluded unknown-traffic variant's sales.
    step = _offline_step(monkeypatch, _input_frame([20, unknown], [20, 100]))
    step.run(ctx)
    path = Path(ctx.resolve_artifact(step.artifacts[0]))
    sheets = pd.read_excel(path, sheet_name=None)
    assert len(sheets["原始表"]) == 2
    auto = sheets["AI已补充"]
    assert auto["变体ASIN"].tolist() == ["A0"]
    assert auto["月销量"].tolist() == [100]
    assert auto["流量占比"].tolist() == [1]
    assert auto["变体月销量"].tolist() == [100]
    assert auto["变体月销额"].tolist() == [1000]
    assert not (path.parent / "流量拆分数据-流量待补充.xlsx").exists()


def test_partial_traffic_multiple_known_variants_renormalize_without_unknown(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    step = _offline_step(monkeypatch, _input_frame([20, 30, None], [20, 30, 100]))
    step.run(ctx)
    auto = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name="AI已补充")
    assert auto["变体ASIN"].tolist() == ["A0", "A1"]
    assert auto["月销量"].tolist() == [100, 100]
    assert auto["流量占比"].tolist() == [.4, .6]
    assert auto["变体月销量"].tolist() == [40, 60]


def test_partial_traffic_pipeline_publishes_and_marks_done(tmp_path, monkeypatch):
    runner = PipelineRunner(_ctx(tmp_path).config, dry_run=True)
    step = _offline_step(monkeypatch, _input_frame([20, None], [20, 100]))
    monkeypatch.setattr(step, "validate_upstream", lambda ctx: None)
    sync = Mock()
    monkeypatch.setattr(runner, "_sync_after_step", sync)
    assert runner._run_one(step) == 0
    assert runner.state.status_of(14) == "DONE"
    sync.assert_called_once_with(step)


def test_single_variant_does_not_require_traffic_counts(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    step = _offline_step(monkeypatch, _input_frame([None]))
    step.run(ctx)
    auto = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name="AI已补充")
    assert auto["流量占比"].tolist() == [1]
    assert auto["变体月销量"].tolist() == [100]


def test_low_sales_empty_output_is_intentional(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    step = _offline_step(monkeypatch, _input_frame([None, None], [5, 5]))
    step.run(ctx)
    sheets = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name=None)
    assert len(sheets["原始表"]) == 2
    assert sheets["AI已补充"].empty


def test_blank_keyword_text_does_not_remove_valid_count_or_sales(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    step = Step14TrafficSplit()
    traffic = pd.DataFrame({"变体ASIN": ["A0", "A1"], "流量词": ["", ""],
                            "全部流量词": [2, 6], "月销量": ["-", "-"], "评分": ["-", "-"]})
    monkeypatch.setattr(step, "_load_traffic", lambda ctx: traffic)
    frame = _input_frame([None, None], [100, 20])
    frame["评分"] = [4.5, 4.6]
    out = step._join_traffic(ctx, frame)
    assert out["流量词数"].tolist() == [2, 6]
    assert out["月销量"].tolist() == [100, 20]
    out = step._unify_sales(ctx, out)
    out = step._variant_count_and_filter(ctx, out)
    out = step._calc_ratio(ctx, out)
    out = step._split_sales(ctx, out)
    assert out["变体月销量"].tolist() == [25, 75]


def test_small_positive_ratio_is_not_rounded_to_zero(tmp_path):
    ctx = _ctx(tmp_path)
    step = Step14TrafficSplit()
    frame = _input_frame([1, 10_000_000])
    frame = step._unify_sales(ctx, frame)
    frame = step._variant_count_and_filter(ctx, frame)
    frame = step._calc_ratio(ctx, frame)
    frame = step._split_sales(ctx, frame)
    assert frame["流量占比"].iloc[0] > 0
    assert frame["变体月销量"].iloc[0] == 1


def test_full_run_real_local_inputs_with_competitor_and_traffic_fallback(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, SITE="德国", TREND_MONTH="202603")
    step = Step14TrafficSplit()
    detail = _input_frame([None, None], [100, 20]).drop(columns=["价格($)"])
    detail["评分"] = [4.5, 4.6]
    single = detail.iloc[[0]].copy()
    single["父ASIN"], single["变体ASIN"], single["月销量"] = "Q", "B", 50
    detail = pd.concat([detail, single], ignore_index=True)
    sources = [(step._detail_path(ctx), "变体明细", detail),
               (step._competitor_path(ctx), "Sheet1", pd.DataFrame({"ASIN": ["A0", "A1", "B"],
                 "月销量": ["-", "-", "-"], "价格(€)": ["€19,99", "€9,99", "€5,99"]})),
               (step._traffic_path(ctx), "Sheet1", pd.DataFrame({"ASIN": ["A0", "A1"],
                 "流量词数": [2, 6], "流量词": ["", ""], "月销量": ["-", "-"], "评分": ["-", "-"]}))]
    for path, sheet, frame in sources:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        frame.to_excel(path, sheet_name=sheet, index=False)
    monkeypatch.setattr(step, "_scrape_page", Mock(side_effect=AssertionError("unexpected network")))
    monkeypatch.setattr(ctx, "mcp", Mock(side_effect=AssertionError("unneeded MCP initialization")))
    step.validate_upstream(ctx)
    step.run(ctx)
    sheets = pd.read_excel(ctx.resolve_artifact(step.artifacts[0]), sheet_name=None)
    auto = sheets["AI已补充"]
    assert auto["变体月销量"].tolist() == [25, 75, 50]
    assert auto["月销量"].tolist() == [100, 100, 50]
    assert auto["价格(€)"].tolist() == [19.99, 9.99, 5.99]
    assert auto["变体月销额"].tolist() == [499.75, 749.25, 299.5]
    assert sheets["人工待补充表"].empty
    ctx.mcp.assert_not_called()


@pytest.mark.parametrize("month,expected", [
    ("202603", ["2026-03", "2026-02", "2026-01", "2025-12", "2025-11", "2025-10"]),
    ("202403", ["2024-03", "2024-02", "2024-01", "2023-12", "2023-11", "2023-10"]),
    ("2026-01", ["2026-01", "2025-12", "2025-11", "2025-10", "2025-09", "2025-08"]),
])
def test_natural_months(month, expected):
    assert _recent_months(month, 6) == expected


@pytest.mark.parametrize("month", ["202613", "202600", "garbage", "2026-1", "000001", ""])
def test_invalid_month_does_not_fall_back_to_today(month):
    with pytest.raises(StepDataError, match="月份"):
        _recent_months(month, 6)


def test_mcp_receives_each_natural_month_once_and_latest_value_wins(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, TREND_MONTH="202603")
    query = Mock(side_effect=lambda month, asins: [{"asin": "A", "fulfillment": month}])
    monkeypatch.setattr(ctx, "mcp", lambda: type("MCP", (), {"competitor_lookup": staticmethod(query)})())
    frame = pd.DataFrame({"变体ASIN": ["A"], "配送方式": [""], "上架时间": [""]})
    Step14TrafficSplit()._mcp_fill_logistics(ctx, frame)
    assert [call.args[0] for call in query.call_args_list] == _recent_months("202603", 6)
    assert frame.loc[0, "配送方式"] == "2026-03"


def test_invalid_month_fails_before_initializing_mcp(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, TREND_MONTH="202613")
    monkeypatch.setattr(ctx, "mcp", Mock(side_effect=AssertionError("unexpected MCP initialization")))
    frame = pd.DataFrame({"变体ASIN": ["A"], "配送方式": [""], "上架时间": [""]})
    with pytest.raises(StepDataError, match="月份"):
        Step14TrafficSplit()._mcp_fill_logistics(ctx, frame)
    ctx.mcp.assert_not_called()


def test_month_boundary_sweep():
    for year in (2023, 2024, 2026):
        for month in range(1, 13):
            result = _recent_months(f"{year:04d}{month:02d}", 12)
            stamps = [pd.Period(item, freq="M").ordinal for item in result]
            assert len(set(stamps)) == 12
            assert all(a - b == 1 for a, b in zip(stamps, stamps[1:]))
