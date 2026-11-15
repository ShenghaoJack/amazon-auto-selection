# -*- coding: utf-8 -*-
"""市场大盘指标: KPI / 生命周期 / 品牌集中度 / 履约 / 属性结构 / 功能溢价 /
价格带画像 / 价格配置阶梯 / Top20 产品级矩阵 / 规格组合阶梯 / Top5 品牌组合。"""
from __future__ import annotations

import collections

from .engine import (BINARY_FEATURES, MISSING, UNKNOWN, band_of, g, is_positive,
                     ladder_of, med, q, set_count_n, share, f0)

# 属性层级(阶梯矩阵行序) —— 按类目配置化, 此处为砧板口径
HIERARCHY = [
    ("市场基础", ["count", "sales_share", "revenue_share"]),
    ("产品类型", ["product_type"]),
    ("主体材质", ["material"]),
    ("套装/件数结构", ["set_count"]),
    ("单件尺寸(中位, 有效样本)", ["size_median"]),
    ("厚度(中位, 有效样本)", ["thickness_median"]),
    ("形状", ["shape"]),
    ("功能密度", ["feature_count"]),
]
FEATURE_ROWS = BINARY_FEATURES


def kpi(recs, ev=None, module="市场大盘"):
    n = len(recs)
    priced = [r for r in recs if r["price"] is not None and r["price"] > 0]
    priced_sales = [r for r in recs if r["sales"] is not None]
    priced_rev = [r for r in recs if r["revenue"] is not None]
    sales_sum = sum(r["sales"] or 0 for r in priced_sales)
    rev_sum = sum(r["revenue"] or 0 for r in priced_rev)
    avg_p = sum(r["price"] for r in priced) / len(priced) if priced else None
    med_p = med([r["price"] for r in priced])
    tw = (rev_sum / sales_sum) if sales_sum else None
    # 评分均值必须排除 评分=0, 并披露排除数
    rated = [r for r in recs if r["rating"] is not None and r["rating"] > 0]
    excl_zero = sum(1 for r in recs if r["rating"] == 0)
    avg_r = sum(r["rating"] for r in rated) / len(rated) if rated else None
    out = {
        "总行数": n, "有效变体": len(priced_sales) or n,
        "有效价格样本": len(priced), "有销量样本": len(priced_sales), "有销额样本": len(priced_rev),
        "月销量合计": sales_sum, "月销额合计": rev_sum,
        "平均价": avg_p, "中位价": med_p, "成交加权价": tw,
        "评分均值(排除0分)": avg_r, "评分0分排除数": excl_zero,
        "父体数": len({r["parent"] for r in recs}),
        "品牌数": len({r["brand"] for r in recs if r["brand"]}),
    }
    if ev is not None:
        ev.add(module, "有效变体", out["有效变体"], "产品源: 变体ASIN 且 变体月销量非空",
               "非空计数", "个", n, out["父体数"], out["品牌数"])
        ev.add(module, "月销量合计", round(sales_sum, 0), "产品源字段 %s" % "变体月销量",
               "SUM(变体月销量)", "件", len(priced_sales))
        ev.add(module, "月销额合计", round(rev_sum, 2), "产品源字段 变体月销额",
               "SUM(变体月销额)", "$", len(priced_rev))
        ev.add(module, "平均价", round(avg_p, 2) if avg_p else None, "产品源字段 价格($)",
               "有效价格样本的算术平均(price>0)", "$", len(priced))
        ev.add(module, "中位价", round(med_p, 2) if med_p else None, "产品源字段 价格($)",
               "与平均价同一样本的中位数", "$", len(priced))
        ev.add(module, "成交加权价", round(tw, 2) if tw else None, "变体月销额 / 变体月销量",
               "SUM(销额)/SUM(销量); 禁止当作平均价", "$", len(priced_rev))
        ev.add(module, "评分均值", round(avg_r, 2) if avg_r else None, "产品源字段 评分",
               "排除 评分=0 后取算术平均; 披露排除数=%d" % excl_zero, "星", len(rated))
    return out


