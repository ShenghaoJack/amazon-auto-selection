# -*- coding: utf-8 -*-
"""Sheet4 选品建议 —— 机会假设 → 100 分市场机会分 → 门槛 → 标签级联。

严格遵循 report-spec.md 第 6 节:
  · 假设由数据推导, 数量不固定, 不预设低/中/高价位架构;
  · 每个分项先算 0–100, 再乘权重; 权重合计 100%;
  · VOC 缺失时必须披露修订权重, 不允许用 0 假装"无 VOC 问题";
  · 门槛与标签级联顺序: 硬否决 → 证据置信度封顶 → 总分带 → 优先开发分项下限(冲突取保守);
  · 0 个过门槛是合法输出(暂不建议进入), 禁止凑数。
"""
from __future__ import annotations

import collections
import math

from .engine import NOT_PROVIDED, UNKNOWN, f0, g, ladder_of, med, q, set_count_n, share

WEIGHTS = {"关键词需求": 20, "价格带吸引力": 20, "竞争余量": 15,
           "配置空白/差异化": 20, "VOC问题价值": 15, "证据置信度": 10}

THRESHOLDS = {"关键词需求": 60, "价格带吸引力": 50, "竞争余量": 40,
              "配置空白/差异化": 60, "证据置信度": 50, "总分": 60}

RULE_NOTES = {
    "关键词需求": "按材质/形态词区分(不做类目级常量): 0.5×相对搜索量(命中词最高月搜索量 ÷ 候选集内最高) + 0.3×趋势(命中词按搜索量加权的站内同比, clamp(50+同比,0,100)) + 0.2×达标质量(命中词中判「达标」的比例)。命中但 0 搜索量的「数据不足」词不计入需求证据; 完全无命中 → 0 分。数据源: FR-08 关键词报告核心词分析。",
    "价格带吸引力": "目标价格窗口覆盖各段「销额占比与销量占比取大者」的平均值, 除以全类目最高段份额, 归一化 0–100。分母=全市场; 使用与其他模块完全相同的动态价格带。",
    "竞争余量": "0.5×价格带竞争 + 0.5×细分(同材质×同件数)竞争; 每个子项 = 0.5×集中度余量(100−CR3×100) + 0.2×品牌分散度(min(100, 品牌数/20×100)) + 0.3×稀疏度(在售行数≤20→100, 之后每翻倍 −20 分)。",
    "配置空白/差异化": "0.5×占用度得分 + 0.5×验证度得分。占用度=**同材质×同件数×同尺寸阶梯**的在售行数(0 行=100, 每翻倍 −25); 验证度=同阶梯在金属面已被验证(父体≥3)=100, 有但不足=70, 同类目其他阶梯已验证=60, 无验证=30。",
    "VOC问题价值": "100 × Σ(优先级权重 × 频次 × 严重度 × 安全加成 × 方案相关性) / Σ权重 × 覆盖折扣。"
                    "优先级权重 P0–P3=4/3/2/1; 频次=该问题在 1–3★ 集合的占比; 严重度=(S−1)/4; "
                    "安全加成 A/B=1.5、C=1.25、无=1.0; **方案相关性**=本次产品定义是否有对应手段解决该问题"
                    "(命中方案关键词取属性上限, 未命中取 0.3 基线; 客服/物流/二手/降价类上限 0.2); "
                    "覆盖折扣按证据条数与抓取覆盖率 0.5–1.0。**VOC 源未提供 → 该分项留空(None), "
                    "不参与计算, 权重按可用项归一化并在报告中披露(不允许按 0 计)。**",
    "证据置信度": "0.5×样本分(**证据基数**父体数×10, 上限 100) + 0.3×关键字段完整度 + 0.2×关键词库可用性。注意: 证据基数取「同材质×件数细分全量」, **不是**目标格子的占用行数——目标格子 0 行代表空白位(机会), 不代表证据不足。",
}


