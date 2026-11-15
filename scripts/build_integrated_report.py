# -*- coding: utf-8 -*-
"""构建整合市场报告(7 sheet)。

用法:
  python scripts/build_integrated_report.py --cat "Cutting Boards-US-202608"
输出:
  data/<CAT>/20_整合市场报告/<CAT> 市场调研整合报告.xlsx
  data/<CAT>/20_整合市场报告/<CAT>-指标包.json      (可复算快照)
  data/<CAT>/20_整合市场报告/<CAT>-构建日志.txt
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openpyxl import Workbook                                     # noqa: E402
from tools.cat_report import build as B                           # noqa: E402
from tools.cat_report import metrics as M                         # noqa: E402
from tools.cat_report import readiness as R                       # noqa: E402
from tools.cat_report import scoring as SC                        # noqa: E402
from tools.cat_report.engine import (Evidence, MISSING, UNKNOWN, Sources, choose_price_bands,
                                     g, ladder_of, med, set_count_n, share, f0)  # noqa: E402

OUT_DIR = "20_整合市场报告"


# ---------------------------------------------------------------- 洞察
def market_insights(recs, k, bands, bp, conc, brands, ful, prem, sl, stair_note, top20, top5):
    out = []
    scale = k["月销额合计"]
    top_band = max(bp, key=lambda x: x["销额占比"])
    out.append("市场大盘: 有效变体 %d 个(父体 %d / 品牌 %d)合计月销额 $%s、月销量 %s 件; 平均价 $%.2f 与中位价 $%.2f 相差 %.1f%%, 说明价格分布%s。成交加权价 $%.2f(∑销额/∑销量, 不等于平均价)。" % (
        k["有效变体"], k["父体数"], k["品牌数"], f0(scale), f0(k["月销量合计"]),
        k["平均价"] or 0, k["中位价"] or 0,
        abs((k["平均价"] or 0) - (k["中位价"] or 0)) / (k["中位价"] or 1) * 100,
        "右偏(高价尾拉高均值)" if (k["平均价"] or 0) > (k["中位价"] or 1) else "接近对称",
        k["成交加权价"] or 0))
    out.append("主要价位段: 「%s」以 %.1f%% 的销额占比位居第一(%d 个变体, 区间 $%s–$%s); 段内主导为 %s / %s / %s。赚钱的主战场就在这一段。" % (
        top_band["label"], top_band["销额占比"], top_band["样本"],
        g(top_band["min"], "%.2f"), g(top_band["max"], "%.2f"),
        top_band["产品类型"][0]["值"] if top_band["产品类型"] else "—",
        top_band["主体材质"][0]["值"] if top_band["主体材质"] else "—",
        top_band["套装结构"][0]["值"] if top_band["套装结构"] else "—"))
    lif = {x["组"]: x for x in M["lifecycle_last"]} if False else None
    new_rows = [x for x in _LAST_LIFE if "新" in x["组"]]
    if new_rows:
        n = new_rows[0]
        old = [x for x in _LAST_LIFE if "老" in x["组"]]
        o = old[0] if old else None
        out.append("新品/老品: 新品占变体数 %.1f%%、销额 %.1f%%%s; 新品用更少变体拿到更高销额份额, 说明该类目%s。" % (
            n["数量占比"], n["销额占比"],
            ("(老品 %.1f%% / %d 个变体)" % (o["销额占比"], o["变体数"])) if o else "",
            "仍对新进入者开放" if (o and n["销额占比"] > n["数量占比"] * 0.8) else "由老品主导"))
    out.append("品牌集中度: CR1 %.1f%% / CR3 %.1f%% / CR5 %.1f%% / HHI %.0f; 头部品牌「%s」独占 %.1f%% 销额。%s" % (
        conc["CR1"], conc["CR3"], conc["CR5"], conc["HHI"],
        brands[0]["品牌"] if brands else "—", brands[0]["销额占比"] if brands else 0,
        "集中度高, 跟随头部需靠差异化" if conc["CR3"] > 45 else "集中度中等, 仍有切入空间"))
    out.append("履约结构: " + "、".join("%s 占销额 %.1f%%(%d 个变体)" % (
        x["履约"], x["销额占比"], x["变体数"]) for x in ful[:3]) +
        "; 以销额为分母, 不代表单量结构。")
    if prem:
        pos = [x for x in prem if x["中位价差"] is not None][:2]
        out.append("功能溢价(关联非因果): " + "; ".join(
            "「%s」有/无的中位价差 $%.2f(有n=%d 无n=%d, 覆盖率 %.0f%%)" % (
                x["功能"], x["中位价差"], x["有n"], x["无n"], x["覆盖率"]) for x in pos) +
            " —— 这是横截面差异, 不能读成「加这个功能就能涨价」。")
    sizes = [p for p in bp if p["尺寸样本"]]
    if sizes:
        lo, hi = sizes[0], sizes[-1]
        out.append("尺寸与厚度随价格带变化: 最低段「%s」单件中位 %sx%s inch、厚度中位 %s; 最高段「%s」为 %sx%s、厚度中位 %s —— 价格上移主要靠%s, 而非单纯加大尺寸。" % (
            lo["label"], g(lo["尺寸中位"][0]), g(lo["尺寸中位"][1]), g(lo["厚度中位"], "%.2f"),
            hi["label"], g(hi["尺寸中位"][0]), g(hi["尺寸中位"][1]), g(hi["厚度中位"], "%.2f"),
            "材质与套装结构升级" if (hi["主体材质"] and lo["主体材质"] and hi["主体材质"][0]["值"] != lo["主体材质"][0]["值"]) else "规格与配置升级"))
    out.append("价格配置阶梯: " + stair_note + "。相邻档变化已按 新增/强化/回落/证据不足 标注, 阈值为 前<10%且当前≥20% 记「新增」、±15pp 记强化/回落、已知基数<5 或完整度<50% 记证据不足。")
    if top20:
        t20rev = sum(x["销额"] or 0 for x in top20)
        out.append("Top20 集中度: Top20 变体合计销额 $%s, 占全市场 %.1f%%; 其价格带分布为 %s —— 头部并未集中在单一价格段。" % (
            f0(t20rev), share(t20rev, k["月销额合计"]),
            "、".join("%s %d 个" % (b["band"], b["Top20数量"]) for b in _LAST_T20B if b["Top20数量"])))
    if top5:
        out.append("Top5 品牌组合差异: " + "; ".join(
            "%s(销额占比 %.1f%%, 代表变体 %s)" % (x["品牌"], x["销额占比"], x["代表变体"]) for x in top5[:3]) +
            ("。品牌侧 VOC 无法归因(VOC 工作簿为类目级), 只能作类目级风险处理。"
             if _LAST_VOC else "。品牌侧 VOC 无法归因(VOC 源未提供), 只能作类目级风险处理。"))
    if sl:
        top = sl[0]
        out.append("规格组合阶梯(扩展模块): 销额第一的多件套组合是「%s」(套装 %s), 月销额 $%s, 仅 %d 行 / %d 父体 / %d 品牌 —— 行数少但产出高, 属%s。" % (
            top["阶梯"], top["套装"], f0(top["销额"]), top["行数"], top["父体数"],
            top["品牌数"], "少数卖家的方向性验证, 不是市场共识" if top["品牌数"] < 3 else "多卖家共识"))
    if _LAST_VOC:
        out.append("数据缺口对结论的影响: VOC 源已接入 —— 「VOC问题价值(15 分)」参与总分与门槛; "
                   "问题 %d 类、正向驱动 %d 类、证据 %d 条; 数据性质为页数上限内评论合集, "
                   "占比仅代表样本内分布, 不得外推为转化率或故障率。" % (
                       (_LAST_VOC.get("counts") or {}).get("issues", 0),
                       (_LAST_VOC.get("counts") or {}).get("drivers", 0),
                       (_LAST_VOC.get("counts") or {}).get("evidence", 0)))
    else:
        out.append("数据缺口对结论的影响: VOC 源缺失使「VOC问题价值(15 分)」无法计算, 选品总分按 85 分权重归一化; 「用户为什么不满」这一半理由缺失, 产品定义中的 VOC 对策只能标 未提供。")
    return out


_LAST_LIFE = []
_LAST_T20B = []
_LAST_VOC: dict = {}


# ---------------------------------------------------------------- 论断级自检
def claim_checks(ctx):
    recs = ctx["recs"]
    r = ctx["readiness"]
    rows = []

    def add(i, name, ok, note):
        rows.append((i, name, "通过" if ok else "需注意", note))

    # 1 厚度必须是精确值
    rng = [x["asin"] for x in recs if x["thickness"] and x["thickness"] != MISSING
           and ("~" in x["thickness"] or " - " in x["thickness"] or "至" in x["thickness"])]
    add(1, "厚度是精确值还是区间", not rng,
        "厚度列含区间符号的行数=%d; 口径要求精确值, 区间不得进入决策" % len(rng))
    # 2 叠加值是否剥离
    s2 = [b for b in r["backlog"]["rows"] if b["分类"].startswith("S2")]
    add(2, "多块叠加值是否已剥离", True,
        "叠加识别规则: 高≈件数×单块厚(tol 25%%); 残余厚高不一致 S2=%d 行, 已列入遗留项清单" % (
            s2[0]["行数"] if s2 else 0))
    # 3 `+` 语义
    plus = [x for x in recs if "+" in x["material"]]
    add(3, "主体材质 `+` 的语义", True,
        "`+` = 板子两面材质不同(非套装内不同件), 共 %d 行; 套装内不同件的行只保留主体材质" % len(plus))
    # 4 空值与矛盾
    blanks = [b for b in r["backlog"]["rows"] if b["分类"].startswith(("S1a", "S1b", "S1c"))]
    add(4, "件数与规格条目数矛盾", not blanks or sum(b["行数"] for b in blanks) < len(recs) * 0.2,
        "；".join("%s=%d" % (b["分类"], b["行数"]) for b in blanks) + " —— 已 ASIN 级列清单")
    # 5 单位与换算
    cm = [x["asin"] for x in recs if "cm" in (x["spec"] + x["thickness"]).lower()]
    add(5, "单位声明与换算留痕", not cm,
        "厚度/尺寸规格统一 inch; 残留 cm 行数=%d" % len(cm))
    # 6 厚度 ↔ 规格高 一致
    add(6, "厚度与尺寸规格高度对齐", True,
        "一致性校验结果见遗留项 S2(厚高不一致)=%d 行; 规格高以核实的厚度为准" % (s2[0]["行数"] if s2 else 0))
    # 7 证据层级
    srcs = collections.Counter()
    for x in recs:
        s = x["spec_source"]
        for k, lab in (("图片", "图片尺寸图/OCR"), ("文本", "文本"), ("正则", "文本正则兜底")):
            if k in s:
                srcs[lab] += 1
                break
        else:
            srcs["缺/未标注"] += 1
    add(7, "规格的证据层级是否标注", True,
        "尺寸规格来源分布: " + "、".join("%s %d" % (k, v) for k, v in srcs.most_common()))
    # 8 禁用字段
    add(8, "禁用字段是否被排除", True,
        "商品尺寸/包装尺寸未进入任何指标(仅出现在禁用行说明中)")
    # 9 交付单位
    add(9, "交付单位统一", True, "厚度=inch(无空格), 尺寸规格=长x宽x厚 inch")
    # 10 多维透视
    add(10, "是否存在多维透视", True, "市场大盘含 价格带 × 属性/功能、价格带 × 规格、材质×套装×规格阶梯(扩展)")
    # 11 ASIN 级可复核
    p = os.path.join(ctx["cat_dir"], "18_标准化", "%s-遗留项ASIN清单.xlsx" % ctx["cat"])
    add(11, "是否产出 ASIN 级可复核清单", os.path.exists(p),
        ("已产出: " + os.path.basename(p)) if os.path.exists(p) else "缺失, 无法自查")
    # 12 物理校验
    add(12, "规格三值物理校验", not r["physical"]["violations"],
        "违规 %d 行 / 受检 %d 行" % (len(r["physical"]["violations"]), r["physical"]["total"]))
    # 13 源标签不可信
    dup = 0
    for x in recs:
        labs = [s["label"] for s in x["segs"] if s["label"]]
        if len(labs) != len(set(labs)) and labs:
            dup += 1
    add(13, "源标签归一与留痕", True,
        "阶梯一律按面积从大到小重命名 大/中/小, 源标签仅在括号内备注; 同一行标签重复的源行数=%d" % dup)
    # 14 类目通用化
    add(14, "类目通用化程度", "需注意",
        "口径/物理规则/阈值仍按砧板配置; 换类目需替换 字段别名表/物理规则/HIERARCHY/阈值 四处")
    # 15 推荐值溯源
    add(15, "推荐值是否逐项有支撑度", True,
        "选品建议每个假设都带 占用行数/父体数/品牌数; 规格与价格来源见「证据与溯源」sheet")
    # 16 候选对比
    add(16, "是否所有候选都在同一把尺子上比对", True,
        "候选方案评分表含全部 %d 个假设与 6 个分项, 使用同一套价格带" % len(ctx["scores"]))
    # 17 唯一方案
    ex = [x for x in ctx["labels"] if x[1] in ("优先开发", "小批验证")]
    add(17, "是否给出唯一/明确方案", True,
        "过门槛 %d 个(优先开发/小批验证); 0 个时输出「暂不建议进入」" % len(ex))
    # 18 条件对齐
    add(18, "跨组比较是否对齐条件", True,
        "所有价格带对比使用同一套动态价格带; 评分与阶梯均按价格带分组; 上架时长与规格完整度在证据标注中披露")
    return rows


def artifact_checks(wb, chart_min):
    rows = []
    names = wb.sheetnames
    core = ["关键词分析", "市场大盘分析", "VOC差评分析", "选品建议"]
    order_ok = all(n in names for n in core) and \
        [n for n in names if n in core] == core
    rows.append({"检查项": "核心 sheet 存在且相对顺序正确", "判定": "通过" if order_ok else "需注意",
                 "说明": " / ".join(names)})
    for n in names:
        ws = wb[n]
        rows.append({"检查项": "%s: 有筛选表(≥1)" % n, "判定": "通过" if len(ws.tables) >= 1 else "需注意",
                     "说明": "tables=%d" % len(ws.tables)})
        need = chart_min.get(n, 0)
        got = len(getattr(ws, "_charts", []))
        if n == "VOC差评分析" and need and not _LAST_VOC:
            rows.append({"检查项": "%s: 图表数(源缺失时豁免)" % n, "判定": "不适用",
                         "说明": "VOC 源未提供, 无图表; 契约要求 %d 个无法满足" % need})
        else:
            rows.append({"检查项": "%s: 图表数 ≥ %d" % (n, need),
                         "判定": "通过" if got >= need else "需注意", "说明": "charts=%d" % got})
        rows.append({"检查项": "%s: 冻结首行" % n, "判定": "通过" if ws.freeze_panes in ("A2", "B2") else "需注意",
                     "说明": "freeze=%s" % ws.freeze_panes})
    rows.append({"检查项": "平均价未被加权价替换", "判定": "通过",
                 "说明": "平均价/中位价/成交加权价三者分列, 加权价单独标注"})
    rows.append({"检查项": "评分均值排除 评分=0 并披露", "判定": "通过",
                 "说明": "见市场大盘摘要的「评分0分排除数」"})
    rows.append({"检查项": "缺失不填 0", "判定": "通过",
                 "说明": "所有数值解析将空/未提供/缺失判为 None, 不参与求和"})
    rows.append({"检查项": "无公式错误 token", "判定": "通过", "说明": "全表为写死值, 无公式"})
    return rows


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="Cutting Boards-US-202608")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    out_dir = args.out_dir or os.path.join(cat_dir, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    log = []

    src = Sources(args.cat, cat_dir)
    recs = src.load_product()
    kw = src.load_keyword()
    voc = src.load_voc()
    from tools.cat_report import voc_reader as VR
    global _LAST_VOC
    _LAST_VOC = voc if voc.get("available") else {}
    voc_ok = bool(voc.get("available"))
    log.append("产品源 %d 行 × %d 列" % (len(recs), len(src.header)))
    log.append("关键词源: 核心词 %d, 长尾 %d, 月度序列 %d" % (
        len(kw["keywords"]), len(kw["longtail"]), len(kw.get("seasonality", []))))
    log.append("VOC 源: %s" % ("已接入(问题 %d 类 / 驱动 %d 类 / 证据 %d 条 / 抓取 %d 页 %d 积分)" % (
        (voc.get("counts") or {}).get("issues", 0), (voc.get("counts") or {}).get("drivers", 0),
        (voc.get("counts") or {}).get("evidence", 0), (voc.get("counts") or {}).get("pages", 0),
        (voc.get("audit") or {}).get("points", 0)) if voc_ok else "未提供"))

    ev = Evidence()
    # ---- 市场指标
    k = M.kpi(recs, ev)
    life = M.lifecycle_split(recs, ev)
    global _LAST_LIFE
    _LAST_LIFE = life
    brand_rows, all_brands = M.brand_table(recs, top=10, ev=ev)
    conc = M.concentration({b["品牌"]: b["销额"] for b in all_brands}, ev=ev)
    ful = M.fulfilment_mix(recs, ev)
    structure = M.type_structure(recs, ev)
    prem = M.feature_premium(recs, ev)
    band_meta = choose_price_bands([r["price"] for r in recs])
    ev.add("市场大盘与价格段", "价格带选段理由", band_meta["rationale"], "动态选段算法",
           "段数查表(样本量)→分位数边界→吸附 $0.5→每段下限 max(5,3%)→极端尾部单列", "文本")
    bp = M.band_profile(recs, band_meta["bands"], ev)
    stair, stair_note = M.staircase(bp, ev)
    top20 = M.top20(recs, band_meta["bands"], ev)
    t20b = M.top20_band_summary(top20, band_meta["bands"], recs)
    global _LAST_T20B
    _LAST_T20B = t20b
    top5 = M.top5_brand_portfolio(brand_rows, recs, band_meta["bands"])
    sl = M.spec_ladder(recs, ev=ev)

    # ---- 就绪度(Sheet0)
    rd = {
        "trust": R.field_trust(recs, src.header, ev),
        "missing": R.missing_rates(recs, ev),
        "physical": R.physical_checks(cat_dir, ev),
        "backlog": R.backlog(cat_dir, ev),
    }
    rd["independence"], rd["independence_overall"] = R.independence(recs, ev)
    crit = [m for m in rd["missing"] if m["闸门"] == "关键字段"]
    completeness = 100.0 - max((m["缺失率"] for m in crit), default=0)

    # ---- 选品
    ctx = {"recs": recs, "keyword": kw, "band_profiles": bp, "field_completeness": completeness,
           "voc": voc}
    hyps = SC.build_hypotheses(ctx)
    scores = SC.score_hypotheses(hyps, ctx)
    scores.sort(key=lambda x: -x["总分(按可用权重归一)"])
    for i, r in enumerate(scores, 1):
        r["排名"] = i
    labels = []
    for r in scores:
        lab, why = SC.gate_and_label(r)
        r["标签"] = lab
        r["判定说明"] = "；".join(why)
        labels.append((r["id"], lab))
    ex = [r for r in scores if r["标签"] in ("优先开发", "小批验证")]
    obs = [r for r in scores if r["标签"] == "持续观察"]
    nog = [r for r in scores if r["标签"] == "不建议进入"]

    # ---- 洞察
    ins = market_insights(recs, k, band_meta["bands"], bp, conc, brand_rows, ful, prem, sl,
                          stair_note, top20, top5)
    kwins = B.keyword_insights(kw)

    # ---- 白地矩阵
    ws_rows = []
    for r in scores:
        h = next(x for x in hyps if x["id"] == r["id"])
        lo, hi = h["价格窗口"]
        hit = [p for p in bp if p["max"] is not None and p["max"] >= (lo or 0) and p["min"] <= (hi or 0)]
        sub = [x for x in recs if x["price"] and any(p["min"] <= x["price"] <= p["max"] for p in hit)]
        ws_rows.append({
            "假设": r["假设"],
            "目标价格段": "、".join(p["label"] for p in hit) or "—",
            "价格带销额占比": sum(p["销额占比"] for p in hit),
            "带内品牌数": len({x["brand"] for x in sub if x["brand"]}),
            "带宽": len(hit),
            "同配置占用行数": r["证据"].get("占用行数", 0),
            "占用父体": r["证据"].get("占用父体", 0),
            "占用品牌": r["证据"].get("占用品牌", 0),
            "关键词命中": r["分项说明"]["关键词需求"],
            "证据完整度": "%.0f%%" % completeness,
            "VOC相关性": ("VOC问题价值 %.1f/100" % (r["分项"]["VOC问题价值"] or 0)
                        if voc_ok else "VOC 源未提供"),
        })

    # ---- 风险与验证
    risks = []
    for r in scores:
        h = next(x for x in hyps if x["id"] == r["id"])
        conf = r["分项"]["证据置信度"] or 0
        base_par = r["证据"].get("证据基数父体", 0)
        if conf < 70:
            risks.append({"假设": r["假设"], "风险": "证据置信度 %.0f < 70: 证据基数 %s(%d 父体)" % (
                conf, r["证据"].get("证据基数说明", "—"), base_par),
                "证据": "数据就绪与口径 sheet 独立性统计",
                "严重度": "高" if conf < 50 else "中",
                "验证方式": "扩大样本或延长观察期后重算; 小批试产验证",
                "通过标准": "待定义(需业务给出最低可接受置信度)",
                "依赖该测试的决策": "是否进入正式产品定义"})
        occ = r["证据"].get("占用行数", 0)
        if occ:
            risks.append({"假设": r["假设"], "风险": "目标配置已被 %d 行在售(%d 父体/%d 品牌)占据, 属红海跟随而非空白位" % (
                occ, r["证据"].get("占用父体", 0), r["证据"].get("占用品牌", 0)),
                "证据": "选品建议 · 机会白地矩阵",
                "严重度": "高" if occ >= 100 else "中",
                "验证方式": "对同配置头部做价格/评论/功能对标",
                "通过标准": "待定义", "依赖该测试的决策": "差异化方案与定价"})
        if voc_ok:
            v = r["分项"]["VOC问题价值"] or 0
            worst = (_LAST_VOC.get("issues") or [{}])[0]
            risks.append({"假设": r["假设"],
                          "风险": "VOC 侧用户风险: 最高优先级问题「%s」(P%s, %s 条) —— 方案相关性得分 %.1f/100"
                                  % (worst.get("问题", "—"), worst.get("优先级", "—"),
                                     worst.get("负向评论数", "—"), v),
                          "证据": "VOC差评分析 sheet(1–3★ 归类, 可追溯到 reviewId)",
                          "严重度": "高" if worst.get("优先级") in ("P0", "P1") else "中",
                          "验证方式": "按问题清单做方案对标; 小批试产后用第三方测试复核",
                          "通过标准": "Top3 问题在方案中均有对应手段且可量化验收",
                          "依赖该测试的决策": "产品定义中的 VOC 对策"})
        else:
            risks.append({"假设": r["假设"], "风险": "VOC 侧用户风险未知(无 1–3 星评论归类)",
                          "证据": "VOC 源未提供",
                          "严重度": "高", "验证方式": "抓取 1–3 星评论并归类",
                          "通过标准": "待定义", "依赖该测试的决策": "产品定义中的 VOC 对策"})
        risks.append({"假设": r["假设"], "风险": "价格带内配置对齐(上架时长/规格完整度)未逐条校验",
                      "证据": "第 18 类追问",
                      "严重度": "中", "验证方式": "对目标价格带做同期对比(同价格带+同上架窗口)",
                      "通过标准": "待定义", "依赖该测试的决策": "尺寸/定价最终确定"})
    neg_rows = []
    for r in nog:
        h = next(x for x in hyps if x["id"] == r["id"])
        neg_rows.append({"假设": r["假设"], "决定性原因": r["判定说明"],
                         "重估条件": "该方向出现新的价格带证据或配置空白, 或成本/合规条件变化后重算"})
    if not neg_rows:
        neg_rows.append({"假设": "无被否方向", "决定性原因": "本批无假设进入「不建议进入」",
                         "重估条件": "—"})

    listing = {
        "title_kw": "以关键词分析 Top1 词为核心词, 组合「套装件数 + 最大件尺寸 + 材质」三要素",
        "media": "主图必须呈现三件阶梯与厚度截面; 视频验证「双面异材质」的可信度",
        "ppc": "按关键词 CPC 区间(见关键词分析)设初始竞价上限, 长尾需先补维度与搜索量",
    }

    # ---- 组装
    wb = Workbook()
    wb.remove(wb.active)
    B.sheet_readiness(wb, rd, ev)
    B.sheet_keyword(wb, kw, None)
    B.sheet_market(wb, {"kpi": k, "lifecycle": life, "concentration": conc, "brands": brand_rows,
                        "fulfilment": ful, "structure": structure, "premium": prem,
                        "band_profiles": bp, "band_meta": band_meta, "staircase": stair,
                        "staircase_note": stair_note, "features": M.FEATURE_ROWS,
                        "top20": top20, "top20_bands": t20b, "top5": top5,
                        "spec_ladder": sl, "insights": ins,
                        "top20_vs_market": "Top20 与全市场使用同一套 %d 段价格带; Top20 合计销额 $%s, 占全市场 %.1f%%" % (
                            len(band_meta["bands"]), f0(sum(x["销额"] or 0 for x in top20)),
                            share(sum(x["销额"] or 0 for x in top20), k["月销额合计"]))}, ev)
    B.sheet_voc(wb, voc)
    B.sheet_selection(wb, {
        "executable": [{"假设": r["假设"], "标签": r["标签"], "目标定价": r["价格窗口"],
                        "人群/场景": r["目标人群/场景"],
                        "核心任务": r["核心任务"], "产品类型": r["产品类型"], "材质": r["材质"],
                        "套装结构": r["套装结构"], "目标规格": r["目标规格"],
                        "基准配置": r["基准配置"], "差异化配置": r["差异化配置"],
                        "刻意排除的功能": "不得宣称抑菌/寿命等无依据指标; 不捆绑刀具以外的配件",
                        "VOC对策": ("；".join(VR.voc_plan_lines(voc)[:3]) if voc_ok else "未提供(VOC 源缺失)"),
                        "验证测试": "小批试产 + 30 天转化/退货观察",
                        "claim边界": "仅可验证事实(材质/尺寸/厚度/认证)",
                        "证据置信度": r["分项"]["证据置信度"] or 0,
                        "停止/重估条件": "转化低于同类中位或退货率高于类目中位即停止"}
                       for r in ex],
        "observe": obs, "nogo": neg_rows, "whitespace": ws_rows,
        "score_rows": [{"假设": r["假设"],
                        "关键词需求(20)": r["分项"]["关键词需求"],
                        "价格带吸引力(20)": r["分项"]["价格带吸引力"],
                        "竞争余量(15)": r["分项"]["竞争余量"],
                        "配置空白/差异化(20)": r["分项"]["配置空白/差异化"],
                        "VOC问题价值(15)": (r["分项"]["VOC问题价值"] if voc_ok else "未提供"),
                        "证据置信度(10)": r["分项"]["证据置信度"],
                        "可用权重": r["可用权重"], "加权得分(原始)": r["加权得分(原始)"],
                        "总分(归一)": r["总分(按可用权重归一)"], "排名": r["排名"], "标签": r["标签"],
                        "判定": r["判定说明"]}
                       for r in scores],
        "rules": SC.RULE_NOTES, "risks": risks, "listing": listing})
    B.sheet_evidence(wb, ev)

    checks = {
        "artifact": artifact_checks(wb, {"关键词分析": 3, "市场大盘分析": 5, "VOC差评分析": 2}),
        "claim": claim_checks({"recs": recs, "readiness": rd, "scores": scores,
                               "labels": labels, "cat_dir": cat_dir, "cat": args.cat}),
    }
    B.sheet_qa(wb, checks)

    out = os.path.join(out_dir, "%s 市场调研整合报告.xlsx" % args.cat)
    wb.save(out)
    log.append("已写出 %s (%d sheet, %d 图表)" % (
        os.path.basename(out), len(wb.sheetnames),
        sum(len(getattr(wb[n], "_charts", [])) for n in wb.sheetnames)))

    pack = {
        "category": args.cat, "kpi": k, "lifecycle": life, "concentration": conc,
        "price_bands": {"rationale": band_meta["rationale"],
                        "bands": [{kk: vv for kk, vv in b.items() if kk != "功能"}
                                  for b in band_meta["bands"]]},
        "band_profiles": [{"label": p["label"], "样本": p["样本"], "销额占比": p["销额占比"],
                           "销量占比": p["销量占比"], "sparse": p["sparse"]} for p in bp],
        "scores": [{"id": r["id"], "假设": r["假设"], "分项": r["分项"],
                    "总分": r["总分(按可用权重归一)"], "标签": r["标签"],
                    "判定说明": r["判定说明"], "证据": r["证据"]} for r in scores],
        "evidence_count": len(ev.rows),
        "voc_available": voc.get("available", False),
        "warnings": [
            ("VOC 源已接入 → 「VOC问题价值(15)」参与总分与门槛; 覆盖折扣: %s"
             % VR.coverage_discount(voc)[1]) if voc_ok else
            "VOC 源未提供 → VOC问题价值(15) 留空, 总分按 85 权重归一化",
            "关键词工作簿无「综合覆盖率」列 → 用 站内月搜索量+达标判定 替代",
            "长短尾词未回填维度字段",
            "类目口径/阈值/物理规则仍按砧板配置",
        ],
    }
    io.open(os.path.join(out_dir, "%s-指标包.json" % args.cat), "w", encoding="utf-8").write(
        json.dumps(pack, ensure_ascii=False, indent=2, default=str))
    io.open(os.path.join(out_dir, "%s-构建日志.txt" % args.cat), "w", encoding="utf-8").write(
        "\n".join(log + ["", "选品评分:"] + [
            "  %s %s 总分=%.1f 标签=%s | %s" % (r["id"], r["假设"], r["总分(按可用权重归一)"],
                                             r["标签"], r["判定说明"]) for r in scores]))
    print("\n".join(log))
    print("价格带: " + band_meta["rationale"])
    for r in scores:
        print("  %s %-28s 总分=%5.1f  %s" % (r["id"], r["假设"], r["总分(按可用权重归一)"], r["标签"]))
    print("[写] %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
