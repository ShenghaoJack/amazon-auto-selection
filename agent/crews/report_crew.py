"""报告 Crew(顺序进程) — Phase 4。

- `run_summary_mock(ctx)`: **无 LLM、无 crewAI** 的结构冒烟。按任务顺序调用各
  agent 的工具收集"证据", 拼出一份标注 Mock 的报告骨架, 用于验证工具接线与顺序。
- `build_report_crew(ctx, config, llm=None)`: 懒导入 crewAI 构造真实 `Crew`。
- `run_summary(config, ctx, llm=None)`: 构造 + `kickoff` 真跑(需 crewAI + 真实 LLM key)。

⚠️ 本层只读产物, 不触碰 `steps/*`、`core/orchestrator.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.context.category_context import CategoryContext
from agent.crews.agents import build_report_agents, mock_run_gather
from agent.crews.prompt_config import effective_report_roles, effective_report_tasks
from agent.crews.llm_factory import build_llm

if TYPE_CHECKING:
    from agent._vendor import Config


# ---------------------------------------------------------------- Mock 冒烟(纯函数)
def run_summary_mock(ctx: CategoryContext) -> dict:
    """无 LLM 的结构冒烟: 收集各 agent 工具证据, 组装报告骨架。

    返回 {"report", "sources", "steps", "mock": True}。
    报告的每个关键数字都来自真实产物, 但**不包含 LLM 推理**——仅验证工具与顺序。
    """
    roles = effective_report_roles(ctx.cat)
    tasks = effective_report_tasks(ctx.cat)
    steps = mock_run_gather(ctx, tasks, roles)
    report = _assemble_mock_report(ctx, steps, roles)
    return {
        "report": report,
        "sources": list(ctx.overview().get("provenance", [])),
        "steps": steps,
        "mock": True,
    }


def _assemble_mock_report(ctx: CategoryContext, steps: list[dict], roles: list[dict] | None = None) -> str:
    _roles = roles or effective_report_roles(ctx.cat)
    ov = ctx.overview()
    lines: list[str] = [
        f"# {ov['cat']} 选品分析报告(结构验证 Mock)",
        "",
        f"> ⚠️ 本报告为 **Mock 冒烟**: 仅按任务顺序收集了各 agent 的工具证据, "
        "**未执行 LLM 推理**。用于验证工具接线与执行顺序。真实运行请用 `run_summary`。",
        "",
        f"**类目**: {ov['cat']} | **站点**: {ov['site']} | **市场**: {ov['market']} | **月份**: {ov['traffic_month'] or '-'}",
        f"**可用数据块({len(ov['available'])}):** {', '.join(ov['available'])}",
        "",
        "---",
    ]
    # 先按角色顺序展示每个 agent 的证据(即使某角色在任务里不直接产出)
    for r in _roles:
        key = r["key"]
        role = r["role"]
        # 汇总该角色的工具输出(去重, 跨任务同工具只收一次)
        gathered: dict[str, str] = {}
        for st in steps:
            if st["agent_key"] == key:
                for tname, text in st["tools"].items():
                    gathered.setdefault(tname, text)
        lines.append(f"## {role}（{key}）")
        if not gathered:
            lines.append("*(此角色在 mock 中无新增工具; 负责综合上下游)*")
        for tname, text in gathered.items():
            lines.append(f"### {tname}")
            lines.append(text)
        lines.append("")
    lines.append("---\n## 结论(占位)\n*实际结论由 LLM 综合生成; Mock 仅收集证据。*")
    return "\n".join(lines)


# ---------------------------------------------------------------- 真实 crew(懒导入)
def build_report_crew(ctx: CategoryContext, config, llm=None):
    """构造 crewai 顺序进程 `Crew`(研究员→关键词→市场→类目负责人)。"""
    llm = llm or build_llm(config)
    try:
        from crewai import Crew, Process, Task
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构造报告 Crew 需要 crewai, 请先安装: pip install -r agent/requirements.txt"
        ) from exc

    roles = effective_report_roles(ctx.cat)
    tasks = effective_report_tasks(ctx.cat)
    agents = build_report_agents(ctx, llm, roles)          # 顺序与 roles 一致
    agent_by_key = {r["key"]: a for r, a in zip(roles, agents)}
    tasks = [
        Task(
            description=t["description"],
            expected_output=t["expected_output"],
            agent=agent_by_key[t["agent_key"]],
        )
        for t in tasks
    ]
    return Crew(
        agents=agents,
        tasks=tasks,
        process=Process.sequential,
        memory=True,
        verbose=True,
    )


def run_summary(config, ctx: CategoryContext, llm=None) -> dict:
    """构造 + kickoff 报告 Crew。需 crewAI + 真实 LLM key。"""
    crew = build_report_crew(ctx, config, llm)
    result = crew.kickoff(inputs={"cat": ctx.cat})
    return {
        "report": result.raw,
        "sources": list(ctx.overview().get("provenance", [])),
        "mock": False,
    }
