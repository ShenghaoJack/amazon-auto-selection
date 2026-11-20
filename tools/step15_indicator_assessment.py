"""Step 15: audit-friendly assessment of the supplied 2026-11-18 indicator list."""
from __future__ import annotations

import json
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
}

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
        item["业务含义与解读边界"] = BUSINESS_MEANINGS.get(row.get("指标"), "未提供业务定义，按实际口径和证据解释，勿自行补造。")
        enriched_rows.append(item)
    return ("你是亚马逊类目研究分析师。依据提供的研究前提识别站点、币种和周期；未提供的信息标记未知，不得猜测。配置趋势月份不一定是实际数据月份，以指标证据为准。"
              "用户是本土卖家。只使用以下已计算指标与证据，明确区分达标、不达标、待确认和数据不足；"
              "不要使用否决、降级等影响等级或机械地以单项不达标阻止进入市场。"
              "冲突未解决和数据不足不可视为达标，不得补造数据或利润。"
              "步骤14指标均为采集样本，不能说是完整市场。缺失字段已从对应指标中排除，应说明有效样本覆盖及其对结论的影响；"
              "用户为本土卖家，本土卖家占比只按卖家所属地判断，不以AMZ配送方式推断国别；"
              "Google趋势按用户选定的各词指数相加口径判定；须注明各词可能独立归一化，合计指数不等于实际搜索量。"
              "季节性以达标核心词的逐月搜索量合计计算两年峰谷比并判断；单个词的峰谷比只作细分参考，不得据此推翻整体判定。"
              "不得依据显示取整值改写指标判定。"
              "金额统一使用明确的ISO币种代码，例如 USD 2.48；金额较大时用 M 或 B，不能推测币种或换算汇率。"
              "核心任务是判断当前站点的本土卖家是否值得进入该类目，不要写空泛套话。"
              "建议：开头明确给出‘可进入’‘有条件进入’‘暂不建议进入’或‘数据不足无法判断’之一，"
              "接着简明说明综合判断的主要原因；有条件进入须说明最关键条件，不能确定时直接说明缺少什么。"
              "主要依据：分别以‘市场需求：’和‘竞争格局：’开头各写一条，综合相关指标说明支持或不支持进入的原因、"
              "关键利弊及其对最终判断的影响；引用最相关的实际指标和判定，解释相互矛盾的证据，不能只堆砌数字或逐项复述。"
              "主要风险：仅从市场需求和竞争格局两方面分点分析与进入决定直接相关的风险，"
              "每点写清具体风险、相关指标证据和对进入的影响；不将盈利能力未确认或样本覆盖不足单列为风险，没有证据不编造。"
              "待验证：紧扣前述建议、依据和风险，简要列出有助决定能否进入的关键验证事项，不写冗长操作计划。"
              "输出JSON：{\"建议\":\"...\",\"主要依据\":[\"市场需求：...\",\"竞争格局：...\"],"
              "\"主要风险\":[\"...\"],\"待验证\":[\"...\"]}；风险不足以识别时返回空数组。\n"
              + json.dumps({"研究前提": context, "指标概况": rule_decision(rows), "指标": enriched_rows}, ensure_ascii=False, allow_nan=False))


def advice(ctx, rows):
    decision = rule_decision(rows)
    if ctx.dry_run:
        return [{"项目": "指标概况", "内容": decision}, {"项目": "AI建议", "内容": "dry-run 未调用模型"}]
    prompt = build_advice_prompt(ctx, rows)
    for attempt in range(2):
        try:
            result = ctx.llm().chat_json(prompt, temperature=0.0, max_tokens=1800)
            if not isinstance(result, dict) or not isinstance(result.get("建议"), str) or not result["建议"].strip():
                raise ValueError("AI 返回缺少建议")
            if not isinstance(result.get("主要风险"), list) or not all(isinstance(x, str) for x in result["主要风险"]):
                raise ValueError("AI 返回缺少主要风险列表或格式错误")
            content = json.dumps({key: result.get(key) for key in ("建议", "主要依据", "主要风险", "待验证")}, ensure_ascii=False)
            if any(label in content for label in ("否决", "降级")):
                prompt += "\n请重新回答，禁止输出已停用的影响等级；仅讨论达标情况、证据和市场进入建议。"
                raise ValueError("AI 建议包含已停用的影响等级")
            out = [{"项目": "指标概况", "内容": decision}, {"项目": "AI建议", "内容": result["建议"].strip()}]
            for key in ("主要依据", "主要风险", "待验证"):
                values = result.get(key)
                if isinstance(values, list): out.extend({"项目": key, "内容": str(x)} for x in values if str(x).strip())
            return out
        except Exception as exc:
            ctx.log.warning("步骤15 AI建议第%d次生成失败：%s%s", attempt + 1, type(exc).__name__, "，准备重试" if attempt == 0 else "，已使用兜底提示")
    return [{"项目": "指标概况", "内容": decision}, {"项目": "AI建议", "内容": "AI建议暂不可用，已保留指标计算和判定结果；请检查模型后重跑步骤15"}]
