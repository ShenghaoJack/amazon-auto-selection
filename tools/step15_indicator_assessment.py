"""Step 15: audit-friendly assessment of the supplied 2026-11-18 indicator list."""
from __future__ import annotations

import json
import re
import math
from decimal import Decimal
import pandas as pd
from core.currency import SITE_CURRENCY

# 业务解释与计算口径分开维护；实际计算和阈值仍以各行结果为准。
BUSINESS_MEANINGS = {
    "核心词搜索量": "反映通过搜索量和CPC筛选的核心词的站内搜索需求；不等于销量或全类目搜索量。",
    "核心词数量": "反映满足当前筛选条件的核心词覆盖广度；并非所有潜在搜索词的数量。",
    "CPC竞价": "反映筛选后核心词的广告点击成本压力；CPC不是订单获客成本，也不能直接推算利润。",
    "平均客单价": "反映样本变体的价格水平；这里是价格算术均值，并非基于订单的真实客单价。",
    "月需求量估算": "反映样本平均每个变体的月销量，不是整个市场的月销量。",
    "类目GMV估算": "反映采集样本的月销额规模，不代表完整类目的市场GMV。",
    "趋势方向（站内）": "反映达标核心词站内搜索需求近三个月的同比变化；缺失数据的词按计算证据处理。",
    "趋势方向（Google）": "反映核心词Google趋势合计指数近三个月的同比变化；指数可能独立归一化，合计不是搜索量。",
    "旺季月份": "反映核心词合计搜索需求相对较高的月份，仅供季节规划参考，不代表实际销量峰值。",
    "淡季月份": "反映核心词合计搜索需求相对较低的月份，仅供季节规划参考，不代表实际销量谷值。",
    "季节波动系数": "反映达标核心词合计搜索需求的季节波动；按各年合计峰谷比中较大值判断，单词波动仅供参考。",
    "头部链接销售额占比": "反映样本前五个变体链接的销额集中程度，不等于父体或全市场集中度。",
    "头部品牌集中度": "反映样本销额向前五个品牌集中的程度，不能直接推断全市场份额。",
    "卖家集中度": "反映样本销额向前五个BuyBox卖家集中的程度，不等同全市场卖家份额。",
    "新卖家占比": "以新品对应的BuyBox卖家数和新品销额占比观察新品切入空间；新品卖家不一定是新注册卖家。",
    "大卖占比": "以评分数超过1000的变体销额占比近似观察成熟链接的竞争压力；不是卖家规模的直接统计。",
    "VC卖家占比": "以AMZ配送方式对应销额作为自营近似口径；并非已核实的VC卖家身份，也不能用于判断卖家所属国家。",
    "本土卖家占比": "观察卖家所属地与站点国家一致的样本销额占比；仅按所属地判断，排除所属地缺失样本，与配送方式无关。",
    "评论壁垒": "观察销量靠前父体的评分数量水平，反映成熟竞品的评价积累；评分数不等于文字评论数。",
    "评分": "反映有效样本变体平均星级，是评价质量的参考；并非按评分数量加权。",
    "老品占比": "反映上架超过1095天的样本变体销额占比，并非全部老品的占比；上架不足或等于1095天的老品不包含在分子。",
    "上架超3年链接销额占比": "反映上架超过1095天的样本变体销额占比，并非全部老品的占比；上架不足或等于1095天的老品不包含在分子。",
}

_METRIC_DISPLAY_ALIAS = {
    "老品占比": "上架超3年链接销额占比",
}


def _display_metric_name(name) -> str:
    s = str(name or "").strip()
    return _METRIC_DISPLAY_ALIAS.get(s, s)


def _normalize_advice_metric_names(text: str) -> str:
    """兜底对齐 AI 输出中的历史指标简称，确保与报告指标明细表一致。"""
    s = str(text or "")
    for old, new in _METRIC_DISPLAY_ALIAS.items():
        if old in s:
            s = s.replace(old, new)
    return s

# Order, original thresholds and remarks are retained for user review.
SPECS = [
 ("P0", "核心词搜索量", "主词月搜索量 < 3,000 否决", "备注要求单词月搜索量 > 30,000；阈值冲突", "conflict"),
 ("P1", "CPC竞价", "CPC > $1.2 且客单价 < $40 否决", "备注要求 CPC < $3；口径冲突", "conflict"),
 ("未标注", "平均客单价", "平均客单价 > $35", "变体价格均值与成交客单价并非同一指标", "price"),
 ("P1", "月需求量估算", "月需求量 < 500 件否决", "备注为平均月销量；总量和均值口径未确认", "conflict"),
 ("P1", "类目GMV估算", "类目GMV < $100,000/月否决", "基于样本变体月销额之和，不能视为全市场完整GMV", "gmv"),
 ("P1", "市场饱和度", "头部10 ASIN销量占比 > 75% 否决", "备注提及 CR5 60%；指标口径冲突", "conflict"),
 ("P0", "趋势方向", "连续3季同比下滑 > 10% 否决", "备注为站内搜索和Google趋势同比 ≤ -20%；口径冲突", "conflict"),
 ("P1", "旺季月份", "单季销量占比 > 70% 降级", "需要连续12个月销量及季度口径", "missing"),
 ("P1", "淡季月份", "淡季/旺季比 < 20% 降级", "需要淡旺季月份及对应销量", "missing"),
 ("P2", "季节波动系数", "季节波动系数 > 2.5 降级", "备注为连续两年峰谷比，统计定义待确认", "conflict"),
 ("P1", "头部链接销量占比", "头部20%链接销量占比 > 70% 降级", "备注建议前五子ASIN销售额；口径冲突", "conflict"),
 ("P0", "卖家总数", "卖家总数 < 8 或 > 200 否决", "备注为前5卖家销售额占比；口径冲突", "conflict"),
 ("P1", "CR5/CR10集中度", "CR5 > 60% 否决", "卖家结构下按卖家销额计算，须完整且可识别的卖家数据", "cr5"),
 ("P1", "新卖家占比/存活率", "新卖家12月存活率 < 30% 否决", "备注为新品卖家数量 > 10 或销额 > 20%；口径冲突", "conflict"),
 ("P1", "评论壁垒", "头部均值评论数 > 5,000 否决", "头部范围和评论数定义未指定", "pending"),
 ("P2", "老品占比", "3年以上老品销量占比 > 70% 降级", "需要完整且可核验的上架天数", "old"),
 ("未标注", "卖家地分布", "本土卖家数 > 20%", "备注询问卖家数还是销售额；口径和结论方向未确认", "conflict"),
 ("未标注", "卖家物流方式分布", "VC卖家数 > 20%", "VC口径与判定方向未确认", "pending"),
 ("未标注", "小类目下退款率", "退款率 > 10%", "需要类目退款率及时间范围", "missing"),
 ("未标注", "平均评分数", "平均评分数 < 4.2分", "按样本变体星级均值展示，不以评分数量代替评分", "rating"),
]

