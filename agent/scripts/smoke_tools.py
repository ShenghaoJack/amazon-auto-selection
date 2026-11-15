"""Phase 2 冒烟: 验证 GroundedTool(纯函数)在真实类目上取回紧凑摘要。

用法:
    python -m agent.scripts.smoke_tools [data/<CAT>/config.yaml]

无网络、无 LLM、无 crewAI(纯函数); 仅读取产物并打印各工具返回的文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.category_context import CategoryContext  # noqa: E402
from agent.tools.artifact_tools import (  # noqa: E402
    get_attribute_frequency_text,
    get_brand_ranking_text,
    get_category_overview_text,
    get_gates_text,
    get_keyword_table_text,
    get_kpis_text,
    get_monitoring_text,
    get_qualified_keywords_text,
    get_variant_table_text,
)


def main(config_path: str) -> int:
    ctx = CategoryContext(config_path)
    print("=" * 60)
    print(f"类目: {ctx.describe()}")
    print("=" * 60)

    print("\n[get_category_overview]\n" + get_category_overview_text(ctx))
    print("\n[get_kpis]\n" + get_kpis_text(ctx))
    print("\n[get_gates]\n" + get_gates_text(ctx))
    print("\n[get_brand_ranking]\n" + get_brand_ranking_text(ctx))
    print("\n[get_keyword_table]\n" + get_keyword_table_text(ctx, 8))
    print("\n[get_qualified_keywords]\n" + get_qualified_keywords_text(ctx))
    print("\n[get_variant_table]\n" + get_variant_table_text(ctx, 6))
    print("\n[get_attribute_frequency]\n" + get_attribute_frequency_text(ctx))
    print("\n[get_monitoring]\n" + get_monitoring_text(ctx))
    print("\n-- provenance --")
    print(f"   {ctx._provenance}")
    return 0


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "data/salt-pepper-shaker-sets/config.yaml"
    raise SystemExit(main(cfg))
