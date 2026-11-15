"""问答 Crew(层级进程) — Phase 5。

- `classify_question` / `analyst_for_category` / `ask_mock(ctx, question)`:
  **无 LLM、无 crewAI** 的规则分类 + 结构冒烟, 验证路由/派发/工具集合正确。
- `build_qa_crew(ctx, config, llm=None)`: 懒导入 crewAI 构造层级进程 `Crew`
  (路由 Agent 委派给对应分析师)。`ask(...)` = 构造 + kickoff。

⚠️ 本层只读产物, 不触碰 `steps/*`、`core/orchestrator.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.context.category_context import CategoryContext
from agent.crews.agents import (
    CATEGORY_LABEL,
    CATEGORY_TO_ANALYST,
    DEFAULT_QA_CATEGORIES,
    DEFAULT_QA_ROUTER_ROLE,
    ROLE_BY_KEY,
    build_report_agents,
    tool_text_bundle,
)
from agent.crews.prompt_config import (
    effective_qa_categories,
    effective_qa_router,
    effective_report_roles,
)
from agent.crews.llm_factory import build_llm

if TYPE_CHECKING:
    from agent._vendor import Config

# 向后兼容: 默认为代码内建值(外置读取见 effective_qa_*)
QA_ROUTER_ROLE: dict = DEFAULT_QA_ROUTER_ROLE
QA_CATEGORIES: list[dict] = DEFAULT_QA_CATEGORIES


# ---------------------------------------------------------------- 纯函数(可测)
def classify_question(question: str, default: str = "general",
                      categories: list[dict] | None = None) -> str:
    """无 LLM 的规则分类: 按关键词命中数最高者定类别; 无命中用 default。"""
    cats = categories or DEFAULT_QA_CATEGORIES
    q = (question or "").lower()
    best, best_score = default, 0
    for c in cats:
        score = sum(1 for kw in c.get("keywords", []) if kw in q)
        if score > best_score:
            best, best_score = c["key"], score
    return best


def analyst_for_category(cat_key: str, categories: list[dict] | None = None) -> str:
    cats = categories or DEFAULT_QA_CATEGORIES
    mapping = {c["key"]: c["analyst_key"] for c in cats}
    return mapping.get(cat_key, "lead")


def ask_mock(ctx: CategoryContext, question: str) -> dict:
    """无 LLM 的结构冒烟: 分类问题→选分析师→收集其工具证据→组装答案骨架。

    使用外置提示词(effective_qa_categories / effective_report_roles)决定类别与角色名。
    """
    cats = effective_qa_categories(ctx.cat)
    cat_key = classify_question(question, categories=cats)
    analyst_key = analyst_for_category(cat_key, categories=cats)
    bundle = tool_text_bundle(ctx, analyst_key)
    answer = _assemble_mock_answer(ctx, question, cat_key, analyst_key, bundle, cats)
    return {
        "answer": answer,
        "category": cat_key,
        "label": CATEGORY_LABEL.get(cat_key, cat_key),
        "analyst": analyst_key,
        "tools": list(bundle),
        "source_blocks": list(ctx.overview().get("provenance", [])),
        "mock": True,
    }


def _assemble_mock_answer(ctx, question, cat_key, analyst_key, bundle, categories=None) -> str:
    cats = categories or DEFAULT_QA_CATEGORIES
    label_by_key = {c.get("key"): c.get("label", c.get("key")) for c in cats}
    ov = ctx.overview()
    role = ROLE_BY_KEY.get(analyst_key, {}).get("role", analyst_key)
    lines = [
        f"# {ov['cat']} 问答(结构验证 Mock)",
        "",
        f"> ⚠️ 本回答为 **Mock 冒烟**: 按规则分类选择了分析师并收集其工具证据, "
        "**未执行 LLM 推理**。真实问答请用 `ask`。",
        "",
        f"**问题**: {question}",
        f"**分类**: {label_by_key.get(cat_key, cat_key)} → {role}（{analyst_key}）",
        "",
        "---\n## 依据证据(该分析师可用的工具输出)",
    ]
    if not bundle:
        lines.append("*(该分析师为综合角色, mock 中无新增工具; 实际由 LLM 综合上游结论作答)*")
    else:
        for tname, text in bundle.items():
            lines.append(f"\n### {tname}\n{text}")
    lines.append("\n---\n## 结论(占位)\n*实际答案由 LLM 基于以上证据生成; Mock 仅验证路由与工具集合。*")
    return "\n".join(lines)


# ---------------------------------------------------------------- 真实 crew(懒导入)
def build_qa_crew(ctx: CategoryContext, config, llm=None):
    """构造 crewai 层级进程 `Crew`: 路由 Agent 委派给对应分析师。"""
    llm = llm or build_llm(config)
    try:
        from crewai import Agent, Crew, Process, Task
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构造问答 Crew 需要 crewai, 请先安装: pip install -r agent/requirements.txt"
        ) from exc

    router_role = effective_qa_router(ctx.cat)
    analysts = build_report_agents(ctx, llm, effective_report_roles(ctx.cat))  # 4 名分析师

    router = Agent(
        role=router_role["role"],
        goal=router_role["goal"],
        backstory=router_role["backstory"],
        llm=llm,
        allow_delegation=True,   # 让路由器可委派给分析师
        verbose=True,
    )
    main_task = Task(
        description=(
            "判断用户问题的类别, 委派给对应的分析师, 最后整合成简洁、可引用的中文答案。\n"
            "问题: {question}"
        ),
        expected_output="一段答案 + 关键数字的来源标注。",
        agent=router,
    )
    # 注意: crewai 层级进程需 manager(litellm/路由)。API 会随版本漂移,
    # 此处按常见签名给出; 装 crewai 后按实际版本核对 manager_agent/manager_llm。
    return Crew(
        agents=[router, *analysts],
        tasks=[main_task],
        process=Process.hierarchical,
        manager_agent=router,      # 层级进程的 manager
        memory=True,
        verbose=True,
    )


def ask(config, ctx: CategoryContext, question: str, llm=None) -> dict:
    """构造 + kickoff 问答 Crew(需 crewAI + 真实 LLM key)。"""
    crew = build_qa_crew(ctx, config, llm)
    result = crew.kickoff(inputs={"cat": ctx.cat, "question": question})
    return {
        "answer": result.raw,
        "sources": list(ctx.overview().get("provenance", [])),
        "mock": False,
    }