COLUMNS = ["优先级", "指标", "原表标准", "原表备注及口径", "实际值", "判定", "影响", "证据与限制"]

def assess(variants, split_df, currency, keywords=None, settings=None):
    settings = settings or {}
    rows = []
    asin = next((c for c in split_df.columns if str(c).strip().lower() in ("变体asin", "asin")), None)
    ids = split_df[asin].fillna("").astype(str).str.strip().str.upper() if asin is not None else None
    unique_rows = ids is not None and not ids.eq("").any() and not ids.duplicated().any()
    for priority, name, threshold, note, kind in SPECS:
        rule = settings.get(name, {})
        threshold = rule.get("standard", threshold)
        if "limit" in rule:
            limit = float(rule["limit"])
            if name == "类目GMV估算": threshold = f"类目GMV < ${limit:g}/月否决"
            elif name == "老品占比": threshold = f"3年以上老品销量占比 > {limit:g}% 降级"
            elif name == "平均评分数": threshold = f"平均评分数 < {limit:g}分"
        status = "待确认" if kind in ("conflict", "pending") else "数据不足"
        value, impact, evidence = "", "", note
        if name in ("核心词搜索量", "CPC竞价") and keywords is not None and not keywords.empty:
            name_col = next((c for c in keywords.columns if str(c).strip() == "关键词"), None)
            metric = "站内月搜索量" if name == "核心词搜索量" else "CPC"
            value_col = next((c for c in keywords.columns if str(c).strip() == metric), None)
            if name_col is not None and value_col is not None:
                pairs = [(str(row[name_col]).strip(), pd.to_numeric(row[value_col], errors="coerce")) for _, row in keywords.iterrows()]
                pairs = [(n, v) for n, v in pairs if n and math.isfinite(v) and v >= 0]
                value = "；".join(f"{n}: {v:g}" for n, v in pairs[:20])
                if len(pairs) > 20: value += f"；其余{len(pairs)-20}个词见关键词与季节性表"
                evidence = note + "；逐词数值来自步骤8关键词报告"
        if name in ("市场饱和度", "头部链接销量占比") and unique_rows:
            sales_col = next((c for c in split_df.columns if str(c).strip() == "变体月销量"), None)
            if sales_col is not None:
                sales = pd.to_numeric(split_df[sales_col], errors="coerce")
                if sales.notna().all() and (sales >= 0).all() and sales.sum() > 0:
                    n = 10 if name == "市场饱和度" else max(1, math.ceil(len(sales) * .2))
                    value = round(float(sales.nlargest(n).sum() / sales.sum() * 100), 2)
                    evidence = note + "；实际值为步骤14样本销量占比%，并非全市场"
        if kind == "price" and not variants.empty and unique_rows:
            value = round(float(variants["_price"].mean()), 2)
            evidence = f"{currency or '未知币种'}；实际值为步骤14有效变体的价格算术均值，缺少订单金额与订单数，无法判断客单价"
        elif kind == "gmv" and not variants.empty and unique_rows:
            # Missing sales revenue was historically filled with 0 in step 15. Do not use it for this gate.
            candidates = [c for c in split_df.columns if str(c).strip() == "变体月销额"]
            if candidates:
                vals = pd.to_numeric(split_df[candidates[0]].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False), errors="coerce")
                if vals.notna().all() and vals.map(math.isfinite).all() and (vals >= 0).all():
                    total = sum((Decimal(str(v)) for v in vals), Decimal(0))
                    value = round(float(total), 2)
                    evidence = "步骤14全量样本的变体月销额之和；样本GMV不代表全市场GMV；判定使用未四舍五入数值"
                    if currency == "USD" and total >= Decimal(str(rule.get("limit", 100000))): status = "达标"
                    elif currency == "USD": evidence += "；样本小于阈值不能证明全市场低于阈值"
                    else: evidence += "；未换算美元，不能判断"
        elif kind == "cr5" and not variants.empty and unique_rows:
            col = next((c for c in split_df.columns if str(c).strip().lower() in ("buybox卖家", "buybox seller", "购物车卖家")), None)
            rev = next((c for c in split_df.columns if str(c).strip() == "变体月销额"), None)
            if col is not None and rev is not None:
                sellers = split_df[col].fillna("").astype(str).str.strip()
                values = pd.to_numeric(split_df[rev].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False), errors="coerce")
                valid = ~sellers.str.lower().isin(["", "未知", "nan", "none", "null", "-"])
                if valid.all() and values.notna().all() and values.map(math.isfinite).all() and (values >= 0).all() and values.sum() > 0:
                    grouped = pd.DataFrame({"seller": sellers, "revenue": values}).groupby("seller")["revenue"].sum()
                    value = round(float(grouped.nlargest(5).sum() / values.sum() * 100), 2)
                    status = "数据不足"
                    evidence = "步骤14样本按BuyBox卖家汇总变体月销额；占比%，尚未证明覆盖全市场，不能据此判断市场CR5"
        elif kind == "old" and unique_rows:
            age = next((c for c in split_df.columns if str(c).strip() == "上架天数"), None)
            sales = next((c for c in split_df.columns if str(c).strip() == "变体月销量"), None)
            if age is not None and sales is not None:
                a = pd.to_numeric(split_df[age], errors="coerce")
                q = pd.to_numeric(split_df[sales], errors="coerce")
                if a.notna().all() and q.notna().all() and a.map(math.isfinite).all() and q.map(math.isfinite).all() and (a >= 0).all() and (q >= 0).all() and q.sum() > 0:
                    total_sales = sum((Decimal(str(v)) for v in q), Decimal(0))
                    old_sales = sum((Decimal(str(v)) for v in q[a > 365 * 3]), Decimal(0))
                    percent = old_sales / total_sales * Decimal(100)
                    value = round(float(percent), 2)
                    status = "不达标" if percent > Decimal(str(rule.get("limit", 70))) else "达标"
                    evidence = "步骤14全量样本，上架天数超过1095天的变体销量/样本销量，单位%；判定使用未四舍五入数值"
        elif kind == "rating" and unique_rows:
            col = next((c for c in split_df.columns if str(c).strip().lower() in ("评分", "rating")), None)
            if col is not None:
                ratings = pd.to_numeric(split_df[col], errors="coerce")
                if ratings.notna().all() and ratings.between(0, 5).all() and len(ratings):
                    mean_rating = sum((Decimal(str(v)) for v in ratings), Decimal(0)) / Decimal(len(ratings))
                    value = round(float(mean_rating), 2)
                    status = "不达标" if mean_rating < Decimal(str(rule.get("limit", 4.2))) else "达标"
                    evidence = "步骤14样本变体评分算术平均（满分5分），非评论数加权；判定使用未四舍五入数值"
        if status == "不达标":
            impact = "降级" if kind == "old" else ("否决" if kind in ("gmv", "cr5") else "需关注")
        if rule.get("enabled") is False:
            status, impact = "未启用", ""
            evidence += "；此指标已在工作台停用，不参与市场初判"
        rows.append(dict(zip(COLUMNS, [priority, name, threshold, note, value, status, impact, evidence])))
    return rows


