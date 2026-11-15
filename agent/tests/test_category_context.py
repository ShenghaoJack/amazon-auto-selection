"""Phase 1 · CategoryContext 只读加载测试。

只读真实产物, 不调 LLM、无网络。测试自包含(不 import core/steps)。
若类目数据目录缺失则跳过(不因数据不在 CI 环境而失败)。
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from agent.context.category_context import CategoryContext
from agent.context.columns import find_col

ROOT = Path(__file__).resolve().parent.parent.parent
FULL_CAT = ROOT / "data/salt-pepper-shaker-sets/config.yaml"
PARTIAL_CAT = ROOT / "data/smoke-test/config.yaml"


def _skip_if_missing(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"类目数据缺失: {path}")


@pytest.fixture
def full_ctx() -> CategoryContext:
    _skip_if_missing(FULL_CAT)
    return CategoryContext(str(FULL_CAT))


@pytest.fixture
def partial_ctx() -> CategoryContext:
    _skip_if_missing(PARTIAL_CAT)
    return CategoryContext(str(PARTIAL_CAT))


def test_config_and_overview(full_ctx):
    assert full_ctx.cat == "salt-pepper-shaker-sets"
    ov = full_ctx.overview()
    assert ov["cat"] == "salt-pepper-shaker-sets"
    assert ov["site"] == "美国"
    assert ov["market"] == "amazon.com"
    assert ov["traffic_month"] == "2026-07"
    assert isinstance(ov["available"], list) and ov["available"]


def test_keywords_loaded(full_ctx):
    df = full_ctx.keywords
    assert isinstance(df, pd.DataFrame) and not df.empty
    # 关键列应能被定位
    assert find_col(df, ["关键词"]) is not None
    assert find_col(df, ["达标判定"]) is not None
    # 达标词非空
    assert full_ctx.qualified_keywords


def test_report_blocks(full_ctx):
    # 综合分析 sheet 拆出的 KPI / 门禁 / 品牌排行
    assert full_ctx.kpi_rows is not None and not full_ctx.kpi_rows.empty
    assert full_ctx.gate_rows is not None and len(full_ctx.gate_rows) == 4
    assert full_ctx.brand_rank_rows is not None and not full_ctx.brand_rank_rows.empty


def test_report_sheets_keys(full_ctx):
    sheets = full_ctx.report_sheets
    assert "综合分析" in sheets
    assert "关键词与季节性" in sheets
    assert "变体明细" in sheets


def test_attributes_loaded(full_ctx):
    df = full_ctx.attributes
    assert isinstance(df, pd.DataFrame) and not df.empty
    # 属性分布有内容或无(产品可能全"未明确"), 但不抛异常
    assert isinstance(full_ctx.attribute_freq, list)


def test_monitoring(full_ctx):
    mon = full_ctx.monitoring
    assert "step_status" in mon and "alerts" in mon and "health" in mon
    # 0-17 应有至少一个 DONE(说明 step-status 读到)
    assert any(v == "DONE" for v in mon["step_status"].values())


def test_provenance_recorded(full_ctx):
    ctx = CategoryContext(str(FULL_CAT))
    _ = ctx.keywords      # 触发读取
    _ = ctx.report_sheets # 触发读取
    assert "keywords" in ctx._provenance
    assert "report" in ctx._provenance


def test_graceful_missing_artifacts(partial_ctx):
    # smoke-test 类目缺少 报告/属性 等产物 → 优雅降级, 不抛异常
    # report_sheets 可能为空 dict, attributes 可能为 None
    assert isinstance(partial_ctx.report_sheets, dict)
    assert partial_ctx.attributes is None or isinstance(partial_ctx.attributes, pd.DataFrame)
    # 无论是否有报告, 均不应抛异常
    _ = partial_ctx.overview()
    assert isinstance(partial_ctx.available(), list)


def test_missing_config_raises():
    with pytest.raises(Exception):
        CategoryContext(str(ROOT / "no-such-config.yaml"))
