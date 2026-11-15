"""Phase 4 冒烟: 报告 Crew 的**结构装配 + Mock 冒烟**(无需 crewAI、无 LLM)。

用法:
    python -m agent.scripts.smoke_report [data/<CAT>/config.yaml]

只读产物; 按任务顺序调用各 agent 的工具, 收集证据并组装标注 Mock 的报告骨架。
同时打印角色/工具/任务顺序, 用于核对结构。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.category_context import CategoryContext  # noqa: E402
from agent.crews.agents import REPORT_ROLES, REPORT_TASKS  # noqa: E402
from agent.crews.report_crew import run_summary_mock  # noqa: E402


def main(config_path: str) -> int:
    ctx = CategoryContext(config_path)
    print("=" * 60)
    print(f"类目: {ctx.describe()}")
    print("=" * 60)

    print("\n-- Agent 角色(顺序) --")
    for r in REPORT_ROLES:
        print(f"  {r['key']:<10} {r['role']:<12} 工具: {r['tool_names'] or '(综合)'}")

    print("\n-- 任务链(顺序) --")
    for i, t in enumerate(REPORT_TASKS, 1):
        print(f"  {i}. {t['agent_key']:<10} -> {t['expected_output'][:40]}")

    out = run_summary_mock(ctx)
    print("\n" + "=" * 60)
    print(f"Mock 报告(长度 {len(out['report'])} 字符, mock={out['mock']})")
    print("=" * 60)
    print(out["report"])
    print("\n-- sources(可引用数据块) --")
    print(f"   {out['sources']}")
    return 0


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "data/salt-pepper-shaker-sets/config.yaml"
    raise SystemExit(main(cfg))