def rule_decision(rows):
    passed = sum(r.get("判定") == "达标" for r in rows)
    failed = sum(r.get("判定") == "不达标" for r in rows)
    incomplete = sum(r.get("判定") in ("待确认", "数据不足") for r in rows)
    if not passed + failed:
        return "暂无可判定指标" + (f"；{incomplete} 项待确认或数据不足" if incomplete else "")
    return f"已判定 {passed + failed} 项，其中 {passed} 项达标、{failed} 项不达标" + (f"；{incomplete} 项待确认或数据不足" if incomplete else "")


_DEMAND_METRICS = {
    "核心词搜索量", "核心词数量", "CPC竞价", "平均客单价", "月需求量估算",
    "类目GMV估算", "趋势方向（站内）", "趋势方向（Google）", "旺季月份",
    "淡季月份", "季节波动系数", "趋势方向",
}
_MONOPOLY_METRICS = {
    "头部链接销售额占比", "头部品牌集中度", "卖家集中度", "VC卖家占比",
    "市场饱和度", "头部链接销量占比", "CR5/CR10集中度", "卖家物流方式分布",
}
_BARRIER_METRICS = {
    "大卖占比", "评论壁垒", "老品占比", "上架超3年链接销额占比", "评分", "平均评分数",
}
_WINDOW_METRICS = {"新卖家占比", "新卖家占比/存活率"}
_LOCAL_METRICS = {"本土卖家占比", "卖家地分布"}
_COMP_METRICS = _MONOPOLY_METRICS | _BARRIER_METRICS | _WINDOW_METRICS | _LOCAL_METRICS | {"卖家总数"}
_ADVICE_TIERS = ("可进入", "低风险条件进入", "高风险条件进入", "有条件进入", "暂不建议进入", "数据不足无法判断")

_DEFAULT_LIMITS = {
    "头部链接销售额占比": 60.0,
    "头部品牌集中度": 60.0,
    "卖家集中度": 60.0,
    "VC卖家占比": 20.0,
    "大卖占比": 50.0,
    "评论壁垒": 5000.0,
    "老品占比": 70.0,
    "上架超3年链接销额占比": 70.0,
    "本土卖家占比": 20.0,
    "月需求量估算": 500.0,
    "评分": 4.2,
}


def _row_section(row: dict) -> str:
    sec = str(row.get("板块") or "").strip()
    if sec in ("市场需求", "竞争格局"):
        return sec
    name = str(row.get("指标") or "").strip()
    if name in _DEMAND_METRICS:
        return "市场需求"
    if name in _COMP_METRICS:
        return "竞争格局"
    return "其他"


def _row_num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    return None


def _row_limit(row: dict, name: str) -> float:
    """从行内‘原表标准’解析工作台当前阈值，缺失时回退默认阈值。"""
    import re
    std = str(row.get("原表标准") or "")
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", std.replace(",", ""))
    if m:
        try:
            v = float(m.group(1))
            if v > 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_LIMITS.get(name, 0.0)