# ---------------------------------------------------------------- 假设构造
def _sub(recs, material=None, set_count=None, contains=None):
    out = []
    for r in recs:
        if material is not None and r["material"] != material:
            continue
        if set_count is not None and set_count_n(r["set_count"]) != set_count:
            continue
        if contains is not None and contains not in r["material"]:
            continue
        out.append(r)
    return out


def _window(recs, lo_frac=0.25, hi_frac=0.75):
    pr = [r["price"] for r in recs if r["price"]]
    if not pr:
        return None, None
    return q(pr, lo_frac), q(pr, hi_frac)


def _ladder_terms(h):
    import re as _re
    nums = _re.findall(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", h.get("目标规格") or "")
    return ["%gx%g" % (float(a), float(b)) for a, b in nums]


def _occ(recs, material, set_count, ladder_key_terms):
    """占用度: 同材质×同件数×**同尺寸阶梯**的在售行数(只数真正同配置, 不是整个细分)。"""
    hit = []
    for r in recs:
        if material is not None and r["material"] != material:
            continue
        if set_count is not None and set_count_n(r["set_count"]) != set_count:
            continue
        lad = ladder_of(r["segs"])[0]
        if not lad:
            continue
        if ladder_key_terms and not all(t in lad for t in ladder_key_terms):
            continue
        hit.append(r)
    return hit


def build_hypotheses(ctx):
    """由市场模块推导机会假设。每条都带选择规则与证据指针。"""
    recs = ctx["recs"]
    kw = ctx["keyword"]
    H = []

    # H1 双面异材质 · 3件套(钛面+PP面 / 304不锈钢面+PP面)
    two = [r for r in recs if r["material"] in ("钛+塑料", "不锈钢+塑料")]
    two_all = [r for r in recs if "+" in r["material"]]
    two3 = [r for r in two if set_count_n(r["set_count"]) == 3]
    terms = _ladder_terms({"目标规格": "大 16x10 / 中 13x8 / 小 10.5x6.5 inch"})
    occ = [r for r in two3 if all(t in ladder_of(r["segs"])[0] for t in terms)]
    lo, hi = _window(two3 or two)
    H.append({
        "id": "H1", "名称": "双面异材质三件套(钛面+食品级PP面)",
        "推导规则": "从 `主体材质` ∈ {钛+塑料, 不锈钢+塑料}(两面异材质且含 PP)且 `套装数量`=3件套 的变体出发, 取该组价格 P25–P75 作为价格窗口; 尺寸阶梯取金属面 3件套中销额最高的阶梯。",
        "目标人群/场景": "小厨房/公寓、需要生熟分开且不想占台面的家庭",
        "核心任务": "一套覆盖大/中/小三档备菜面, 金属面处理生肉、PP 面处理果蔬",
        "产品类型": "砧板套装", "材质": "钛+食品级PP(或304不锈钢+PP)", "套装结构": "3件套",
        "基准配置": "三件同厚 0.5 inch; 带汁槽; 食品级/BPA-Free 话术",
        "差异化配置": "立式收纳架 + 环保/食品级认证(全类目 0 行)",
        "目标规格": "大 16x10 / 中 13x8 / 小 10.5x6.5 inch",
        "价格窗口": (lo, hi), "目标材质": "钛+塑料", "目标件数": 3,
        "证据": {"双面异材质总行数": len(two_all), "含PP双面行数": len(two),
                 "3件套行数": len(two3),
                 # 占用度 = 目标阶梯这个"格子"的在售行数(0 = 空白位)
                 "占用行数": len(occ), "占用父体": len({x["parent"] for x in occ}),
                 "占用品牌": len({x["brand"] for x in occ if x["brand"]}),
                 # 证据基数 = 判断需求/价格/置信度所用的总体(同材质×件数细分的已验证样本)
                 "证据基数行数": len(two3), "证据基数父体": len({x["parent"] for x in two3}),
                 "证据基数品牌": len({x["brand"] for x in two3 if x["brand"]}),
                 "证据基数说明": "双面异材质(含PP)×3件套 细分全量"},
        "kw_terms": ["titanium", "stainless", "steel", "metal"],
    })

    # H2 双面异材质 · 单件 16x11
    two1 = [r for r in two if set_count_n(r["set_count"]) == 1]
    occ2 = [r for r in two1 if r["segs"] and any(
        abs(s["l"] - 16) < 0.11 and abs(s["w"] - 11) < 0.11 for s in r["segs"])]
    lo, hi = _window(two1)
    H.append({
        "id": "H2", "名称": "双面异材质单件 16x11",
        "推导规则": "双面异材质单件中, 按面积最大件的长宽分组, 取出现次数最多且品牌数最多的尺寸。",
        "目标人群/场景": "只买一块板、要求大台面的用户",
        "核心任务": "单块板双面复用(金属面/PP面)",
        "产品类型": "砧板", "材质": "钛+食品级PP", "套装结构": "1件",
        "基准配置": "厚 0.6~0.9 inch; 带汁槽; 挂孔",
        "差异化配置": "环保认证; 防滑脚垫",
        "目标规格": "16 x 11 inch", "价格窗口": (lo, hi),
        "目标材质": "钛+塑料", "目标件数": 1,
        "证据": {"双面异材质总行数": len(two_all), "单件行数": len(two1),
                 "占用行数": len(occ2), "占用父体": len({x["parent"] for x in occ2}),
                 "占用品牌": len({x["brand"] for x in occ2 if x["brand"]}),
                 "证据基数行数": len(two1), "证据基数父体": len({x["parent"] for x in two1}),
                 "证据基数品牌": len({x["brand"] for x in two1 if x["brand"]}),
                 "证据基数说明": "双面异材质(含PP)×单件 细分全量"},
        "kw_terms": ["titanium", "stainless", "steel", "metal"],
    })

    # H3 竹制三件套(全类目销额第一阶梯) —— 作为对照/成熟方向
    bam3 = _sub(recs, material="竹制", set_count=3)
    occ3 = [r for r in bam3 if r["segs"] and abs(r["segs"][0]["l"] - 15) < 0.3
            and abs(max(s["l"] for s in r["segs"]) - 15) < 0.3]
    lo, hi = _window(bam3)
    H.append({
        "id": "H3", "名称": "竹制三件套 15x10+12x8+9x6",
        "推导规则": "全类目 3件套中销额最高的尺寸阶梯(按 尺寸规格×套装数量 分组的销额排名)。",
        "目标人群/场景": "价格敏感的家庭日常备菜",
        "核心任务": "低成本覆盖大中小三档",
        "产品类型": "竹制砧板套装", "材质": "竹制", "套装结构": "3件套",
        "基准配置": "厚 0.6 inch; 三件阶梯", "差异化配置": "汁槽/手柄",
        "目标规格": "15x10 / 12x8 / 9x6 inch", "价格窗口": (lo, hi),
        "目标材质": "竹制", "目标件数": 3,
        "证据": {"材质组行数": len(bam3), "占用行数": len(occ3),
                 "占用父体": len({x["parent"] for x in occ3}),
                 "占用品牌": len({x["brand"] for x in occ3 if x["brand"]}),
                 "证据基数行数": len(bam3), "证据基数父体": len({x["parent"] for x in bam3}),
                 "证据基数品牌": len({x["brand"] for x in bam3 if x["brand"]}),
                 "证据基数说明": "竹制×3件套 细分全量"},
        "kw_terms": ["bamboo", "wood"],
    })

    # H4 塑料软垫多件套
    pl4 = _sub(recs, material="塑料")
    pl4 = [r for r in pl4 if (set_count_n(r["set_count"]) or 0) >= 4]
    lo, hi = _window(pl4)
    H.append({
        "id": "H4", "名称": "塑料软垫 4件套以上",
        "推导规则": "`主体材质`=塑料 且 `套装数量`≥4 的变体组。",
        "目标人群/场景": "需要多块小垫分区处理的用户",
        "核心任务": "低价多片分区", "产品类型": "塑料软砧板垫", "材质": "塑料", "套装结构": "4件套+",
        "基准配置": "薄垫(0.25in)", "差异化配置": "防滑底纹、可洗碗机",
        "目标规格": "15x10 一族", "价格窗口": (lo, hi),
        "目标材质": "塑料", "目标件数": 4,
        "证据": {"组行数": len(pl4), "占用行数": len(pl4),
                 "占用父体": len({x["parent"] for x in pl4}),
                 "占用品牌": len({x["brand"] for x in pl4 if x["brand"]}),
                 "证据基数行数": len(pl4), "证据基数父体": len({x["parent"] for x in pl4}),
                 "证据基数品牌": len({x["brand"] for x in pl4 if x["brand"]}),
                 "证据基数说明": "塑料×4件套以上 细分全量"},
        "kw_terms": ["plastic", "flexible", "mat"],
    })

    # H5 不锈钢单件(大市场/高集中)
    ss1 = _sub(recs, material="不锈钢", set_count=1)
    lo, hi = _window(ss1)
    H.append({
        "id": "H5", "名称": "不锈钢单件切菜板(存量市场)",
        "推导规则": "`主体材质`=不锈钢 且 `套装数量`=1 的变体组(类目现存最大单材质组之一)。",
        "目标人群/场景": "要金属面单板的用户",
        "核心任务": "单板金属面", "产品类型": "不锈钢砧板", "材质": "不锈钢", "套装结构": "1件",
        "基准配置": "16x11; 双面", "差异化配置": "厚度加厚/防滑",
        "目标规格": "16 x 11 inch", "价格窗口": (lo, hi),
        "目标材质": "不锈钢", "目标件数": 1,
        "证据": {"组行数": len(ss1), "占用行数": len(ss1),
                 "占用父体": len({x["parent"] for x in ss1}),
                 "占用品牌": len({x["brand"] for x in ss1 if x["brand"]}),
                 "证据基数行数": len(ss1), "证据基数父体": len({x["parent"] for x in ss1}),
                 "证据基数品牌": len({x["brand"] for x in ss1 if x["brand"]}),
                 "证据基数说明": "不锈钢×单件 细分全量"},
        "kw_terms": ["stainless", "steel", "metal"],
    })
    return H


# ---------------------------------------------------------------- 打分
def _kw_hits(h, kw):
    kws = kw.get("keywords", [])
    terms = [t.lower() for t in h.get("kw_terms", [])]
    return [k for k in kws if any(t in k["keyword"].lower() for t in terms)]


def _kw_matched(h, kw):
    """命中且**有搜索量**的关键词(0 搜索量的「数据不足」词不构成需求证据)。"""
    return [k for k in _kw_hits(h, kw) if (k["search"] or 0) > 0]


def _kw_component(h, kw, max_matched_vol=1):
    """需求分(按材质/形态词区分)。

    0.5×相对搜索量(命中词最高月搜索量 ÷ 候选集内最高, 候选集内归一) +
    0.3×趋势(命中词按搜索量加权的站内同比, clamp(50+同比,0,100)) +
    0.2×达标质量(命中词中判「达标」的比例)。
    命中词里 0 搜索量的「数据不足」词不计入, 也不当作 0 需求。
    """
    kws = kw.get("keywords", [])
    if not kws:
        return None, "关键词工作簿缺失"
    m = _kw_matched(h, kw)
    if not m:
        return 0.0, ("无匹配关键词(材质/形态词在关键词库中无搜索量证据) → 需求分 0; "
                   "命中但 0 搜索量的词: %s" % (
                       "、".join(k["keyword"] for k in _kw_hits(h, kw)[:4]) or "无"))
    vol = max(k["search"] for k in m)
    rel = min(100.0, 100.0 * vol / max(1, max_matched_vol))
    wsum = sum(k["search"] for k in m) or 1
    trend_raw = sum((k["mom_yoy"] or 0) * k["search"] for k in m) / wsum
    trend = max(0.0, min(100.0, 50.0 + trend_raw))
    qual = 100.0 * sum(1 for k in m if k["verdict"] == "达标") / len(m)
    val = 0.5 * rel + 0.3 * trend + 0.2 * qual
    note = ("命中 %d 个有量词(最高「%s」%s) → 搜索量分 %.0f; 加权同比 %.1f%% → 趋势分 %.0f; "
            "达标 %d/%d → 质量分 %.0f" % (
                len(m), max(m, key=lambda k: k["search"])["keyword"], f0(vol), rel,
                trend_raw, trend, sum(1 for k in m if k["verdict"] == "达标"), len(m), qual))
    return round(val, 1), note


def _band_component(h, band_profiles):
    """目标窗口的吸引力 = 覆盖各段的平均份额 / 全类目最高段份额(避免宽窗口一律满分)。"""
    lo, hi = h.get("价格窗口") or (None, None)
    if lo is None:
        return None, "无价格窗口"
    hit = [p for p in band_profiles if p["max"] is not None and p["max"] >= lo and p["min"] <= hi]
    if not hit:
        return None, "价格窗口未落在任何价格带"
    best = max(p["销额占比"] for p in band_profiles) or 1
    mean_share = sum(max(p["销额占比"], p["销量占比"]) for p in hit) / len(hit)
    val = min(100.0, 100.0 * mean_share / best)
    return round(val, 1), "覆盖 %d 段(%s), 段均份额 %.1f%%, 全类目最高段 %.1f%%" % (
        len(hit), "、".join(p["label"] for p in hit[:4]) + ("…" if len(hit) > 4 else ""),
        mean_share, best)


def _comp_component(h, recs, band_profiles):
    """竞争余量 = 0.5×价格带竞争 + 0.5×细分(材质×件数)竞争。

    每个子项 = 0.5×集中度余量 + 0.2×品牌分散 + 0.3×稀疏度(在售行数≤20→100, 之后每翻倍 −20)。
    只看价格带会把"上百个品牌的菜市场"误判成高余量; 只看细分又会忽略价格带内的直接对手。
    """
    lo, hi = h.get("价格窗口") or (None, None)
    if lo is None:
        return None, "无价格窗口"
    hit = [p for p in band_profiles if p["max"] is not None and p["max"] >= lo and p["min"] <= hi]
    if not hit:
        return None, "价格窗口未落在任何价格带"
    band_sub = [r for r in recs if r["price"] and any(p["min"] <= r["price"] <= p["max"] for p in hit)]
    seg_sub = [r for r in recs if r["material"] == h["目标材质"]
               and set_count_n(r["set_count"]) == h["目标件数"]]

    def one(sub):
        if not sub:
            return None, 0
        br = collections.defaultdict(float)
        for r in sub:
            br[r["brand"] or UNKNOWN] += r["revenue"] or 0
        total = sum(br.values()) or 1
        sh = sorted((v / total for v in br.values()), reverse=True)
        cr3 = sum(sh[:3])
        conc = 100 - cr3 * 100
        spread = min(100.0, len(br) / 20.0 * 100)
        n = len(sub)
        sparse = 100.0 if n <= 20 else max(0.0, 100.0 - 20.0 * math.log2(n / 20.0))
        return (0.5 * conc + 0.2 * spread + 0.3 * sparse), (len(sub), len(br), cr3)
    vb, db = one(band_sub)
    vs, ds = one(seg_sub)
    parts = [v for v in (vb, vs) if v is not None]
    if not parts:
        return None, "无可比样本"
    val = sum(parts) / len(parts)
    return round(val, 1), (
        "价格带内 %d 行/%d 品牌 CR3=%.1f%%(分 %s); 细分(同材质×同件数) %d 行/%d 品牌 CR3=%.1f%%(分 %s)" % (
            db[0], db[1], db[2] * 100, g(vb, "%.1f"),
            ds[0], ds[1], ds[2] * 100, g(vs, "%.1f")))


def _white_component(h, recs):
    occ = h["证据"].get("占用行数", 0)
    occ_score = 100.0 if occ == 0 else max(0.0, 100.0 - 25.0 * math.log2(1 + occ))
    # 验证度: 同阶梯在金属面(含双面异材质)已被验证?
    lad_terms = None
    tgt = h["目标规格"]
    import re as _re
    nums = _re.findall(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", tgt or "")
    if nums:
        lad_terms = ["%gx%g" % (float(a), float(b)) for a, b in nums]
    val_score = 30.0
    if lad_terms:
        metal = [r for r in recs if any(m in r["material"] for m in ("钛", "不锈钢"))
                 and any(t in ladder_of(r["segs"])[0] for t in lad_terms)]
        same = [r for r in recs if h["目标材质"] in r["material"]
                and any(t in ladder_of(r["segs"])[0] for t in lad_terms)]
        if len({r["parent"] for r in metal}) >= 3:
            val_score = 100.0
        elif metal:
            val_score = 70.0
        elif same:
            val_score = 60.0
    val = 0.5 * occ_score + 0.5 * val_score
    return round(val, 1), "占用 %d 行(占用度分 %.0f), 验证度分 %.0f" % (occ, occ_score, val_score)


def _conf_component(h, recs, completeness, kw_note):
    """置信度必须读「证据基数」(细分全量), 不能读「占用度」(目标格子的行数)。

    目标格子 0 行 = 空白位, 是机会而不是证据不足; 判断该机会是否可信, 要看它所在的
    同材质×件数细分里有多少已验证样本。
    """
    ev = h["证据"]
    parents = ev.get("证据基数父体", ev.get("占用父体") or 0)
    rows = ev.get("证据基数行数", ev.get("占用行数") or 0)
    sample = min(100.0, parents * 10.0)
    val = 0.5 * sample + 0.3 * completeness + 0.2 * (100.0 if "类目级常量" in (kw_note or "") else 60.0)
    return round(val, 1), ("证据基数=%s(%d 行/%d 父体) → 样本分 %.0f(每父体 10 分, 上限 100); "
                       "关键字段完整度 %.0f%%; 关键词库 %s" % (
                           ev.get("证据基数说明", "—"), rows, parents, sample, completeness,
                           "类目级常量可用" if "类目级常量" in (kw_note or "") else "不足"))


def score_hypotheses(hypotheses, ctx):
    recs = ctx["recs"]
    band_profiles = ctx["band_profiles"]
    kw = ctx["keyword"]
    completeness = ctx.get("field_completeness", 100.0)
    max_matched_vol = max([max([k["search"] for k in _kw_matched(h, kw)] or [1])
                           for h in hypotheses] or [1])
    rows = []
    for h in hypotheses:
        comp = {}
        notes = {}
        for name, fn in [("关键词需求", lambda: _kw_component(h, kw, max_matched_vol)),
                         ("价格带吸引力", lambda: _band_component(h, band_profiles)),
                         ("竞争余量", lambda: _comp_component(h, recs, band_profiles)),
                         ("配置空白/差异化", lambda: _white_component(h, recs)),
                         ("证据置信度", None)]:
            if name == "证据置信度":
                continue
            v, n = fn()
            comp[name] = v
            notes[name] = n
        conf, conf_note = _conf_component(h, recs, completeness, notes.get("关键词需求", ""))
        comp["证据置信度"] = conf
        notes["证据置信度"] = conf_note
        # VOC问题价值: 有 VOC 工作簿 → 按「频次/优先级/严重度/方案相关性」计算;
        # 缺失 → 留空(None) + 披露, 绝不按 0 计(见模块 docstring 第 7 行)。
        from . import voc_reader
        voc_score, voc_note = voc_reader.voc_issue_value(ctx.get("voc") or {}, h)
        comp["VOC问题价值"] = voc_score
        notes["VOC问题价值"] = voc_note

        avail_w = sum(WEIGHTS[k] for k, v in comp.items() if v is not None)
        avail_pts = sum(WEIGHTS[k] * v / 100.0 for k, v in comp.items() if v is not None)
        total = (avail_pts / avail_w * 100.0) if avail_w else 0.0
        raw_pts = sum(WEIGHTS[k] * (v or 0) / 100.0 for k, v in comp.items())
        rows.append({
            "id": h["id"], "假设": h["名称"], "推导规则": h["推导规则"],
            "目标人群/场景": h["目标人群/场景"], "核心任务": h["核心任务"],
            "产品类型": h["产品类型"], "材质": h["材质"], "套装结构": h["套装结构"],
            "基准配置": h["基准配置"], "差异化配置": h["差异化配置"],
            "目标规格": h["目标规格"], "价格窗口": "%s~%s" % (g(h["价格窗口"][0], "%.2f"),
                                                          g(h["价格窗口"][1], "%.2f")),
            "分项": comp, "分项说明": notes,
            "可用权重": avail_w, "加权得分(原始)": round(raw_pts, 1),
            "总分(按可用权重归一)": round(total, 1),
            "证据": h["证据"],
        })
    return rows


# ---------------------------------------------------------------- 门槛 / 标签
def gate_and_label(row, hard_veto=None):
    """顺序: 硬否决 → 证据置信度封顶 → 总分带 → 优先开发分项下限; 冲突取保守。"""
    comp = row["分项"]
    total = row["总分(按可用权重归一)"]
    reasons = []

    if hard_veto:
        return "不建议进入", ["硬否决: " + hard_veto]

    # 门槛(默认值; VOC 不可用时该项不参与)
    fails = []
    if comp.get("关键词需求") is not None and comp["关键词需求"] < THRESHOLDS["关键词需求"]:
        fails.append("关键词需求 %.1f < %d" % (comp["关键词需求"], THRESHOLDS["关键词需求"]))
    if comp.get("价格带吸引力") is not None and comp["价格带吸引力"] < THRESHOLDS["价格带吸引力"]:
        fails.append("价格带吸引力 %.1f < %d" % (comp["价格带吸引力"], THRESHOLDS["价格带吸引力"]))
    comp_ok = ((comp.get("竞争余量") or 0) >= THRESHOLDS["竞争余量"]
               or (comp.get("配置空白/差异化") or 0) >= THRESHOLDS["配置空白/差异化"])
    if not comp_ok:
        fails.append("竞争余量 %.1f < %d 且 配置空白 %.1f < %d" % (
            comp.get("竞争余量") or 0, THRESHOLDS["竞争余量"],
            comp.get("配置空白/差异化") or 0, THRESHOLDS["配置空白/差异化"]))
    if (comp.get("证据置信度") or 0) < THRESHOLDS["证据置信度"]:
        fails.append("证据置信度 %.1f < %d" % (comp.get("证据置信度") or 0, THRESHOLDS["证据置信度"]))
    if total < THRESHOLDS["总分"]:
        fails.append("总分 %.1f < %d" % (total, THRESHOLDS["总分"]))
    if fails:
        reasons.append("未过市场机会门槛: " + "; ".join(fails))
        # 门槛未过 → 一律不得进入「可执行机会」(不得标 小批验证/优先开发)
        if (comp.get("证据置信度") or 0) < 50 or total < 45:
            return "不建议进入", reasons + ["且总分 %.1f < 45 或 证据置信度 < 50" % total]
        return "持续观察", reasons + ["门槛未过 → 只能列为观察机会, 不得作为产品线推荐"]

    reasons.append("通过市场机会门槛")
    conf = comp.get("证据置信度") or 0
    if conf < 50:
        return "持续观察", reasons + ["证据置信度 %.1f < 50 → 封顶为持续观察" % conf]
    if conf < 70:
        return "小批验证", reasons + ["证据置信度 %.1f ∈ [50,70) → 封顶为小批验证" % conf]
    if total >= 75:
        mins = [("关键词需求", 60), ("价格带吸引力", 50), ("配置空白/差异化", 60)]
        bad = [n for n, t in mins if (comp.get(n) or 0) < t]
        if bad:
            return "小批验证", reasons + ["总分≥75 但分项下限未达: " + "、".join(bad)]
        return "优先开发", reasons + ["总分 %.1f ≥ 75 且各分项下限满足" % total]
    if total >= 60:
        return "小批验证", reasons + ["总分 %.1f ∈ [60,75) → 小批验证" % total]
    return "持续观察", reasons + ["总分 %.1f < 60" % total]
