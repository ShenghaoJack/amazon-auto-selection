"""报告 Crew 的 Agent 定义(Phase 4)。

**依赖策略**: 
- `DEFAULT_REPORT_ROLES` / `DEFAULT_REPORT_TASKS` / `DEFAULT_QA_ROUTER_ROLE` /
  `DEFAULT_QA_CATEGORIES` 是**纯数据 default spec**(代码内建, YAML 未提供时的回退);
- `tool_text_bundle()` / `mock_run_gather()` 是**纯函数**(不依赖 crewAI, 可单测);
- `build_report_agents(ctx, llm, roles=None)` 才懒导入 `crewai.Agent`。

**提示词外置**: 真正生效的角色/任务提示词由 `agent.crews.prompt_config`
从 `agent/prompts.yaml`(+ 可选 `data/<CAT>/prompts.yaml`)读取并合并到这里的默认值,
缺失字段自动回退到本模块的 `DEFAULT_*`。

⚠️ 本层只读产物, 不触碰 `steps/*`、`core/orchestrator.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.context.category_context import CategoryContext
from agent.tools import artifact_tools as _at

if TYPE_CHECKING:
    from crewai import Agent


# ---------------------------------------------------------------- 默认角色/任务
# 顺序即执行顺序。tool_names 决定每个 agent 可调用的工具(子集)。
DEFAULT_REPORT_ROLES: list[dict] = [
    {
        "key": "researcher",
        "role": "类目数据研究员",
        "goal": "从交付产物中提取准确、可引用的核心指标",
        "backstory": "你只依据工具返回的数据作答, 不臆造任何数值。",
        "tool_names": ["get_category_overview", "get_kpis", "get_gates",
                       "get_brand_ranking", "get_monitoring"],
    },
    {
        "key": "keyword",
        "role": "关键词分析师",
        "goal": "解读关键词机会(搜索量/同比/CPC/达标词/季节性)",
        "backstory": "专注关键词表, 指出机会词与衰减词。",
        "tool_names": ["get_keyword_table", "get_qualified_keywords", "get_category_overview"],
    },
    {
        "key": "market",
        "role": "市场竞争分析师",
        "goal": "解读品牌集中度/类目门禁/价格带/渠道格局",
        "backstory": "专注门禁与品牌排行, 判断类目进入难度。",
        "tool_names": ["get_gates", "get_brand_ranking", "get_variant_table",
                       "get_attribute_frequency"],
    },
    {
        "key": "lead",
        "role": "类目负责人",
        "goal": "综合各分析师结论, 形成机会/风险/可执行的选品建议",
        "backstory": "你是最终决策者, 任何结论必须能追溯到数据, 明确标注不确定处。",
        "tool_names": [],   # 只做综合, 不加新工具(避免与下游重复取数)
    },
]

DEFAULT_REPORT_TASKS: list[dict] = [
    {
        "agent_key": "researcher",
        "description": "提取并核验该类目核心指标(变体数/总销量/总销额/价格/品牌数/四类门禁判定)。"
                       "逐条列出数值并标注来源。只依据工具返回。",
        "expected_output": "结构化指标清单, 每条带数值与来源。",
    },
    {
        "agent_key": "keyword",
        "description": "分析关键词表: 达标词、高搜索量词、同比与季节性信号、CPC 高低。",
        "expected_output": "关键词机会与风险的要点。",
    },
    {
        "agent_key": "market",
        "description": "分析品牌格局与类目门禁: Top品牌集中度、价格带、新老品、属性差异化空间。",
        "expected_output": "市场结构判断与进入难度结论。",
    },
    {
        "agent_key": "lead",
        "description": "综合以上, 输出一份中文选品分析报告: 概览/机会/风险/结论与建议。"
                       "每个关键数字必须引用来源工具。",
        "expected_output": "Markdown 报告。",
    },
]

# ---- 问答 Crew 默认(Phase 5) ----
DEFAULT_QA_ROUTER_ROLE: dict = {
    "role": "问答路由",
    "goal": "判断问题类别并决定由哪个分析师回答, 汇总成最终答案。",
    "backstory": (
        "问题分级: 指标类→研究员; 关键词类→关键词分析师; 竞争/门禁→市场分析师; "
        "综合/建议→类目负责人。回答要简洁、可引用。"
    ),
}

DEFAULT_QA_CATEGORIES: list[dict] = [
    {"key": "kpi", "label": "核心指标", "analyst_key": "researcher",
     "keywords": ["kpi", "指标", "月销量", "月销额", "变体", "价格", "品牌数", "总数", "概况", "多少"]},
    {"key": "keyword", "label": "关键词", "analyst_key": "keyword",
     "keywords": ["关键词", "搜索量", "同比", "cpc", "词", "搜索", "季节性"]},
    {"key": "market", "label": "市场竞争", "analyst_key": "market",
     "keywords": ["门禁", "品牌", "竞争", "市场", "价格带", "集中度", "新老品", "属性", "差异化"]},
    {"key": "general", "label": "综合/建议", "analyst_key": "lead",
     "keywords": ["建议", "值不值得", "机会", "风险", "分析", "总结", "报告", "综合", "选品", "结论"]},
]

# ---- 向后兼容别名(与旧常量名一致, 供测试/外部引用) ----
REPORT_ROLES: list[dict] = DEFAULT_REPORT_ROLES
REPORT_TASKS: list[dict] = DEFAULT_REPORT_TASKS
QA_ROUTER_ROLE: dict = DEFAULT_QA_ROUTER_ROLE
QA_CATEGORIES: list[dict] = DEFAULT_QA_CATEGORIES

# 每个 agent 可用的工具名 → 工具列表(供 mock 冒烟按 agent 取数)
AGENT_TOOL_MAP: dict[str, list[str]] = {
    r["key"]: r["tool_names"] for r in DEFAULT_REPORT_ROLES
}
ROLE_BY_KEY: dict[str, dict] = {r["key"]: r for r in DEFAULT_REPORT_ROLES}
CATEGORY_TO_ANALYST: dict[str, str] = {c["key"]: c["analyst_key"] for c in DEFAULT_QA_CATEGORIES}
CATEGORY_LABEL: dict[str, str] = {c["key"]: c["label"] for c in DEFAULT_QA_CATEGORIES}

# 工具名 → 纯文本取数函数(供 mock 冒烟直接调用, 无需 crewAI)
TOOL_TEXT: dict[str, callable] = {
    "get_category_overview": _at.get_category_overview_text,
    "get_kpis": _at.get_kpis_text,
    "get_gates": _at.get_gates_text,
    "get_brand_ranking": _at.get_brand_ranking_text,
    "get_keyword_table": _at.get_keyword_table_text,
    "get_qualified_keywords": _at.get_qualified_keywords_text,
    "get_variant_table": _at.get_variant_table_text,
    "get_attribute_frequency": _at.get_attribute_frequency_text,
    "get_monitoring": _at.get_monitoring_text,
}


# ---------------------------------------------------------------- 纯函数(可测)
def tool_text_bundle(ctx: CategoryContext, agent_key: str,
                     roles: list[dict] | None = None) -> dict[str, str]:
    """按 agent 的工具集调用纯取数函数, 返回 {工具名: 文本}(mock 冒烟用)。"""
    roles = roles or DEFAULT_REPORT_ROLES
    tools_for_key = next((r["tool_names"] for r in roles if r["key"] == agent_key), [])
    out: dict[str, str] = {}
    for name in tools_for_key:
        func = TOOL_TEXT.get(name)
        if func is not None:
            out[name] = func(ctx)
    return out


def mock_run_gather(ctx: CategoryContext, tasks: list[dict] | None = None,
                    roles: list[dict] | None = None) -> list[dict]:
    """按任务顺序, 为每个 agent 收集其工具证据(无 LLM, 纯结构冒烟)。"""
    tasks = tasks or DEFAULT_REPORT_TASKS
    roles = roles or DEFAULT_REPORT_ROLES
    role_by_key = {r["key"]: r for r in roles}
    steps = []
    for t in tasks:
        key = t["agent_key"]
        steps.append({
            "agent_key": key,
            "role": role_by_key.get(key, {}).get("role", key),
            "tools": tool_text_bundle(ctx, key, roles),
        })
    return steps


# ---------------------------------------------------------------- 懒构建(需 crewAI)
def build_report_agents(ctx: CategoryContext, llm,
                        roles: list[dict] | None = None):
    """把 spec 变成 crewai `Agent` 列表(顺序与 roles 一致; 缺省用 DEFAULT_REPORT_ROLES)。"""
    try:
        from crewai import Agent
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构建 Agent 需要 crewai, 请先安装: pip install -r agent/requirements.txt"
        ) from exc

    roles = roles or DEFAULT_REPORT_ROLES
    tools = _at.build_tools(ctx)                  # crewai Tool 列表
    by_name = {getattr(t, "name", ""): t for t in tools}
    agents = []
    for role in roles:
        role_tools = [by_name[n] for n in role["tool_names"] if n in by_name]
        agents.append(
            Agent(
                role=role["role"],
                goal=role["goal"],
                backstory=role["backstory"],
                tools=role_tools,
                llm=llm,
                verbose=True,
            )
        )
    return agents