def _summarize_assessment(rows: list[dict]) -> dict:
    """按 4 大独立业务维度 × 相对阈值偏离度（轻/中/重）预计算结构化摘要与判定边界参考。

    设计原则：
    - 避免等权数项数与同源指标重复计票（VC/卖家/品牌归入‘垄断与自营控盘’，大卖/评论/老品归入‘评价与冷启动壁垒’）；
    - 本土卖家占比降权为本土身份提醒，不计入垄断/壁垒重度判定；
    - 偏离度自动联动工作台阈值（如默认 VC 阈值 20% 时，≥2.5×即 ≥50% 判为重度控盘）。
    """
    sections = {
        "市场需求": {"达标": 0, "不达标": 0, "待确认或数据不足": 0, "不达标项": []},
        "竞争格局": {"达标": 0, "不达标": 0, "待确认或数据不足": 0, "不达标项": []},
    }
    by_name: dict[str, dict] = {}
    severe_fails: list[str] = []

    monopoly_decided = monopoly_total = monopoly_fail = 0
    monopoly_level = "无明显压力"
    monopoly_notes: list[str] = []

    barrier_decided = barrier_total = barrier_fail = 0
    barrier_level = "无明显压力"
    barrier_notes: list[str] = []
    big_seller_fail = False
    big_seller_val = None
    big_seller_lim = 50.0
    review_fail = False
    review_val = None
    review_lim = 5000.0
    vc_fail = False
    vc_val = None
    vc_lim = 20.0
    concentration_fail = False
    extreme_monopoly = False
    extreme_monopoly_notes: list[str] = []

    local_note = ""

    _RANK = {"无明显压力": 0, "轻度": 1, "中度": 2, "重度": 3}

    def bump(cur: str, nxt: str) -> str:
        return nxt if _RANK[nxt] > _RANK[cur] else cur

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("指标") or "").strip()
        status = str(row.get("判定") or "").strip()
        if name:
            by_name[name] = row
        if status in ("仅展示", "未启用"):
            continue

        sec = _row_section(row)
        bucket = sections.get(sec)
        if bucket is not None:
            if status == "达标":
                bucket["达标"] += 1
            elif status == "不达标":
                bucket["不达标"] += 1
                if name:
                    bucket["不达标项"].append(_display_metric_name(name))
            elif status in ("待确认", "数据不足"):
                bucket["待确认或数据不足"] += 1

        val = _row_num(row.get("实际值"))
        lim = _row_limit(row, name)

        # 维度 B：垄断与自营控盘
        if name in _MONOPOLY_METRICS:
            monopoly_total += 1
            if status in ("达标", "不达标"):
                monopoly_decided += 1
            if status == "不达标":
                monopoly_fail += 1
                monopoly_level = bump(monopoly_level, "轻度")
                if name == "VC卖家占比" and val is not None:
                    vc_fail, vc_val, vc_lim = True, val, (lim or 20.0)
                    extreme_vc = max(70.0, lim * 3.5) if lim > 0 else 70.0
                    heavy_vc = max(50.0, lim * 2.5) if lim > 0 else 50.0
                    mid_vc = max(35.0, lim * 1.75) if lim > 0 else 35.0
                    if val >= heavy_vc:
                        monopoly_level = bump(monopoly_level, "重度")
                        if val >= extreme_vc:
                            extreme_monopoly = True
                            note = f"VC/AMZ近似自营极端控盘({val:.1f}%≥{extreme_vc:g}%)"
                            extreme_monopoly_notes.append(note)
                        else:
                            note = f"VC/AMZ近似自营重度控盘({val:.1f}%≥{heavy_vc:g}%)"
                        monopoly_notes.append(note)
                        severe_fails.append(note)
                    elif val >= mid_vc:
                        monopoly_level = bump(monopoly_level, "中度")
                        monopoly_notes.append(f"VC/AMZ近似自营占比偏高({val:.1f}%≥{mid_vc:g}%)")
                    else:
                        monopoly_notes.append(f"VC/AMZ近似自营略超标({val:.1f}%)")
                elif name in ("头部品牌集中度", "卖家集中度") and val is not None:
                    concentration_fail = True
                    extreme_c = lim + 25.0 if lim > 0 else 85.0
                    heavy_c = lim + 15.0 if lim > 0 else 75.0
                    mid_c = lim + 10.0 if lim > 0 else 70.0
                    if val >= heavy_c:
                        monopoly_level = bump(monopoly_level, "重度")
                        if val >= extreme_c:
                            extreme_monopoly = True
                            note = f"{name}极端垄断({val:.1f}%≥{extreme_c:g}%)"
                            extreme_monopoly_notes.append(note)
                        else:
                            note = f"{name}重度集中({val:.1f}%≥{heavy_c:g}%)"
                        monopoly_notes.append(note)
                        severe_fails.append(note)
                    elif val >= mid_c:
                        monopoly_level = bump(monopoly_level, "中度")
                        monopoly_notes.append(f"{name}中度集中({val:.1f}%)")
                    else:
                        monopoly_notes.append(f"{name}轻度超标({val:.1f}%)")
                elif name == "头部链接销售额占比" and val is not None:
                    extreme_l = lim + 20.0 if lim > 0 else 80.0
                    heavy_l = lim + 10.0 if lim > 0 else 70.0
                    if val >= heavy_l:
                        monopoly_level = bump(monopoly_level, "重度")
                        if val >= extreme_l:
                            extreme_monopoly = True
                            note = f"头部链接销售额占比极端集中({val:.1f}%≥{extreme_l:g}%)"
                            extreme_monopoly_notes.append(note)
                        else:
                            note = f"头部链接销售额占比高度集中({val:.1f}%≥{heavy_l:g}%)"
                        monopoly_notes.append(note)
                        severe_fails.append(note)
                    else:
                        monopoly_notes.append(f"头部链接销售额占比偏高({val:.1f}%)")

        # 维度 C：评价与成熟冷启动壁垒
        elif name in _BARRIER_METRICS:
            barrier_total += 1
            if status in ("达标", "不达标"):
                barrier_decided += 1
            if status == "不达标":
                barrier_fail += 1
                barrier_level = bump(barrier_level, "轻度")
                if name == "评论壁垒" and val is not None:
                    review_fail, review_val, review_lim = True, val, (lim or 5000.0)
                    extreme_r = review_lim * 4.0
                    heavy_r = review_lim * 2.0
                    mid_r = review_lim * 1.3
                    if val >= heavy_r:
                        barrier_level = bump(barrier_level, "重度")
                        if val >= extreme_r:
                            note = f"评论壁垒极端高企({int(round(val))}条≥{int(round(extreme_r))})"
                        else:
                            note = f"评论壁垒重度高企({int(round(val))}条≥{int(round(heavy_r))})"
                        barrier_notes.append(note)
                        severe_fails.append(note)
                    elif val >= mid_r:
                        barrier_level = bump(barrier_level, "中度")
                        note = f"评论壁垒高({int(round(val))}条≥{int(round(mid_r))})"
                        barrier_notes.append(note)
                        if val >= review_lim * 1.6:
                            severe_fails.append(note)
                    else:
                        barrier_notes.append(f"评论壁垒略超标({int(round(val))}条)")
                elif name == "大卖占比" and val is not None:
                    big_seller_fail, big_seller_val, big_seller_lim = True, val, (lim or 50.0)
                    if val >= big_seller_lim + 25.0:
                        barrier_level = bump(barrier_level, "重度")
                        note = f"大卖占比极高({val:.1f}%≥{big_seller_lim + 25.0:g}%)"
                        barrier_notes.append(note)
                        severe_fails.append(note)
                    elif val >= big_seller_lim + 15.0:
                        barrier_level = bump(barrier_level, "中度")
                        note = f"大卖占比偏高({val:.1f}%≥{big_seller_lim + 15.0:g}%)"
                        barrier_notes.append(note)
                        severe_fails.append(note)
                    else:
                        barrier_notes.append(f"大卖占比略超标({val:.1f}%)")
                elif name in ("老品占比", "上架超3年链接销额占比") and val is not None:
                    if val >= (lim or 70.0) + 10.0:
                        barrier_level = bump(barrier_level, "中度")
                        barrier_notes.append(f"上架超3年链接销额占比偏高({val:.1f}%)")
                    else:
                        barrier_notes.append(f"上架超3年链接销额占比略超标({val:.1f}%)")
                elif name in ("评分", "平均评分数") and val is not None:
                    barrier_notes.append(f"类目均分偏低({val:.2f}分)")

        # 本土卖家占比：辅助身份提醒（不计入垄断/壁垒重度）
        elif name in _LOCAL_METRICS and status == "不达标":
            if val is not None:
                local_note = f"本土卖家占比{val:.1f}%超标（本土同行已较多，本土身份不再具备稀缺优势，属辅助提醒）"
            else:
                local_note = "本土卖家占比超标（本土身份不再具备稀缺优势，属辅助提醒）"

        # 维度 A 突出不达标项记录
        elif sec == "市场需求" and status == "不达标":
            if name == "月需求量估算" and val is not None and val < (lim or 500.0) * 0.6:
                severe_fails.append(f"单变体月销量明显偏薄({val:.0f}件/变体/月<{(lim or 500.0) * 0.6:g})")
            elif name == "核心词数量":
                severe_fails.append("达标核心词数量不足")
            elif name == "类目GMV估算":
                severe_fails.append("样本类目GMV不达标")
            elif name in ("趋势方向（站内）", "趋势方向（Google）") and val is not None:
                severe_fails.append(f"{name}显著下滑({val:+.1f}%)")

    # 同维叠加修正：
    # 1) 垄断维 ≥2 项不达标至少为中度；若 VC≥40%(≥2×阈值) 且叠加品牌/卖家集中度不达标，升为重度控盘
    if monopoly_fail >= 2:
        monopoly_level = bump(monopoly_level, "中度")
    if vc_fail and concentration_fail and vc_val is not None:
        vc_combo_heavy = max(40.0, vc_lim * 2.0)
        if vc_val >= vc_combo_heavy and monopoly_level != "重度":
            monopoly_level = bump(monopoly_level, "重度")
            combo_note = f"VC/AMZ占比高({vc_val:.1f}%≥{vc_combo_heavy:g}%)叠加头部集中度超标构成重度控盘"
            if combo_note not in monopoly_notes:
                monopoly_notes.append(combo_note)
            if combo_note not in severe_fails:
                severe_fails.append(combo_note)
    # 2) 大卖偏高(≥+15pct)叠加高评论(≥1.6×阈值)升为重度壁垒
    if big_seller_fail and review_fail:
        barrier_level = bump(barrier_level, "中度")
        if (
            big_seller_val is not None
            and review_val is not None
            and big_seller_val >= big_seller_lim + 15.0
            and review_val >= review_lim * 1.6
        ):
            barrier_level = bump(barrier_level, "重度")

    d = sections["市场需求"]
    c = sections["竞争格局"]
    d_decided = d["达标"] + d["不达标"]
    c_decided = c["达标"] + c["不达标"]
    d_total = d_decided + d["待确认或数据不足"]
    c_total = c_decided + c["待确认或数据不足"]
    total_decided = d_decided + c_decided
    total_judgeable = d_total + c_total

    # 维度 A 评级
    demand_core_fail = (
        (d_decided > 0 and d["不达标"] * 3 >= d_decided and d["不达标"] >= 3)
        or (by_name.get("核心词数量", {}).get("判定") == "不达标")
        or (by_name.get("类目GMV估算", {}).get("判定") == "不达标")
        or (
            by_name.get("趋势方向（站内）", {}).get("判定") == "不达标"
            and by_name.get("趋势方向（Google）", {}).get("判定") == "不达标"
        )
    )
    if d_decided == 0 or (d_total >= 3 and d_decided < 3):
        demand_level = "数据不足"
    elif demand_core_fail:
        demand_level = "不成立"
    elif d["不达标"] > 0:
        demand_level = "偏弱"
    else:
        demand_level = "健康"

    # 维度 D（新品窗口）评级：若新品卖家数达标但新品销额占比 < 15%，标记为「弱窗口」（新卖家多但抢不到销量）
    win_row = by_name.get("新卖家占比") or by_name.get("新卖家占比/存活率") or {}
    win_status = str(win_row.get("判定") or "").strip()
    win_rev_share = None
    m_share = re.search(r"新品销额占比\s*([0-9]+(?:\.[0-9]+)?)\s*%", str(win_row.get("实际值") or ""))
    if m_share:
        try:
            win_rev_share = float(m_share.group(1))
        except ValueError:
            win_rev_share = None
    if win_status == "达标":
        window_level = "弱窗口" if (win_rev_share is not None and win_rev_share < 15.0) else "有窗口"
    elif win_status == "不达标":
        window_level = "无窗口"
    else:
        window_level = "未知"

    extreme_review_barrier = bool(
        review_fail and review_val is not None and review_val >= review_lim * 4.0
    )

    # 五档判定边界（完全基于 A/B/C/D 四维度，不机械数总项数）
    reasons: list[str] = []
    if (
        total_judgeable == 0
        or total_decided < total_judgeable * 0.5
        or demand_level == "数据不足"
        or (monopoly_total >= 2 and monopoly_decided == 0)
        or (barrier_total >= 2 and barrier_decided == 0)
        or (c_total >= 3 and c_decided < 3)
    ):
        tier = "数据不足无法判断"
        reasons.append(f"有效已判定指标不足({total_decided}/{total_judgeable})")
    elif demand_level == "不成立":
        tier = "暂不建议进入"
        reasons.append(f"维度A需求底盘不成立(不达标项: {'、'.join(d['不达标项'])})")
    elif monopoly_level == "重度" and barrier_level == "重度":
        tier = "暂不建议进入"
        reasons.append(
            f"维度B垄断/自营重度控盘({'；'.join(monopoly_notes[:3])}) 叠加 维度C评价壁垒重度({'；'.join(barrier_notes[:2])})"
        )
    elif extreme_review_barrier and monopoly_level in ("中度", "重度"):
        tier = "暂不建议进入"
        reasons.append(
            f"评论壁垒极端高企({int(round(review_val))}条≥4×阈值) 叠加 维度B垄断/自营{monopoly_level}控盘({'；'.join(monopoly_notes[:2])})"
        )
    elif extreme_monopoly and (barrier_level in ("中度", "重度") or window_level in ("弱窗口", "无窗口")):
        tier = "暂不建议进入"
        reasons.append(
            f"维度B极端控盘/垄断({'；'.join(extreme_monopoly_notes[:2])}) 叠加 维度C评价壁垒={barrier_level}/维度D={window_level}"
        )
    elif (monopoly_level == "重度" or barrier_level == "重度") and window_level == "无窗口":
        tier = "暂不建议进入"
        reasons.append(f"存在重度壁垒(垄断={monopoly_level}/评价={barrier_level})且维度D缺乏新品切入窗口")
    elif (
        window_level == "弱窗口"
        and (
            (monopoly_level == "重度" and barrier_level == "中度")
            or (barrier_level == "重度" and monopoly_level == "中度")
        )
    ):
        tier = "暂不建议进入"
        reasons.append(
            f"存在重度+中度叠加壁垒(垄断={monopoly_level}/评价={barrier_level})且维度D仅为弱新品窗口(新品销额占比{win_rev_share:.1f}%<15%)"
        )
    elif (
        demand_level in ("健康", "偏弱")
        and d["不达标"] <= 1
        and by_name.get("趋势方向（站内）", {}).get("判定") != "不达标"
        and monopoly_level in ("无明显压力", "轻度")
        and barrier_level in ("无明显压力", "轻度")
        and window_level != "无窗口"
    ):
        tier = "可进入"
        reasons.append(
            f"维度A需求{demand_level}({d['达标']}/{d_decided})，维度B垄断({monopoly_level})与维度C壁垒({barrier_level})均可控且存在新品窗口"
        )
    elif extreme_monopoly or extreme_review_barrier or monopoly_level == "重度" or barrier_level == "重度":
        tier = "高风险条件进入"
        if extreme_monopoly:
            reasons.append(
                f"维度B极端控盘/垄断({'；'.join(extreme_monopoly_notes[:2])})，虽维度C={barrier_level}且维度D={window_level}未触发双维封锁，但仍属高风险博弈，须人工核查新品窗口是否由第三方新卖家而非头部大牌/自营贡献"
            )
        elif extreme_review_barrier:
            reasons.append(
                f"维度C评论壁垒极端高企({int(round(review_val))}条≥4×阈值)，虽维度B={monopoly_level}且维度D={window_level}，仍属高风险冷启动，须核查差异化切入与价格带空间"
            )
        else:
            reasons.append(
                f"存在单维重度压力(维度B垄断={monopoly_level}/维度C壁垒={barrier_level})，虽另一维可控且维度D={window_level}，仍属高风险条件进入，须人工核查渠道/BuyBox/伪新品窗口风险"
            )
    else:
        tier = "低风险条件进入"
        reasons.append(
            f"维度A需求{demand_level}({d['达标']}/{d_decided})，维度B垄断({monopoly_level})/维度C壁垒({barrier_level})存在中度或局部压力但无重度/极端封锁，维度D={window_level}"
        )

    dimensions = {
        "A_需求底盘": {"评级": demand_level, "达标数": f"{d['达标']}/{d_decided}", "不达标项": d["不达标项"]},
        "B_垄断与自营控盘": {"评级": monopoly_level, "是否极端": extreme_monopoly, "说明": monopoly_notes},
        "C_评价与冷启动壁垒": {"评级": barrier_level, "是否极端": extreme_review_barrier, "说明": barrier_notes},
        "D_新品窗口与本土身份": {
            "新品窗口": window_level,
            "新品信号": str(win_row.get("实际值") or "").strip(),
            "本土身份提醒": local_note or "本土卖家占比达标或未触发提醒",
        },
    }

    return {
        "分板块统计": sections,
        "四维度评估": dimensions,
        "突出不达标项": severe_fails,
        "判定边界参考": {"参考档位": tier, "触发依据": reasons},
    }


