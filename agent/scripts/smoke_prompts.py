"""提示词外置冒烟: 验证从 YAML 读取/覆盖默认值。

用法:
    python -m agent.scripts.smoke_prompts [data/<CAT>/config.yaml]

演示:
    1) 当前生效的角色/任务/路由/类别/工具说明(来自 agent/prompts.yaml + 默认)
    2) 字段级覆盖: 临时给 lead 角色改一句 goal, 确认只有该字段被覆盖
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tempfile  # noqa: E402

from agent.crews.prompt_config import (  # noqa: E402
    effective_qa_categories,
    effective_qa_router,
    effective_report_roles,
    effective_report_tasks,
    effective_tool_descriptions,
    load_prompts,
)


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "salt-pepper-shaker-sets"

    print(f"== 类目: {cat}")
    roles = effective_report_roles(cat)
    print("\n-- report_roles(顺序) --")
    for r in roles:
        print(f"  {r['key']:<10} {r['role']} | goal={r['goal'][:30]} | tools={len(r['tool_names'])}")

    print("\n-- report_tasks(顺序) --")
    for t in effective_report_tasks(cat):
        print(f"  {t['agent_key']:<10} | {t['expected_output'][:40]}")

    print("\n-- qa_router --")
    print("  ", effective_qa_router(cat))

    print("\n-- qa_categories --")
    for c in effective_qa_categories(cat):
        print(f"  {c['key']:<8} → {c['analyst_key']}")

    print("\n-- tool_descriptions(在当前 YAML 中) --")
    print("  ", effective_tool_descriptions(cat) or "(空, 使用代码 docstring)")

    # ---- 字段级覆盖演示: 用临时 YAML 只改 lead 的 goal ----
    print("\n== 字段级覆盖演示(临时 YAML) ==")
    sample = """report_roles:
  - key: lead
    goal: 只给出最终结论, 控制在100字内, 用中文。
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(sample); tmp = fh.name
    over_roles = effective_report_roles(cat, global_path=tmp)
    lead = next(r for r in over_roles if r["key"] == "lead")
    print(f"  lead.goal        = {lead['goal']}")
    print(f"  lead.backstory   = {lead['backstory'][:30]}...(保留默认, 未被覆盖)")
    print(f"  lead.tool_names  = {lead['tool_names']}(保留默认)")
    Path(tmp).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
