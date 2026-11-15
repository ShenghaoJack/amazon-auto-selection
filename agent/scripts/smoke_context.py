"""Phase 1 冒烟: 用真实类目产物验证 CategoryContext 只读加载。

用法:
    python -m agent.scripts.smoke_context [data/<CAT>/config.yaml]

无网络、无 LLM 调用; 仅读取已存在的产物并打印规范化结果。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.category_context import CategoryContext  # noqa: E402


def _fmt_rows(df, cols, n=15):
    """按候选列名取前 n 行, 转紧凑文本; 去 NaN。"""
    if df is None or df.empty:
        return "  （无数据）"
    from agent.context.columns import find_col
    present = [find_col(df, [c]) for c in cols]
    use = [c for c in present if c]
    if not use:
        return "  （无匹配列）"
    sub = df[use].head(n)
    return sub.to_string(index=False)


def main(config_path: str) -> int:
    print(f"== 加载配置: {config_path}")
    ctx = CategoryContext(config_path)
    ov = ctx.overview()
    print(f"== 概览: {ctx.describe()}")
    print(f"   site={ov['site']} market={ov['market']} month={ov['traffic_month']}")
    print(f"   可用数据块({len(ov['available'])}): {ov['available']}")

    print("\n-- 核心KPI --")
    print(_fmt_rows(ctx.kpi_rows, ["项目", "数值"]))
    print("\n-- 类目门禁 --")
    print(_fmt_rows(ctx.gate_rows, ["项目", "数值", "判定"]))
    print("\n-- 品牌销额Top10 --")
    print(_fmt_rows(ctx.brand_rank_rows, ["品牌", "变体月销量", "变体月销额", "销额占比%"], n=10))

    print("\n-- 达标关键词 --")
    qk = ctx.qualified_keywords
    print("  ", ", ".join(qk) if qk else "  无")

    print("\n-- 关键词 Top(核心词分析) --")
    print(_fmt_rows(ctx.keywords, ["关键词", "站内月搜索量", "达标判定"], n=10))

    print("\n-- 属性分布(前几列) --")
    for item in ctx.attribute_freq:
        print(f"   {item['attr']}: {item['top']}")

    print("\n-- 监控/步骤状态 --")
    mon = ctx.monitoring
    done = [k for k, v in mon["step_status"].items() if v == "DONE"]
    print(f"   步骤 DONE: {sorted(done)}")
    print(f"   告警条数: {len(mon['alerts'])}")
    print(f"   health keys: {list(mon['health'].keys()) if mon['health'] else '无'}")

    print("\n-- provenance(已接触数据块) --")
    print(f"   {ctx._provenance}")
    return 0


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "data/salt-pepper-shaker-sets/config.yaml"
    raise SystemExit(main(cfg))
