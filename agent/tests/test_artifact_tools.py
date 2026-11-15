"""Phase 2 · GroundedTool(纯函数)测试。

只读真实产物, 零 LLM/零 crewAI 依赖。验证:
- 各 `get_*_text(ctx)` 返回紧凑、有内容的文本;
- 优雅降级(smoke-test 缺报告/属性时返回"暂无…"不异常);
- provenance 传播;
- `build_tools(ctx)` 的依赖处理(有 crewai 返 list, 无 crewai 抛 RuntimeError)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent.context.category_context import CategoryContext
from agent.tools import artifact_tools as at

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


def test_category_overview_text(full_ctx):
    t = at.get_category_overview_text(full_ctx)
    assert "salt-pepper-shaker-sets" in t
    assert "2026-07" in t
    assert "可用数据块" in t


def test_kpis_text(full_ctx):
    t = at.get_kpis_text(full_ctx)
    assert "变体总数" in t
    assert "总变体月销额" in t


def test_gates_text(full_ctx):
    t = at.get_gates_text(full_ctx)
    assert "门禁" in t
    assert len([ln for ln in t.splitlines() if "门禁" in ln]) == 4


def test_brand_ranking_text(full_ctx):
    t = at.get_brand_ranking_text(full_ctx)
    assert "MDLUU" in t                       # 首个品牌
    assert "销额占比%" in t


def test_keyword_table_text(full_ctx):
    t = at.get_keyword_table_text(full_ctx, 8)
    assert "盐" not in t                       # 英文关键词
    assert "salt and pepper shakers" in t
    # limit 生效: 最多 8+1(表头) 行
    assert len(t.splitlines()) <= 9


def test_qualified_keywords_text(full_ctx):
    t = at.get_qualified_keywords_text(full_ctx)
    assert "salt and pepper" in t


def test_variant_table_text(full_ctx):
    t = at.get_variant_table_text(full_ctx, 6)
    assert "变体ASIN" in t


def test_attribute_frequency_text(full_ctx):
    t = at.get_attribute_frequency_text(full_ctx)
    assert "属性_" in t


def test_monitoring_text(full_ctx):
    t = at.get_monitoring_text(full_ctx)
    assert "步骤状态分布" in t


def test_provenance_propagates(full_ctx):
    ctx = CategoryContext(str(FULL_CAT))
    _ = at.get_kpis_text(ctx)
    _ = at.get_keyword_table_text(ctx)
    _ = at.get_brand_ranking_text(ctx)
    assert "report" in ctx._provenance
    assert "keywords" in ctx._provenance


def test_graceful_degrade(partial_ctx):
    # smoke-test 无报告/属性 → 返回"暂无…"不异常
    assert "暂无" in at.get_kpis_text(partial_ctx)
    assert "暂无" in at.get_gates_text(partial_ctx)
    assert "暂无" in at.get_brand_ranking_text(partial_ctx)
    assert "暂无" in at.get_attribute_frequency_text(partial_ctx)
    # 关键字/监控仍在
    assert at.get_qualified_keywords_text(partial_ctx).strip()


def test_build_tools_dependency():
    # build_tools 依赖 crewai: 装在则返回 list, 未装则抛 RuntimeError(带安装提示)
    ctx = CategoryContext(str(FULL_CAT)) if FULL_CAT.exists() else None
    if ctx is None:
        pytest.skip("类目数据缺失")
    has_crewai = importlib.util.find_spec("crewai") is not None
    if has_crewai:
        tools = at.build_tools(ctx)
        assert isinstance(tools, list) and len(tools) == 9
    else:
        with pytest.raises(RuntimeError, match="crewai"):
            at.build_tools(ctx)


def test_build_tools_plain_funcs_importable():
    # 纯函数应能在无 crewai 时被引用(它们是 Phase 2 的核心)
    names = ["get_kpis_text", "get_gates_text", "get_keyword_table_text",
             "get_variant_table_text", "get_attribute_frequency_text"]
    for n in names:
        assert callable(getattr(at, n))
