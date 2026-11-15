# agent 层: GroundedTool 数据访问器(防幻觉)。
# 只读产物, 回传紧凑摘要并记录 provenance。
#
# - `get_*_text(ctx)`           纯函数(零依赖, 可直接单测)
# - `build_tools(ctx)`          返回 crewAI `@tool` 列表(需 crewAI, 懒导入)
from .artifact_tools import (
    build_tools,
    get_category_overview_text,
    get_kpis_text,
    get_gates_text,
    get_brand_ranking_text,
    get_keyword_table_text,
    get_qualified_keywords_text,
    get_variant_table_text,
    get_attribute_frequency_text,
    get_monitoring_text,
)

__all__ = [
    "build_tools",
    "get_category_overview_text",
    "get_kpis_text",
    "get_gates_text",
    "get_brand_ranking_text",
    "get_keyword_table_text",
    "get_qualified_keywords_text",
    "get_variant_table_text",
    "get_attribute_frequency_text",
    "get_monitoring_text",
]
