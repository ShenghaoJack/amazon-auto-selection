"""Phase 4 · 报告 Crew(结构装配 + Mock)测试。

零 crewAI、零 LLM: 验证 spec 顺序/工具指派, 以及 `run_summary_mock` 在真实类目上
能按任务顺序收集证据并组装报告骨架(含优雅降级)。`build_report_crew` 只测依赖/退化行为。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent.context.category_context import CategoryContext
from agent._vendor import Config
from agent.crews import agents as ag
from agent.crews import report_crew as rc

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


def test_role_order_and_set():
    keys = [r["key"] for r in ag.REPORT_ROLES]
    assert keys == ["researcher", "keyword", "market", "lead"]
    assert len(ag.REPORT_ROLES) == 4


def test_task_order_and_agent_mapping():
    # 任务顺序: researcher → keyword → market → lead; 每个任务指定了存在的 agent
    assert [t["agent_key"] for t in ag.REPORT_TASKS] == ["researcher", "keyword", "market", "lead"]
    for t in ag.REPORT_TASKS:
        assert t["agent_key"] in ag.ROLE_BY_KEY
        assert t["description"] and t["expected_output"]


def test_tool_mapping():
    # 研究员含核心指标工具; 市场含品牌/变体/属性; 负责人不加新工具
    assert "get_kpis" in ag.AGENT_TOOL_MAP["researcher"]
    assert "get_brand_ranking" in ag.AGENT_TOOL_MAP["market"]
    assert "get_attribute_frequency" in ag.AGENT_TOOL_MAP["market"]
    assert ag.AGENT_TOOL_MAP["lead"] == []
    # 所有声明的工具名都应在 TOOL_TEXT 中有对应纯函数
    for tools in ag.AGENT_TOOL_MAP.values():
        for tn in tools:
            assert tn in ag.TOOL_TEXT


def test_tool_text_bundle(full_ctx):
    b = ag.tool_text_bundle(full_ctx, "researcher")
    assert "get_kpis" in b and isinstance(b["get_kpis"], str) and "变体总数" in b["get_kpis"]
    bm = ag.tool_text_bundle(full_ctx, "market")
    assert "get_brand_ranking" in bm


def test_mock_run_gather_order(full_ctx):
    steps = ag.mock_run_gather(full_ctx)
    assert [s["agent_key"] for s in steps] == ["researcher", "keyword", "market", "lead"]
    assert steps[0]["role"] == "类目数据研究员"


def test_run_summary_mock_full(full_ctx):
    out = rc.run_summary_mock(full_ctx)
    assert out["mock"] is True
    assert "salt-pepper-shaker-sets" in out["report"]
    assert "Mock" in out["report"] or "结构验证" in out["report"]
    # 报告包含了关键工具证据
    assert "门禁" in out["report"]
    assert "品牌" in out["report"]
    assert "达标" in out["report"]
    assert len(out["steps"]) == 4
    assert isinstance(out["sources"], list) and out["sources"]


def test_run_summary_mock_partial(partial_ctx):
    out = rc.run_summary_mock(partial_ctx)
    assert out["mock"] is True
    # 缺报告/属性时优雅降级, 不抛错
    assert "暂无" in out["report"] or "达标" in out["report"]


def test_build_report_crew_dependency():
    # 有真实 ctx; 用带 key 的配置, 但 crewai 未装 → 报"需要 crewai"
    cfg = Config({"LLM_API_KEY": "sk-test", "LLM_MODEL": "deepseek-chat"})
    if importlib.util.find_spec("crewai") is None:
        with pytest.raises(RuntimeError, match="crewai"):
            rc.build_report_crew(CategoryContext(str(FULL_CAT)), cfg)
    else:
        # 未提供真实 key 时(缺 key 配置) build_llm 会先报"配置缺失"
        with pytest.raises(RuntimeError, match="LLM 配置缺失"):
            rc.build_report_crew(CategoryContext(str(FULL_CAT)), Config({}))
