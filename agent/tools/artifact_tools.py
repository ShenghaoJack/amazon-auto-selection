"""GroundedTool: 把 `CategoryContext` 的数据包成**紧凑文本**供 agent 使用。

这是 Phase 2。核心原则: agent 只能通过这里的工具取数, 拿到的是**摘要文本**
而非原始大数据表, 从而:
- 控制喂给 LLM 的 token 成本;
- 让 agent 只解释、不编造数字;
- 每个工具读取时都会触发 `CategoryContext._touch`, 记录 provenance,
  供服务层生成 sources 引用(回答自带来源)。

**依赖策略**: 
- `get_*_text(ctx)` 是**纯函数**(不下发 crewAI), 可直接单测, 零依赖;
- `build_tools(ctx)` 才需要 crewAI(懒导入), 返回 crewai `@tool` 列表。
  未装 crewAI 时 `build_tools` 抛出清晰的安装提示, 而纯函数照常可用。

⚠️ 本层只读产物, 不触碰 `steps/*`、`core/orchestrator.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.context.category_context import CategoryContext
from agent.context.columns import find_col

if TYPE_CHECKING:  # 仅类型标注用, 运行时懒导入
    from crewai.tools import tool


# ---------------------------------------------------------------- 格式化辅助
def _fmt_rows(df, cols, n: int = 15) -> str:
    """按候选列名取前 n 行, 转紧凑文本(不含索引); 缺失列跳过。"""
    if df is None or df.empty:
        return "暂无数据"
    use = [find_col(df, [c]) for c in cols]
    use = [c for c in use if c]
    if not use:
        return "无匹配列"
    return df[use].head(n).to_string(index=False)


def _fmt_list(items) -> str:
    if not items:
        return "无"
    return ", ".join(str(x) for x in items)


# ---------------------------------------------------------------- 纯函数(可测)
def get_category_overview_text(ctx: CategoryContext) -> str:
    ov = ctx.overview()
    return (
        f"类目: {ov['cat']}\n站点: {ov['site']}  市场: {ov['market']}\n"
        f"数据月份: {ov['traffic_month'] or '-'}  (竞品月: {ov['competitor_month'] or '-'})\n"
        f"可用数据块({len(ov['available'])}): {_fmt_list(ov['available'])}\n"
        f"是否离线模拟: {ov['dry_run']}"
    )


def get_kpis_text(ctx: CategoryContext) -> str:
    """核心KPI: 变体总数/总月销量/总月销额/价格/品牌数/配送/新品。"""
    df = ctx.kpi_rows
    if df is None or df.empty:
        return "暂无核心KPI(可能类目报告未生成)"
    rows = []
    b_col = find_col(df, ["项目"])
    v_col = find_col(df, ["数值"])
    if b_col is None:
        return "核心KPI表无可读列"
    for _, r in df.iterrows():
        item = str(r.get(b_col, "")).strip()
        val = r.get(v_col) if v_col is not None else ""
        rows.append(f"- {item}: {val}")
    return "\n".join(rows)


def get_gates_text(ctx: CategoryContext) -> str:
    """四类目门禁判定(达标/不达标/数据不足)。"""
    df = ctx.gate_rows
    if df is None or df.empty:
        return "暂无门禁(可能类目报告未生成)"
    return _fmt_rows(df, ["项目", "数值", "判定"], n=10)


def get_brand_ranking_text(ctx: CategoryContext) -> str:
    """品牌销额Top10(月销量/月销额/占比)。"""
    df = ctx.brand_rank_rows
    if df is None or df.empty:
        return "暂无品牌排行"
    return _fmt_rows(df, ["项目", "品牌", "变体月销量", "变体月销额", "销额占比%"], n=10)


def get_keyword_table_text(ctx: CategoryContext, limit: int = 15) -> str:
    """关键词表(搜索量/同比/CPC/达标判定)。limit 控制取样条数。"""
    df = ctx.keywords
    if df is None or df.empty:
        return "暂无关键词数据"
    return _fmt_rows(df, ["关键词", "站内月搜索量", "站内同比%", "Google同比%", "CPC", "达标判定"],
                     n=min(int(limit), 20))


def get_qualified_keywords_text(ctx: CategoryContext) -> str:
    """判定为'达标'的关键词清单。"""
    return _fmt_list(ctx.qualified_keywords) or "无达标关键词"


def get_variant_table_text(ctx: CategoryContext, limit: int = 10) -> str:
    """变体明细(取自步骤14流量拆分·AI已补充: 含价格/品牌/月销量/月销额)。"""
    df = ctx.split
    if df is None or df.empty:
        return "暂无变体明细"
    return _fmt_rows(df, ["变体ASIN", "品牌", "价格", "变体月销量", "变体月销额", "新老品"],
                     n=min(int(limit), 15))


def get_attribute_frequency_text(ctx: CategoryContext) -> str:
    """属性标注里各属性列值的分布(找差异化卖点/共性)。"""
    items = ctx.attribute_freq
    if not items:
        return "暂无属性分布(可能属性表未生成)"
    lines = []
    for it in items:
        top = it.get("top", {})
        lines.append(f"- {it['attr']}: " + (", ".join(f"{k}={v}" for k, v in top.items()) or "无"))
    return "\n".join(lines)


def get_monitoring_text(ctx: CategoryContext) -> str:
    """数据成熟度: 各步骤状态 + 告警 + 体检(说明数据是否可靠/完整)。"""
    mon = ctx.monitoring
    st = mon.get("step_status", {})
    by = {}
    for v in st.values():
        by[v] = by.get(v, 0) + 1
    remaining = [k for k, v in st.items() if v not in ("DONE", "SKIP")]
    alerts = mon.get("alerts", []) or []
    health = mon.get("health", {}) or {}
    lines = [
        f"步骤状态分布: {by}",
        f"未完成步骤: {_fmt_list(remaining) if remaining else '无(0-19 全完成)'}",
        f"告警条数: {len(alerts)}",
    ]
    if alerts:
        lines.append("最近告警:")
        lines += [f"  - {a}" for a in alerts[-5:]]
    if health:
        lines.append(f"体检: exit_code={health.get('exit_code')} checked_at={health.get('checked_at')}")
    return "\n".join(lines)


# 显式声明所有纯函数(便于 build_tools 与测试引用)
_TEXT_FUNCS = {
    "get_category_overview": get_category_overview_text,
    "get_kpis": get_kpis_text,
    "get_gates": get_gates_text,
    "get_brand_ranking": get_brand_ranking_text,
    "get_keyword_table": get_keyword_table_text,
    "get_qualified_keywords": get_qualified_keywords_text,
    "get_variant_table": get_variant_table_text,
    "get_attribute_frequency": get_attribute_frequency_text,
    "get_monitoring": get_monitoring_text,
}


# ---------------------------------------------------------------- crewAI 包装
def build_tools(ctx: CategoryContext, tool_descriptions: dict | None = None):
    """返回 crewai `@tool` 列表(供 Agent tools=[...])。

    `tool_descriptions`: 可选的 {工具名: 描述}, 覆盖各工具默认 docstring
    (供 `agent/prompts.yaml` 的 `tool_descriptions` 外置化使用)。

    需要 crewAI; 未安装则抛出带安装提示的 RuntimeError。
    """
    try:
        from crewai.tools import tool as crewai_tool
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构建 agent 工具需要 crewai, 请先安装: pip install -r agent/requirements.txt"
        ) from exc

    desc = tool_descriptions or {}

    def _make(name: str, fn, doc: str):
        """把纯函数包装成 crewai tool; 若提供覆盖描述则改 docstring。"""
        fn.__name__ = name
        if desc.get(name):
            fn.__doc__ = desc[name]
        else:
            fn.__doc__ = doc
        return crewai_tool(name)(fn)

    def _ov():
        return get_category_overview_text(ctx)

    def _kpi():
        return get_kpis_text(ctx)

    def _gates():
        return get_gates_text(ctx)

    def _brand():
        return get_brand_ranking_text(ctx)

    def _kw(limit: int = 15):
        return get_keyword_table_text(ctx, limit)

    def _qual():
        return get_qualified_keywords_text(ctx)

    def _var(limit: int = 10):
        return get_variant_table_text(ctx, limit)

    def _attr():
        return get_attribute_frequency_text(ctx)

    def _mon():
        return get_monitoring_text(ctx)

    return [
        _make("get_category_overview", _ov,
              "返回该类目基本概况(类目/站点/数据月份/可用数据块)。"),
        _make("get_kpis", _kpi,
              "返回核心KPI(变体总数/总月销量/总月销额/价格均值与中位数/品牌数/配送/新品数)。"),
        _make("get_gates", _gates,
              "返回四个类目门禁的判定结果(达标/不达标/数据不足)。"),
        _make("get_brand_ranking", _brand,
              "返回品牌销额Top10(月销量/月销额/销额占比)。"),
        _make("get_keyword_table", _kw,
              "返回关键词表(搜索量/同比/CPC/达标判定), limit 控制取样条数(<=20)。"),
        _make("get_qualified_keywords", _qual,
              "返回判定为'达标'的关键词清单。"),
        _make("get_variant_table", _var,
              "返回变体明细(价格/品牌/月销量/月销额), limit 控制取样条数(<=15)。"),
        _make("get_attribute_frequency", _attr,
              "返回属性标注里各属性列的值分布(找差异化卖点)。"),
        _make("get_monitoring", _mon,
              "返回数据成熟度: 各步骤状态与告警(说明数据是否可靠/完整)。"),
    ]