def lifecycle_split(recs, ev=None):
    tot_n = len(recs)
    tot_s = sum(r["sales"] or 0 for r in recs)
    tot_v = sum(r["revenue"] or 0 for r in recs)
    cnt = collections.Counter(r["lifecycle"] or UNKNOWN for r in recs)
    out = []
    for k, c in cnt.most_common():
        sub = [r for r in recs if (r["lifecycle"] or UNKNOWN) == k]
        s = sum(x["sales"] or 0 for x in sub)
        v = sum(x["revenue"] or 0 for x in sub)
        out.append({"组": k, "变体数": c, "数量占比": share(c, tot_n),
                    "销量": s, "销量占比": share(s, tot_s),
                    "销额": v, "销额占比": share(v, tot_v)})
    if ev is not None:
        ev.add("市场大盘", "生命周期分组", "、".join("%s %d" % (x["组"], x["变体数"]) for x in out),
               "产品源字段 新老品", "分组计数(未做推断)", "组", tot_n)
    return out


def concentration(revenue_by_group, label="品牌", ev=None, module="品牌与履约集中度"):
    vals = sorted((v for v in revenue_by_group.values() if v), reverse=True)
    total = sum(vals)
    sh = [v / total for v in vals] if total else []
    out = {"CR1": share(sum(sh[:1]), 1), "CR3": share(sum(sh[:3]), 1),
           "CR5": share(sum(sh[:5]), 1),
           "HHI": sum(s * s for s in sh) * 10000, "组数": len(vals)}
    if ev is not None:
        for k in ("CR1", "CR3", "CR5", "HHI"):
            ev.add(module, "%s %s" % (label, k), round(out[k], 2),
                   "产品源 变体月销额", "按%s汇总销额后计算份额; 分母=全部%s销额" % (label, label),
                   "%" if k != "HHI" else "点")
    return out


def brand_table(recs, top=10, ev=None):
    by = collections.defaultdict(list)
    for r in recs:
        by[r["brand"] or UNKNOWN].append(r)
    rows = []
    total_rev = sum(r["revenue"] or 0 for r in recs)
    for b, sub in by.items():
        rev = sum(x["revenue"] or 0 for x in sub)
        pr = [x["price"] for x in sub if x["price"]]
        rated = [x["rating"] for x in sub if x["rating"] and x["rating"] > 0]
        new = sum(1 for x in sub if x["lifecycle"] and "新" in x["lifecycle"])
        ful = collections.Counter(x["fulfilment"] or UNKNOWN for x in sub)
        rows.append({
            "品牌": b, "变体数": len(sub), "新品数": new,
            "销量": sum(x["sales"] or 0 for x in sub), "销额": rev,
            "销额占比": share(rev, total_rev),
            "价格区间": "%s~%s" % (g(min(pr), "%.2f") if pr else "—", g(max(pr), "%.2f") if pr else "—"),
            "平均价": (sum(pr) / len(pr)) if pr else None, "中位价": med(pr),
            "有效评分(排除0)": (sum(rated) / len(rated)) if rated else None,
            "履约结构": "、".join("%s %d" % (k, v) for k, v in ful.most_common(3)),
        })
    rows.sort(key=lambda x: -x["销额"])
    return rows[:top], rows


def fulfilment_mix(recs, ev=None):
    cnt = collections.Counter((r["fulfilment"] or UNKNOWN) for r in recs)
    total_rev = sum(r["revenue"] or 0 for r in recs)
    out = []
    for k, c in cnt.most_common():
        rev = sum(r["revenue"] or 0 for r in (x for x in recs if (x["fulfilment"] or UNKNOWN) == k))
        out.append({"履约": k, "变体数": c, "销额": rev, "销额占比": share(rev, total_rev)})
    if ev is not None:
        ev.add("品牌与履约集中度", "履约销额占比",
               "、".join("%s %.1f%%" % (x["履约"], x["销额占比"]) for x in out),
               "产品源字段 配送方式", "以 变体月销额 为分母")
    return out


def _mix(recs, keyfunc):
    cnt = collections.Counter(keyfunc(r) or UNKNOWN for r in recs)
    total_rev = sum(r["revenue"] or 0 for r in recs)
    out = []
    for k, c in cnt.most_common():
        rev = sum(r["revenue"] or 0 for r in recs if (keyfunc(r) or UNKNOWN) == k)
        out.append({"值": k, "变体数": c, "销额": rev, "销额占比": share(rev, total_rev)})
    return out


