"""Phase 5 冒烟: 问答 Crew 的**路由分类 + Mock 冒烟**(无需 crewAI、无 LLM)。

用法:
    python -m agent.scripts.smoke_qa [data/<CAT>/config.yaml] ["问题"]

无网络、无 LLM、无 crewAI; 用规则分类 + 工具证据组装标注 Mock 的答案骨架。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.category_context import CategoryContext  # noqa: E402
from agent.crews.qa_crew import (  # noqa: E402
    QA_CATEGORIES,
    ask_mock,
    classify_question,
)


def main(config_path: str, question: str) -> int:
    ctx = CategoryContext(config_path)
    print("=" * 60)
    print(f"类目: {ctx.describe()}")
    print("=" * 60)

    print("\n-- 问题类别(规则分类) --")
    for c in QA_CATEGORIES:
        print(f"  {c['key']:<9} {c['label']:<8} → {c['analyst_key']}")

    print("\n-- 逐个问题的路由(规则) --")
    demo = [
        "这个类目的月销量和价格中位数是多少?",
        "有哪些达标关键词? 搜索量最高的是哪个?",
        "Top5 品牌集中度如何? 门禁①达标吗?",
        "这个类目值不值得做? 有什么机会和风险?",
    ]
    for q in demo:
        print(f"  [{classify_question(q):<8}] {q}")

    print("\n" + "=" * 60)
    print(f"提问: {question}")
    out = ask_mock(ctx, question)
    print(f"分类: {out['label']} → {out['analyst']} | 工具: {out['tools']}")
    print("=" * 60)
    print(out["answer"])
    return 0


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "data/salt-pepper-shaker-sets/config.yaml"
    q = sys.argv[2] if len(sys.argv) > 2 else "这个类目的核心指标怎么样?"
    raise SystemExit(main(cfg, q))
