"""Phase 5 · 问答 Crew(路由分类 + Mock)测试。

零 crewAI、零 LLM: 验证规则分类、类别→分析师映射, 以及 `ask_mock` 在真实类目上
能按匹配的分析师收集证据并组装答案骨架(含优雅降级)。`build_qa_crew` 只测依赖/退化。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent.context.category_context import CategoryContext
from agent._vendor import Config
from agent.crews import qa_crew as qc

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


def test_category_mapping():
    # 每个类别都映射到存在的分析师(分析师 key 来自 REPORT_ROLES)
    assert qc.CATEGORY_TO_ANALYST["kpi"] == "researcher"
    assert qc.CATEGORY_TO_ANALYST["keyword"] == "keyword"
    assert qc.CATEGORY_TO_ANALYST["market"] == "market"
    assert qc.CATEGORY_TO_ANALYST["general"] == "lead"
    for c in qc.QA_CATEGORIES:
        assert c["key"] in qc.CATEGORY_LABEL
        assert c["analyst_key"] in ("researcher", "keyword", "market", "lead")


def test_classify_question():
    assert qc.classify_question("月销量和价格中位数是多少?") == "kpi"
    assert qc.classify_question("有哪些达标关键词?") == "keyword"
    assert qc.classify_question("Top5 品牌集中度?") == "market"
    assert qc.classify_question("这个类目值不值得做?") == "general"
    # 无命中 → default
    assert qc.classify_question("xxxx", default="general") == "general"


def test_analyst_for_category():
    assert qc.analyst_for_category("keyword") == "keyword"
    assert qc.analyst_for_category("unknown") == "lead"   # 兜底


def test_ask_mock_full(full_ctx):
    out = qc.ask_mock(full_ctx, "这个类目的核心指标怎么样?")
    assert out["mock"] is True
    assert out["category"] == "kpi"
    assert out["analyst"] == "researcher"
    assert "Mock" in out["answer"]
    assert "总变体月销额" in out["answer"] or "变体总数" in out["answer"]
    assert out["source_blocks"]


def test_ask_mock_keyword(full_ctx):
    out = qc.ask_mock(full_ctx, "有哪些达标关键词?")
    assert out["analyst"] == "keyword"
    assert "get_qualified_keywords" in out["tools"]
    assert "salt and pepper" in out["answer"]


def test_ask_mock_partial(partial_ctx):
    # 缺报告/属性也降级, 不抛错
    out = qc.ask_mock(partial_ctx, "核心指标?")
    assert out["mock"] is True
    assert out["analyst"] == "researcher"


def test_build_qa_crew_dependency():
    cfg = Config({"LLM_API_KEY": "sk-test", "LLM_MODEL": "deepseek-chat"})
    if importlib.util.find_spec("crewai") is None:
        with pytest.raises(RuntimeError, match="crewai"):
            qc.build_qa_crew(CategoryContext(str(FULL_CAT)), cfg)
    else:
        with pytest.raises(RuntimeError, match="LLM 配置缺失"):
            qc.build_qa_crew(CategoryContext(str(FULL_CAT)), Config({}))