def type_structure(recs, ev=None):
    t = _mix(recs, lambda r: r["product_type"])
    m = _mix(recs, lambda r: r["material"])
    s = _mix(recs, lambda r: r["set_count"])
    if ev is not None:
        for name, tab in (("产品类型", t), ("主体材质", m), ("套装结构", s)):
            ev.add("产品类型与属性结构", name + "分布",
                   "、".join("%s %d(%.1f%%)" % (x["值"], x["变体数"], x["销额占比"]) for x in tab[:5]),
                   "产品源经 17/18 步标准化后的属性列", "组内以 变体月销额 为分母")
    return {"产品类型": t, "主体材质": m, "套装结构": s}


def band_profile(recs, bands, ev=None):
    """每个价格带: 样本/极值/份额/类型材质套装结构/单件尺寸中位/功能 prevalence+完整度。"""
    total_sales = sum(r["sales"] or 0 for r in recs)
    total_rev = sum(r["revenue"] or 0 for r in recs)
    out = []
    for b in bands:
        sub = [r for r in recs if band_of(bands, r["price"]) is b]
        rev = sum(x["revenue"] or 0 for x in sub)
        sal = sum(x["sales"] or 0 for x in sub)
        sizes = [x for x in sub if x["segs"]]
        L = [s["l"] for x in sizes for s in x["segs"]]
        W = [s["w"] for x in sizes for s in x["segs"]]
        H = [s["h"] for x in sub for s in x["segs"] if s["h"] is not None]
        ths = []
        for x in sub:
            if x["thickness"] and x["thickness"] != MISSING:
                import re as _re
                ths += [float(v) for v in _re.findall(r"(\d+(?:\.\d+)?)", x["thickness"])]
        row = {
            "band": b, "label": b["label"], "样本": len(sub),
            "min": b["min"], "max": b["max"],
            "销量": sal, "销量占比": share(sal, total_sales),
            "销额": rev, "销额占比": share(rev, total_rev),
            "产品类型": _mix(sub, lambda r: r["product_type"])[:2],
            "主体材质": _mix(sub, lambda r: r["material"])[:2],
            "套装结构": _mix(sub, lambda r: r["set_count"])[:2],
            "尺寸中位": (med(L), med(W), med(H)), "尺寸样本": len(L),
            "厚度中位": med(ths), "厚度样本": len(ths),
            "功能": {}, "sparse": len(sub) < 5 or len(sub) < 0.03 * len(recs),
        }
        for f in BINARY_FEATURES:
            yes = sum(1 for x in sub if is_positive(f, x[f]) is True)
            no = sum(1 for x in sub if is_positive(f, x[f]) is False)
            known = yes + no
            row["功能"][f] = {
                "yes": yes, "known": known, "all": len(sub),
                "prev": share(yes, known), "complete": share(known, len(sub)),
            }
        out.append(row)
    if ev is not None:
        ev.add("市场大盘与价格段", "价格带段数与边界",
               " / ".join(x["label"] for x in out), "动态选段(见口径说明)", "段")
        for x in out:
            ev.add("市场大盘与价格段", "价格带 %s 销额占比" % x["label"],
                   round(x["销额占比"], 2), "产品源 变体月销额 + 价格($)",
                   "组内销额/全市场销额", "%", x["样本"])
    return out