def summarize_four_dimensions(rows) -> dict:
    """生成可直接用于步骤15报告/PDF展示的确定性四维度判定拆解。"""
    summary = _summarize_assessment(rows)
    dims = summary["四维度评估"]
    ref = summary["判定边界参考"]

    a = dims["A_需求底盘"]
    a_grade = f"{a['评级']}（{a['达标数']}）" if a.get("达标数") and a["评级"] != "数据不足" else a["评级"]
    if a["不达标项"]:
        a_ev = f"不达标项：{'、'.join(a['不达标项'])}"
    elif a["评级"] == "数据不足":
        a_ev = "需求端有效判定指标不足"
    else:
        a_ev = "核心词、客单价、需求量、GMV 与趋势指标均达标"

    b = dims["B_垄断与自营控盘"]
    b_grade = "极端" if b.get("是否极端") else b["评级"]
    b_ev = "；".join(b["说明"]) if b.get("说明") else "头部链接/品牌/卖家集中度与 VC/AMZ 占比均可控"

    c = dims["C_评价与冷启动壁垒"]
    c_grade = "极端" if c.get("是否极端") else c["评级"]
    c_ev = "；".join(c["说明"]) if c.get("说明") else "评论壁垒、大卖占比、老品占比与评分均可控"

    d = dims["D_新品窗口与本土身份"]
    d_parts = []
    if d.get("新品信号"):
        d_parts.append(d["新品信号"])
    elif d["新品窗口"] == "未知":
        d_parts.append("新品窗口数据待确认")
    if d.get("本土身份提醒"):
        d_parts.append(d["本土身份提醒"])
    d_ev = "；".join(d_parts) if d_parts else "新品切入窗口可控"

    return {
        "参考档位": ref["参考档位"],
        "触发依据": ref["触发依据"],
        "维度明细": [
            {"维度": "A. 需求底盘", "评级": a_grade, "核心证据": a_ev},
            {"维度": "B. 垄断与自营控盘", "评级": b_grade, "核心证据": b_ev},
            {"维度": "C. 评价与冷启动壁垒", "评级": c_grade, "核心证据": c_ev},
            {"维度": "D. 新品窗口与本土身份", "评级": d["新品窗口"], "核心证据": d_ev},
        ],
    }