def staircase(profiles, ev=None):
    """相邻档变化: 新增/强化/回落/稳定/证据不足(带显式阈值与原始百分点)。"""
    labels = ["新增配置", "覆盖率提升", "配置回落", "证据不足", "稳定"]
    rows = []
    for i, prof in enumerate(profiles):
        cell = {"band": prof["label"], "样本": prof["样本"], "新增配置": [], "覆盖率提升": [],
                "配置回落": [], "证据不足": [], "稳定": []}
        if i > 0:
            prev = profiles[i - 1]
            for f in BINARY_FEATURES:
                a, b = prev["功能"][f], prof["功能"][f]
                if a["known"] < 5 or b["known"] < 5 or a["complete"] < 50 or b["complete"] < 50:
                    cell["证据不足"].append("%s(基数%s→%s/完整度%.0f%%→%.0f%%)" % (
                        f, a["known"], b["known"], a["complete"], b["complete"]))
                    continue
                d = b["prev"] - a["prev"]
                if a["prev"] < 10 and b["prev"] >= 20:
                    cell["新增配置"].append("%s %.0f%%→%.0f%%(+%.1fpp)" % (f, a["prev"], b["prev"], d))
                elif d >= 15:
                    cell["覆盖率提升"].append("%s %.0f%%→%.0f%%(+%.1fpp)" % (f, a["prev"], b["prev"], d))
                elif d <= -15:
                    cell["配置回落"].append("%s %.0f%%→%.0f%%(%.1fpp)" % (f, a["prev"], b["prev"], d))
                else:
                    cell["稳定"].append("%s(%.1fpp)" % (f, d))
        rows.append(cell)
    # 单调性判定
    mono_notes = []
    for f in BINARY_FEATURES:
        seq = [p["功能"][f]["prev"] for p in profiles if p["功能"][f]["known"] >= 5]
        if len(seq) >= 3:
            inc = all(seq[i + 1] >= seq[i] - 1e-9 for i in range(len(seq) - 1))
            dec = all(seq[i + 1] <= seq[i] + 1e-9 for i in range(len(seq) - 1))
            if not inc and not dec:
                rev = [profiles[i + 1]["label"] for i in range(len(profiles) - 1)
                       if profiles[i]["功能"][f]["known"] >= 5 and profiles[i + 1]["功能"][f]["known"] >= 5
                       and profiles[i + 1]["功能"][f]["prev"] < profiles[i]["功能"][f]["prev"] - 1e-9]
                mono_notes.append("%s 非单调(回落出现在 %s)" % (f, "、".join(rev[:3])))
    if ev is not None:
        ev.add("价格配置阶梯矩阵", "阶梯单调性",
               "；".join(mono_notes[:5]) if mono_notes else "全部功能随价格单调不减",
               "同一套详细价格带 + 逐档 prevalence 比较", "文本")
    return rows, ("；".join(mono_notes[:5]) if mono_notes
                  else "所有功能覆盖率随价格带单调不减(无回落)")


def feature_premium(recs, ev=None):
    """横截面对比: 有/无该功能的变体价格对比。关联, 非因果。"""
    rows = []
    for f in BINARY_FEATURES:
        yes = [r for r in recs if is_positive(f, r[f]) is True]
        no = [r for r in recs if is_positive(f, r[f]) is False]
        known = len(yes) + len(no)
        if known == 0:
            rows.append({"功能": f, "明确数": 0, "覆盖率": 0.0, "有n": 0, "无n": 0,
                         "有中位价": None, "无中位价": None, "中位价差": None,
                         "完整度": 0.0, "备注": "全类目无来源支持(0)"})
            continue
        py = [r["price"] for r in yes if r["price"]]
        pn = [r["price"] for r in no if r["price"]]
        rows.append({
            "功能": f, "明确数": known, "覆盖率": share(len(yes), known),
            "有n": len(yes), "无n": len(no),
            "有中位价": med(py), "无中位价": med(pn),
            "中位价差": (med(py) - med(pn)) if (py and pn) else None,
            "有销量": sum(r["sales"] or 0 for r in yes), "有销额": sum(r["revenue"] or 0 for r in yes),
            "完整度": share(known, len(recs)),
            "备注": "关联(横截面对比), 非因果" if known >= 5 else "明确数<5, 仅作线索",
        })
    rows.sort(key=lambda x: -(x["中位价差"] if x["中位价差"] is not None else -999))
    if ev is not None:
        for x in rows[:4]:
            if x["中位价差"] is not None:
                ev.add("功能溢价分析", "功能「%s」中位价差" % x["功能"], round(x["中位价差"], 2),
                       "产品源 价格($) + %s" % x["功能"],
                       "有 vs 无 的横截面中位价差(关联非因果); 有n=%d 无n=%d" % (x["有n"], x["无n"]),
                       "$")
    return rows