def build_advice_prompt(ctx, rows):
    config = getattr(ctx, "config", {})
    def setting(name):
        return str(config.get(name, "") or "").strip() if config is not None else ""
    site = setting("SITE")
    context = {
        "平台": "亚马逊", "类目": setting("CAT"), "站点": site,
        "站点币种": SITE_CURRENCY.get(site, "未确认"),
        "流量数据周期": setting("TRAFFIC_MONTH"), "竞品数据周期": setting("COMPETITOR_MONTH"),
        "配置趋势月份": setting("TREND_MONTH"), "卖家身份": "本土卖家",
    }
    enriched_rows = []
    for row in rows:
        item = {k: v for k, v in row.items() if k not in ("影响", "优先级")}
        raw_name = row.get("指标")
        disp_name = _display_metric_name(raw_name)
        item["指标"] = disp_name
        item["业务含义与解读边界"] = (
            BUSINESS_MEANINGS.get(disp_name)
            or BUSINESS_MEANINGS.get(raw_name)
            or "未提供业务定义，按实际口径和证据解释，勿自行补造。"
        )
        enriched_rows.append(item)
    summary = _summarize_assessment(rows)
    return ("你是严谨的亚马逊类目研究分析师，为当前站点的本土卖家输出稳定、边界清晰、不可和稀泥的市场进入评估。"
              "依据提供的研究前提识别站点、币种和周期；未提供的信息标记未知，不得猜测。配置趋势月份不一定是实际数据月份，以指标证据为准。"
              "只使用以下已计算指标与证据，明确区分达标、不达标、待确认和数据不足；"
              "不要使用否决、降级等已停用影响等级或机械地以单项不达标下结论。"
              "冲突未解决和数据不足不可视为达标，不得补造数据或利润。"
              "步骤14指标均为采集样本，不能说是完整市场；缺失字段已从对应指标中排除，须说明有效样本覆盖及其对结论的影响。"
              "用户为本土卖家：本土卖家占比只按卖家所属地判断，不以AMZ配送方式推断国别；本土卖家占比超标意味着本土同行已较多、本土身份不再具备稀缺优势（属辅助竞争提醒，权重低于自营控盘与评价壁垒）。"
              "Google趋势按各词指数相加口径判定，须注明各词可能独立归一化，合计指数不等于实际搜索量。"
              "季节性以达标核心词的逐月搜索量合计计算两年峰谷比并判断；单词峰谷比只作细分参考，不得据此推翻整体判定。"
              "不得依据显示取整值改写指标判定。金额统一使用明确的ISO币种代码（例如 USD 2.48），金额较大时用 M 或 B，不得换算汇率。"
              "【指标名称严格对齐】输出中引用任何指标时，必须严格使用下方「指标」数组中的完整指标名称（例如使用‘上架超3年链接销额占比’而非‘老品占比’，使用‘头部链接销售额占比’而非‘头部链接占比’，使用‘新卖家占比’而非‘新卖家窗口’），确保正文与报告指标明细表逐字一致。"
              "【四维度×五档结论判定边界（按独立业务维度而非机械数项数判定，禁止将双维重度封锁类目放宽为‘低风险条件进入’或‘高风险条件进入’）】："
              "四维度=A需求底盘（健康/偏弱/不成立）、B垄断与自营控盘（含头部链接销售额占比/头部品牌集中度/卖家集中度与VC卖家占比；VC卖家占比≥50%或≥2.5×阈值，或VC≥40%叠加品牌/卖家集中度超标均属重度控盘；品牌/卖家集中度≥阈值+15pct即重度集中；VC≥70%或品牌/卖家集中度≥85%或头部链接≥80%属极端控盘/垄断）、"
              "C评价与冷启动壁垒（含大卖占比/评论壁垒/上架超3年链接销额占比/评分；评论壁垒≥2×阈值或大卖占比偏高叠加高评论壁垒即重度壁垒，评论壁垒≥4×阈值属极端壁垒）、D新品窗口（新卖家占比为主信号，若新品卖家数达标但新品销额占比<15%属弱窗口；本土卖家占比为辅助提醒）。"
              "1) 数据不足无法判断：有效已判定指标不足50%，或任一核心维度缺少有效判定；"
              "2) 暂不建议进入：满足任一即归入本档——①A需求底盘不成立；②B垄断/自营控盘与C评价壁垒双双为重度；③评论壁垒极端高企(≥4×阈值)叠加B达中度及以上，或B极端控盘/垄断叠加C达中度及以上(或D为弱/无窗口)；④B或C任一为重度且D无新品窗口，或重度+中度叠加且D仅为弱窗口(新品销额占比<15%)；"
              "3) 可进入：A健康或仅1项次要需求指标偏弱且站内趋势达标、B与C均不超过轻度、且D不缺新品窗口；"
              "4) 低风险条件进入：常规中间态——需求成立、B与C均未达到重度或极端（最高为中度压力），需结合差异化/供应链条件进入；"
              "5) 高风险条件进入：高门槛强警示中间态——需求成立且未触发‘暂不建议进入’，但B或C存在单维重度或单维极端控盘/壁垒压力（仅靠另一维可控与新品窗口保留空间）；须明确提示人工复核‘新品窗口是否由第三方新卖家而非头部大牌/自营贡献’及价格带/BuyBox/专利壁垒。"
              "原则上遵循数据中‘判定边界参考’给出的参考档位；若因指标间存在压倒性抵消确需调整一档，必须在建议中明确给出定量理由。"
              "【输出格式与稳定篇幅硬性要求】："
              "建议：必须严格以‘可进入：’‘低风险条件进入：’‘高风险条件进入：’‘暂不建议进入：’或‘数据不足无法判断：’之一开头，紧跟一段80-140字完整判断，"
              "依次包含：①按四维度串联判定原因（A需求底盘/B垄断控盘/C评价壁垒/D新品窗口定级）及分板块达标概览（需求端X/Y达标、竞争端A/B达标）；②决定该档位的2-4个最关键指标及实际值；③明确行动边界"
              "（可进入→首选切入方向；低风险条件进入→必须同时满足的1-2个常规差异化/供应链前提；高风险条件进入→核心高风险压制点及必须先人工核实的关键前提，如排除大牌/自营造成的伪新品窗口；暂不建议进入→核心阻断因素及仅在何种极高门槛下才值得复评；数据不足→缺失关键项）。"
              "严禁只输出孤立的结论标签（如仅写‘低风险条件进入’或‘高风险条件进入’），严禁超过180字。"
              "主要依据：严格输出2条，第1条以‘市场需求：’开头、第2条以‘竞争格局：’开头，每条90-160字；先交代本板块达标数，再综合核心达标项与不达标项的证据与矛盾点；"
              "数据中列出的全部‘突出不达标项’必须在对应板块中逐一正面提及，严禁遗漏（尤其是VC/AMZ近似自营占比、品牌/卖家集中度、评论壁垒等）。"
              "主要风险：仅从市场需求与竞争格局两方面按严重度从高到低输出3-5条（确实无风险时可少于3条或空数组），每条45-90字，"
              "格式为‘风险标题：指标实际值与标准对比 + 对本土卖家进入/冷启动/BuyBox的直接影响’；全部‘突出不达标项’必须覆盖，严禁遗漏；"
              "不将盈利能力未确认或样本覆盖不足单独列为一条风险，没有证据不编造。"
              "待验证：输出3-5条，每条35-75字，紧扣前述核心阻断因素或放行前提，给出具体可核查对象，不写冗长操作计划或空泛套话。"
              "输出JSON：{\"建议\":\"结论标签：...\",\"主要依据\":[\"市场需求：...\",\"竞争格局：...\"],"
              "\"主要风险\":[\"...\"],\"待验证\":[\"...\"]}；风险不足以识别时返回空数组。\n"
              + json.dumps({
                  "研究前提": context,
                  "指标概况": rule_decision(rows),
                  "分板块统计": summary["分板块统计"],
                  "四维度评估": summary["四维度评估"],
                  "突出不达标项": summary["突出不达标项"],
                  "判定边界参考": summary["判定边界参考"],
                  "指标": enriched_rows,
              }, ensure_ascii=False, allow_nan=False))


def advice(ctx, rows):
    decision = rule_decision(rows)
    if ctx.dry_run:
        return [{"项目": "指标概况", "内容": decision}, {"项目": "AI建议", "内容": "dry-run 未调用模型"}]
    prompt = build_advice_prompt(ctx, rows)
    for attempt in range(2):
        try:
            result = ctx.llm().chat_json(prompt, temperature=0.0, max_tokens=2200)
            if not isinstance(result, dict) or not isinstance(result.get("建议"), str) or not result["建议"].strip():
                raise ValueError("AI 返回缺少建议")
            advice_text = result["建议"].strip()
            bare_check = advice_text.strip("。：:！! \t\n")
            if bare_check in _ADVICE_TIERS:
                prompt += "\n请重新回答：‘建议’字段严禁只输出结论标签，必须按‘结论标签：80-140字完整判断（分板块达标概览+核心指标证据+明确行动边界）’格式输出。"
                raise ValueError("AI 建议仅含结论标签，缺少理由说明")
            if not isinstance(result.get("主要风险"), list) or not all(isinstance(x, str) for x in result["主要风险"]):
                raise ValueError("AI 返回缺少主要风险列表或格式错误")
            content = json.dumps({key: result.get(key) for key in ("建议", "主要依据", "主要风险", "待验证")}, ensure_ascii=False)
            if any(label in content for label in ("否决", "降级")):
                prompt += "\n请重新回答，禁止输出已停用的影响等级；仅讨论达标情况、证据和市场进入建议。"
                raise ValueError("AI 建议包含已停用的影响等级")
            out = [{"项目": "指标概况", "内容": decision}, {"项目": "AI建议", "内容": _normalize_advice_metric_names(advice_text)}]
            for key in ("主要依据", "主要风险", "待验证"):
                values = result.get(key)
                if isinstance(values, list):
                    out.extend(
                        {"项目": key, "内容": _normalize_advice_metric_names(str(x))}
                        for x in values if str(x).strip()
                    )
            return out
        except Exception as exc:
            ctx.log.warning("步骤15 AI建议第%d次生成失败：%s%s", attempt + 1, type(exc).__name__, "，准备重试" if attempt == 0 else "，已使用兜底提示")
    return [{"项目": "指标概况", "内容": decision}, {"项目": "AI建议", "内容": "AI建议暂不可用，已保留指标计算和判定结果；请检查模型后重跑步骤15"}]