def top20(recs, bands, ev=None):
    pool = [r for r in recs if r["revenue"] is not None]
    pool.sort(key=lambda r: -(r["revenue"] or 0))
    top = pool[:20]
    rows = []
    for i, r in enumerate(top, 1):
        b = band_of(bands, r["price"])
        rows.append({
            "排名": i, "asin": r["asin"], "品牌": r["brand"], "价格": r["price"],
            "销量": r["sales"], "销额": r["revenue"], "band": b["label"] if b else UNKNOWN,
            "产品类型": r["product_type"], "主体材质": r["material"],
            "套装结构": r["set_count"], "尺寸规格": r["spec"], "厚度": r["thickness"],
            "ladder": ladder_of(r["segs"])[0], "url": r["url"], "rec": r,
        })
    if ev is not None:
        ev.add("Top20产品分析", "Top20 合计销额", round(sum(x["销额"] or 0 for x in rows), 2),
               "产品源 变体月销额", "按 变体月销额 降序取前 20", "$", len(rows),
               len({x["rec"]["parent"] for x in rows}), len({x["品牌"] for x in rows}))
    return rows


def top20_band_summary(top, bands, recs):
    total_rev = sum(x["销额"] or 0 for x in top)
    out = []
    for b in bands:
        sub = [x for x in top if x["band"] == b["label"]]
        rev = sum(x["销额"] or 0 for x in sub)
        out.append({"band": b["label"], "Top20数量": len(sub),
                    "Top20销额": rev, "Top20销额内占比": share(rev, total_rev),
                    "sparse": len(sub) < 3})
    return out


def top5_brand_portfolio(brand_rows, recs, bands):
    out = []
    for br in brand_rows[:5]:
        sub = [r for r in recs if (r["brand"] or UNKNOWN) == br["品牌"]]
        pr = [r["price"] for r in sub if r["price"]]
        fs = []
        for f in BINARY_FEATURES:
            yes = sum(1 for r in sub if is_positive(f, r[f]) is True)
            known = sum(1 for r in sub if is_positive(f, r[f]) is not None)
            if known:
                fs.append((f, share(yes, known)))
        fs.sort(key=lambda x: -x[1])
        rep = sorted(sub, key=lambda r: -(r["revenue"] or 0))[:2]
        out.append({
            "品牌": br["品牌"], "变体数": br["变体数"], "销额占比": br["销额占比"],
            "价格阶梯": "P25 %s / 中位 %s / P75 %s" % (g(q(pr, .25), "%.2f"), g(med(pr), "%.2f"), g(q(pr, .75), "%.2f")),
            "主力功能": "、".join("%s %.0f%%" % (k, v) for k, v in fs[:3]) or "—",
            "代表变体": "、".join("%s(%s)" % (r["asin"], f0(r["revenue"])) for r in rep),
            "VOC": "类目级VOC, 非品牌归因(VOC 源未提供)",
        })
    return out


def spec_ladder(recs, min_rows=2, ev=None):
    """规格组合阶梯(本项目补充): 多件套的逐件尺寸组合 × 销量产出 × 独立性。"""
    by = collections.defaultdict(list)
    for r in recs:
        n = set_count_n(r["set_count"])
        lad = ladder_of(r["segs"])[0]
        if n and n >= 2 and lad:
            by[(r["set_count"], lad)].append(r)
    rows = []
    for (sc, lad), sub in by.items():
        rev = sum(x["revenue"] or 0 for x in sub)
        rows.append({
            "套装": sc, "阶梯": lad, "行数": len(sub),
            "父体数": len({x["parent"] for x in sub}),
            "品牌数": len({x["brand"] for x in sub if x["brand"]}),
            "销额": rev, "销量": sum(x["sales"] or 0 for x in sub),
            "单行均销额": rev / len(sub) if sub else 0,
            "材质": "、".join("%s%d" % (k, v) for k, v in
                            collections.Counter(x["material"] for x in sub).most_common(3)),
            "双面异材质行数": sum(1 for x in sub if "+" in x["material"]),
        })
    rows.sort(key=lambda x: -x["销额"])
    if ev is not None:
        for x in rows[:5]:
            ev.add("规格组合阶梯(扩展)", "阶梯「%s」销额" % x["阶梯"], round(x["销额"], 2),
                   "产品源 尺寸规格 + 套装数量 + 变体月销额",
                   "按件数×逐件尺寸组合分组; 行数/父体数/品牌数并列以判独立性",
                   "$", x["行数"], x["父体数"], x["品牌数"])
    return rows
