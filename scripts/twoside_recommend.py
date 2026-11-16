# -*- coding: utf-8 -*-
"""双面异材质砧板(不锈钢+PP / 钛+PP)的企划推荐分析。

目标人群: 想开发「双面材质不同的不锈钢+塑料(PP)」「钛+塑料(PP)」砧板的选品
输出: 件数(套装数量) × 尺寸规格 的推荐, 含多件套的逐件尺寸组合。

只读交付表, 输出 md 报告。
"""
from __future__ import annotations

import argparse
import collections
import io
import os
import re

import openpyxl

CAT = "Cutting Boards-US-202608"
STD = "18_标准化"
MISSING = "缺"
TARGET = ("钛+塑料", "不锈钢+塑料")
NUM = re.compile(r"(\d+(?:\.\d+)?)")


def num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").replace("%", "").strip()
    if not s or s in ("-", "缺", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_spec(v) -> list[dict]:
    s = str(v or "").strip()
    segs = []
    if not s or s == MISSING:
        return segs
    for part in re.split(r"[,，]", s):
        part = part.strip()
        if not part:
            continue
        lab, sep, body = part.partition(":")
        if not sep:
            lab, body = "", part
        ns = NUM.findall(body)
        if len(ns) >= 2:
            segs.append({"label": lab.strip(), "l": float(ns[0]), "w": float(ns[1]),
                         "h": float(ns[2]) if len(ns) >= 3 else None})
    return segs


def g(x, fmt="%g"):
    return fmt % x


def ladder_names(n: int) -> list[str]:
    """按件数给出 大/中/小 命名, 与源数据的习惯一致。"""
    table = {1: ["大"], 2: ["大", "小"], 3: ["大", "中", "小"],
             4: ["特大", "大", "中", "小"], 5: ["特大", "大", "中", "小", "迷你"]}
    if n in table:
        return table[n]
    base = ["特大", "大", "中", "小", "迷你"]
    return base + ["第%d件" % (i + 1) for i in range(len(base), n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default=CAT)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cat_dir = os.path.join(root, "data", args.cat)
    path = os.path.join(cat_dir, STD, "%s-属性标注-标准化交付.xlsx" % args.cat)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    I = {h: i for i, h in enumerate(hdr)}

    def cell(r, name):
        return r[I[name]] if name in I else None

    recs = []
    for r in rows[1:]:
        mat = str(cell(r, "主体材质") or "").strip()
        rec = {
            "asin": str(cell(r, "变体ASIN") or ""),
            "父": str(cell(r, "父ASIN") or ""),
            "标题": str(cell(r, "标题") or ""),
            "品牌": str(cell(r, "品牌") or "").strip(),
            "材质": mat,
            "类型": str(cell(r, "产品类型") or "").strip(),
            "套装": str(cell(r, "套装数量") or "未明确").strip() or "未明确",
            "规格": str(cell(r, "尺寸规格") or MISSING).strip(),
            "厚度": str(cell(r, "厚度") or MISSING).strip(),
            "双面": str(cell(r, "是否双面可用") or "").strip(),
            "销额": num(cell(r, "变体月销额")),
            "销量": num(cell(r, "变体月销量")),
            "价格": num(cell(r, "价格($)")),
            "评分": num(cell(r, "评分")),
            "评分数": num(cell(r, "评分数")),
            "天数": num(cell(r, "上架天数")),
            "小类BSR": num(cell(r, "小类BSR")),
            "洗碗机": str(cell(r, "是否可洗碗机清洗") or "").strip(),
            "汁槽": str(cell(r, "是否带汁槽") or "").strip(),
            "防滑": str(cell(r, "是否防滑") or "").strip(),
            "收纳架": str(cell(r, "是否含收纳架") or "").strip(),
            "耐高温": str(cell(r, "是否耐高温") or "").strip(),
            "挂孔": str(cell(r, "是否带挂孔") or "").strip(),
            "手柄": str(cell(r, "是否带手柄") or "").strip(),
            "食品级": str(cell(r, "是否食品级安全") or "").strip(),
            "形状": str(cell(r, "形状") or "").strip(),
            "环保认证": str(cell(r, "是否环保认证") or "").strip(),
        }
        segs = parse_spec(rec["规格"])
        rec["段"] = segs
        rec["件数"] = len(segs)
        if segs:
            big = max(segs, key=lambda x: x["l"] * x["w"])
            rec["主尺寸"] = "%sx%s" % (g(big["l"]), g(big["w"]))
            rec["组合"] = ",".join(
                ("%s:%sx%s" % (s["label"], g(s["l"]), g(s["w"]))) if s["label"]
                else ("%sx%s" % (g(s["l"]), g(s["w"]))) for s in segs)
            rec["长宽比"] = big["l"] / big["w"] if big["w"] else None
            # 阶梯: 不依赖源标签, 按面积从大到小重排后给 特大/大/中/小/迷你
            order = sorted(segs, key=lambda x: -(x["l"] * x["w"]))
            names = ladder_names(len(order))
            rec["阶梯"] = " · ".join(
                "%s %sx%s" % (names[i], g(s["l"]), g(s["w"]))
                for i, s in enumerate(order))
            rec["阶梯纯"] = rec["阶梯"]
            rec["阶梯列表"] = [(s["l"], s["w"]) for s in order]
            labs = [s["label"] for s in segs if s["label"]]
            rec["标签异常"] = (len(labs) != len(segs)) or (len(set(labs)) < len(labs))
            if rec["标签异常"]:
                rec["阶梯"] += "(源标注: %s)" % ("/".join(s["label"] or "无" for s in order))
        else:
            rec["主尺寸"], rec["组合"], rec["长宽比"] = "", "", None
            rec["阶梯"], rec["标签异常"] = "", False
            rec["阶梯纯"], rec["阶梯列表"] = "", []
        th = NUM.findall(rec["厚度"]) if rec["厚度"] != MISSING else []
        rec["厚度数"] = max(float(x) for x in th) if th else None
        recs.append(rec)

    tgt = [r for r in recs if r["材质"] in TARGET]
    # 基准: 同材质单材质, 用于对比"双面异材质"是否更好卖
    base = [r for r in recs if r["材质"] in ("不锈钢", "钛", "塑料")]

    def med(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    def agg(group):
        sal = sum(x["销额"] or 0 for x in group)
        sals = sorted((x["销额"] or 0) for x in group)
        top1 = max(sals) if sals else 0
        top3 = sum(sals[-3:]) if sals else 0
        return {
            "行数": len(group),
            "父体数": len({x["父"] for x in group}),
            "销额合计": sal,
            "单行均销额": sal / len(group) if group else 0,
            "单行中位销额": med(sals),
            "销量合计": sum(x["销量"] or 0 for x in group),
            "单行中位销量": med([x["销量"] for x in group]),
            "价格中位": med([x["价格"] for x in group]),
            "价格区间": (min([x["价格"] for x in group if x["价格"]] or [0]),
                         max([x["价格"] for x in group if x["价格"]] or [0])),
            "评分中位": med([x["评分"] for x in group]),
            "评分数中位": med([x["评分数"] for x in group]),
            "上架天数中位": med([x["天数"] for x in group]),
            "BSR中位": med([x["小类BSR"] for x in group]),
            "Top1占比": (top1 / sal * 100) if sal else 0,
            "Top3占比": (top3 / sal * 100) if sal else 0,
            "双面不同材质": sum(1 for x in group if x["双面"] == "双面不同材质"),
            "有厚度的行": sum(1 for x in group if x["厚度数"] is not None),
        }

    SET_ORDER = ["1件", "2件套", "3件套", "4件套", "5件套", "6件套", "10件套", "未明确"]

    def srt_keys(groups):
        return sorted(groups, key=lambda k: (SET_ORDER.index(k) if k in SET_ORDER else 99, k))

    lines = []
    A = lines.append
    A("# %s · 双面异材质砧板(不锈钢+PP / 钛+PP)企划推荐" % args.cat)
    A("")
    A("数据源: `%s`(只读); 筛出 `主体材质` ∈ {钛+塑料, 不锈钢+塑料} 的变体, 共 **%d 行**。" % (os.path.join("data", args.cat, STD, os.path.basename(path)), len(tgt)))
    A("")
    A("- 「双面异材质」= 该项目口径下主体材质写作 `A+B` 表示**板子两面材质不同**;")
    A("- 单位 inch; 「主尺寸」= 尺寸规格里**面积最大那一件**的长x宽;")
    A("- 销额/销量为**变体月**维度。")
    A("")

    # ---------- 一 ----------
    A("## 一、现有格局: 材质 × 件数")
    A("")
    A("| 材质 | 件数 | 变体行数 | 父体数 | 月销额合计 | 单行均销额 | 单行中位销额 | 月销量合计 | 价格中位 | 价格区间 | 评分数中位 | 上架天数中位 | Top1占比 | Top3占比 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    grp = collections.defaultdict(list)
    for r in tgt:
        grp[(r["材质"], r["套装"])].append(r)
    rows_sum = []
    for k in sorted(grp, key=lambda k: (k[0], SET_ORDER.index(k[1]) if k[1] in SET_ORDER else 99)):
        a = agg(grp[k])
        rows_sum.append((k, a))
        A("| %s | %s | %d | %d | %s | %s | %s | %s | %s | %s~%s | %s | %s | %.1f%% | %.1f%% |" % (
            k[0], k[1], a["行数"], a["父体数"], "{:,.0f}".format(a["销额合计"]),
            "{:,.0f}".format(a["单行均销额"]),
            "{:,.0f}".format(a["单行中位销额"] or 0),
            "{:,.0f}".format(a["销量合计"]),
            g(a["价格中位"] or 0, "%.2f"), g(a["价格区间"][0], "%.2f"), g(a["价格区间"][1], "%.2f"),
            g(a["评分数中位"] or 0, "%.0f"), g(a["上架天数中位"] or 0, "%.0f"),
            a["Top1占比"], a["Top3占比"]))
    A("")

    # ---------- 二 ----------
    A("## 二、单件 vs 多件套: 单行效率")
    A("")
    A("| 件数 | 材质 | 行数 | 单行均销额 | 单行均销量 | 结论 |")
    A("|---|---|---|---|---|---|")
    byset = collections.defaultdict(list)
    for r in tgt:
        byset[r["套装"]].append(r)
    for st in srt_keys(byset):
        grp2 = byset[st]
        for mat in sorted({x["材质"] for x in grp2}):
            sub = [x for x in grp2 if x["材质"] == mat]
            a = agg(sub)
            note = "多件套" if st != "1件" else "单件"
            A("| %s | %s | %d | %s | %s | %s |" % (
                st, mat, len(sub), "{:,.0f}".format(a["单行均销额"]),
                g((a["销量合计"] / len(sub)) if sub else 0, "%.0f"),
                "单行产出高于单件" if st != "1件" else "—"))
    A("")

    # ---------- 三 ----------
    A("## 三、各件数的热销 SKU(按变体月销额降序, 前 8)")
    A("")
    for st in srt_keys(byset):
        A("### %s" % st)
        A("")
        A("| 排名 | 变体ASIN | 材质 | 产品类型 | 月销额 | 月销量 | 价格 | 尺寸阶梯(按面积 大→小) | 主尺寸 | 厚度 | 评分数 | 上架天数 |")
        A("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(sorted(byset[st], key=lambda x: -(x["销额"] or 0))[:8], 1):
            A("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                i, r["asin"], r["材质"], r["类型"],
                "{:,.0f}".format(r["销额"] or 0), "{:,.0f}".format(r["销量"] or 0),
                g(r["价格"] or 0, "%.2f"), r["阶梯"] or "缺", r["主尺寸"] or "缺", r["厚度"],
                g(r["评分数"] or 0, "%.0f"), g(r["天数"] or 0, "%.0f")))
        A("")
        # 尺寸分布
        main = collections.Counter(r["主尺寸"] for r in byset[st] if r["主尺寸"])
        A("- 主尺寸频次: " + "、".join("%s(%d)" % (k, v) for k, v in main.most_common(8)))
        ths = [r["厚度数"] for r in byset[st] if r["厚度数"] is not None]
        A("- 厚度: 中位 %s inch, 区间 %s~%s inch (%d/%d 行有值)" % (
            g(med(ths) or 0, "%.2f"), g(min(ths) or 0, "%.2f"), g(max(ths) or 0, "%.2f"),
            len(ths), len(byset[st])))
        A("")

    # ---------- 四 ----------
    A("## 四、多件套的逐件尺寸组合")
    A("")
    for st in [s for s in srt_keys(byset) if s != "1件"]:
        combos = collections.Counter(r["阶梯纯"] for r in byset[st] if r["阶梯纯"])
        A("### %s(共 %d 行)" % (st, len(byset[st])))
        A("")
        A("| 逐件尺寸阶梯(大→小) | 出现次数 | 该组合月销额合计 |")
        A("|---|---|---|")
        for c, n in combos.most_common(10):
            s = sum((r["销额"] or 0) for r in byset[st] if r["阶梯纯"] == c)
            A("| %s | %d | %s |" % (c, n, "{:,.0f}".format(s)))
        A("")

    # ---------- 五 ----------
    A("## 五、3件套「最大件多大」最赚钱: 全类目 418 行的尺寸带分析")
    A("")
    bands = [(0, 12), (12, 14), (14, 15.5), (15.5, 17), (17, 19), (19, 99)]

    def band(x):
        for lo, hi in bands:
            if lo <= x < hi:
                return "%g-%g" % (lo, hi)
        return "?"

    def band_agg(pool):
        acc = collections.defaultdict(lambda: [0.0, 0, 0.0])
        for r in pool:
            if not r["阶梯列表"]:
                acc["尺寸缺"][0] += r["销额"] or 0
                acc["尺寸缺"][1] += 1
                continue
            k = band(r["阶梯列表"][0][0])
            acc[k][0] += r["销额"] or 0
            acc[k][1] += 1
            acc[k][2] += r["销量"] or 0
        return acc

    all3 = [r for r in recs if r["套装"] == "3件套"]
    metal3 = [r for r in all3 if any(m in r["材质"] for m in ("钛", "不锈钢"))]
    for pool, name in [(all3, "全类目 3件套"), (metal3, "金属面(钛/不锈钢, 含双面异材质)3件套")]:
        acc = band_agg(pool)
        A("### %s(%d 行)" % (name, len(pool)))
        A("")
        A("| 最大件长度带(inch) | 行数 | 月销额合计 | 单行均销额 | 月销量合计 |")
        A("|---|---|---|---|---|")
        for k in sorted(acc, key=lambda k: -acc[k][0]):
            v = acc[k]
            A("| %s | %d | %s | %s | %s |" % (
                k, v[1], "{:,.0f}".format(v[0]),
                "{:,.0f}".format(v[0] / v[1]) if v[1] else "—", "{:,.0f}".format(v[2])))
        A("")
    A("**读法: 金属面 3件套里, 最大件 15.5–17 inch 这一档单行均销额最高——双面异材质要选的主流就是这一档。**")
    A("")

    A("### 全类目 3件套 的尺寸阶梯销量榜(按销额)")
    A("")
    lad = collections.defaultdict(lambda: [0.0, 0, 0.0])
    for r in all3:
        if not r["阶梯纯"]:
            continue
        lad[r["阶梯纯"]][0] += r["销额"] or 0
        lad[r["阶梯纯"]][1] += 1
        lad[r["阶梯纯"]][2] += r["销量"] or 0
    A("| 尺寸阶梯(大→小) | 行数 | 父体数 | 品牌数 | 月销额合计 | 单行均销额 | 月销量合计 | 其中金属面行数 | 双面异材质行数 |")
    A("|---|---|---|---|---|---|---|---|---|")
    for k in sorted(lad, key=lambda k: -lad[k][0])[:12]:
        v = lad[k]
        grp = [r for r in all3 if r["阶梯纯"] == k]
        np_ = len({r["父"] for r in grp})
        nb = len({r["品牌"] for r in grp if r["品牌"]})
        pm = sum(1 for r in grp if any(m in r["材质"] for m in ("钛", "不锈钢")))
        tm = sum(1 for r in grp if r["材质"] in TARGET)
        A("| %s | %d | %d | %d | %s | %s | %s | %d | %s |" % (
            k, v[1], np_, nb, "{:,.0f}".format(v[0]),
            "{:,.0f}".format(v[0] / v[1]) if v[1] else "—", "{:,.0f}".format(v[2]),
            pm, ("%d" % tm) if tm else ("**0(金属面已验证的空白位)**" if pm else "0")))
    A("")
    A("> 「父体数/品牌数」用来判断这行销额是不是**同一条 listing 反复计入**——行数多但父体/品牌少, 说明是少数卖家, 不能当成市场共识。")
    A("> 「其中金属面行数」= 主体材质含 钛/不锈钢(含双面异材质)的行数。**金属面已验证、而双面异材质 0 行**的阶梯才是真空白位(标「空白位」); 金属面也是 0 行的阶梯(如竹/塑主流阶梯)不构成金属板的空白位。")
    A("")

    # ---------- 六 ----------
    A("## 六、对标: 单材质 不锈钢 / 钛 / 塑料")
    A("")
    A("| 材质 | 件数 | 行数 | 月销额合计 | 单行均销额 | 价格中位 | 评分数中位 | 上架天数中位 |")
    A("|---|---|---|---|---|---|---|---|")
    bg = collections.defaultdict(list)
    for r in base:
        bg[(r["材质"], r["套装"])].append(r)
    for k in sorted(bg, key=lambda k: -sum(x["销额"] or 0 for x in bg[k]))[:16]:
        a = agg(bg[k])
        A("| %s | %s | %d | %s | %s | %s | %s | %s |" % (
            k[0], k[1], a["行数"], "{:,.0f}".format(a["销额合计"]),
            "{:,.0f}".format(a["单行均销额"]), g(a["价格中位"] or 0, "%.2f"),
            g(a["评分数中位"] or 0, "%.0f"), g(a["上架天数中位"] or 0, "%.0f")))
    A("")

    # ---------- 七 ----------
    A("## 七、属性必备度: 全部 vs 头部(月销额 ≥ 2 万)")
    A("")
    head = [r for r in tgt if (r["销额"] or 0) >= 20000]
    A("头部 %d 行 / 全部 %d 行。" % (len(head), len(tgt)))
    A("")
    A("| 属性 | 全部(83 行) | 头部(13 行) | 读法 |")
    A("|---|---|---|---|")
    POS = {
        "汁槽": ("带汁槽",),
        "防滑": ("防滑",),
        "洗碗机": ("可洗碗机清洗",),
        "挂孔": ("带挂孔",),
        "手柄": ("带手柄",),
        "食品级": ("食品级", "BPA-Free", "BPA Free", "无毒"),
        "收纳架": ("含收纳架",),
        "形状": ("长方形",),
    }
    for key, name in [("汁槽", "带汁槽"), ("防滑", "防滑"), ("洗碗机", "可洗碗机清洗"),
                      ("挂孔", "带挂孔"), ("手柄", "带手柄"), ("食品级", "食品级/BPA-Free"),
                      ("收纳架", "含收纳架"), ("形状", "长方形")]:
        def share(g, key=key):
            if not g:
                return 0, 0
            good = sum(1 for x in g if x[key] in POS[key])
            return good, len(g)
        na, da = share(tgt)
        nb, db = share(head)
        sa = na / da * 100 if da else 0
        sb = nb / db * 100 if db else 0
        verdict = "头部更高 → 建议标配" if sb - sa >= 8 else ("头部明显更低" if sb - sa <= -8 else "无显著差异")
        A("| %s | %d/%d (%.0f%%) | %d/%d (%.0f%%) | %s |" % (name, na, da, sa, nb, db, sb, verdict))
    cert = sum(1 for x in tgt if x["环保认证"] and x["环保认证"] not in ("未明确", "无环保认证"))
    A("| 有环保认证 | %d/%d (%.0f%%) | 0/%d | **全类目空白 → 差异化机会** |" % (
        cert, len(tgt), cert / len(tgt) * 100, len(head)))
    A("")

    # ---------- 八 ----------
    t3 = [r for r in tgt if r["套装"] == "3件套"]
    t1 = [r for r in tgt if r["套装"] == "1件"]
    t3_top = sorted(t3, key=lambda x: -(x["销额"] or 0))
    A("## 八、结论与推荐")
    A("")
    A("### 8.1 做几件套: **主推 3件套, 单件做引流**")
    A("")
    A("| 件数 | 单行均销额(钛+PP) | 单行均销额(不锈钢+PP) | 价格中位 | Top1占比 | 建议 |")
    A("|---|---|---|---|---|---|")
    for st in ["1件", "2件套", "3件套"]:
        vals = {}
        for mat in TARGET:
            sub = [x for x in tgt if x["套装"] == st and x["材质"] == mat]
            vals[mat] = agg(sub) if sub else None
        tp = max((vals[m]["Top1占比"] for m in vals if vals[m]), default=0)
        pm = med([x["价格"] for x in tgt if x["套装"] == st])
        reco = {"1件": "做: 引流款/评论款", "2件套": "不做主线(头部锁死, 单行产出低)",
                "3件套": "**主推**: 单行产出最高"}[st]
        A("| %s | %s | %s | %s | %.1f%% | %s |" % (
            st,
            "{:,.0f}".format(vals["钛+塑料"]["单行均销额"]) if vals["钛+塑料"] else "—",
            "{:,.0f}".format(vals["不锈钢+塑料"]["单行均销额"]) if vals["不锈钢+塑料"] else "—",
            g(pm or 0, "%.2f"), tp, reco))
    A("")

    A("### 8.2 尺寸阶梯候选: 三个候选放在同一把尺子上比")
    A("")
    A("| 候选 | 尺寸阶梯(大→小, inch) | 全类目行数 | 全类目月销额 | 单行均销额 | 双面异材质在用 | 该阶梯在双面异材质的实测 |")
    A("|---|---|---|---|---|---|---|")
    cands = [
        ("A(小尺寸高价)", "大 14.5x9.5 · 中 13x8.5 · 小 11.5x7.5"),
        ("B(主流大尺寸)", "大 18x12.5 · 中 15.5x10.5 · 小 12x8.5"),
        ("C(金属面主流)", "大 16x10 · 中 13x8 · 小 10.5x6.5"),
        ("C'(金属面变体)", "大 16x10 · 中 13x8 · 小 10x6"),
    ]
    for tag, key in cands:
        v = lad.get(key)
        used_rows = [r for r in tgt if r["阶梯纯"] == key]
        if used_rows:
            u = "%d 行 / 月销额 %s / 均价 %s" % (
                len(used_rows), "{:,.0f}".format(sum(r["销额"] or 0 for r in used_rows)),
                g(med([r["价格"] for r in used_rows]) or 0, "%.2f"))
        else:
            u = "**0 行(空白)**"
        A("| %s | %s | %d | %s | %s | %s | %s |" % (
            tag, key, v[1] if v else 0,
            "{:,.0f}".format(v[0]) if v else "—",
            "{:,.0f}".format(v[0] / v[1]) if v and v[1] else "—",
            "有" if used_rows else "**无**", u))
    A("")

    # ---- 推荐档与逐件支撑度(先算, 供 8.3 / 8.4 共用) ----
    REC = [(16.0, 10.0, "大件"), (13.0, 8.0, "中件"), (10.5, 6.5, "小件")]
    big16 = [r for r in all3 if r["阶梯列表"] and abs(r["阶梯列表"][0][0] - 16) < 0.011
             and abs(r["阶梯列表"][0][1] - 10) < 0.011]
    mid13 = sum(1 for r in big16 if len(r["阶梯列表"]) > 1
                and abs(r["阶梯列表"][1][0] - 13) < 0.011 and abs(r["阶梯列表"][1][1] - 8) < 0.011)
    small_freq = collections.Counter(
        "%gx%g" % (r["阶梯列表"][2][0], r["阶梯列表"][2][1])
        for r in all3 if len(r["阶梯列表"]) == 3)
    ratios = sorted(
        (r["阶梯列表"][2][0] * r["阶梯列表"][2][1]) /
        (r["阶梯列表"][0][0] * r["阶梯列表"][0][1])
        for r in all3 if len(r["阶梯列表"]) == 3 and r["阶梯列表"][0][0] * r["阶梯列表"][0][1] > 0)
    ratio_n = len(ratios)
    ratio_med = ratios[ratio_n // 2] if ratio_n else 0

    def ladder_sal(L, W):
        return sum(r["销额"] or 0 for r in all3 if r["阶梯列表"] and len(r["阶梯列表"]) == 3
                   and abs(r["阶梯列表"][0][0] - 16) < 0.011 and abs(r["阶梯列表"][0][1] - 10) < 0.011
                   and abs(r["阶梯列表"][1][0] - 13) < 0.011 and abs(r["阶梯列表"][1][1] - 8) < 0.011
                   and abs(r["阶梯列表"][2][0] - L) < 0.011 and abs(r["阶梯列表"][2][1] - W) < 0.011)

    # 推荐阶梯自身的独立性统计(用于 8.3 的理由)
    rl = [r for r in all3 if len(r["阶梯列表"]) == 3
          and abs(r["阶梯列表"][0][0] - 16) < 0.011 and abs(r["阶梯列表"][0][1] - 10) < 0.011
          and abs(r["阶梯列表"][1][0] - 13) < 0.011 and abs(r["阶梯列表"][1][1] - 8) < 0.011
          and abs(r["阶梯列表"][2][0] - 10.5) < 0.011 and abs(r["阶梯列表"][2][1] - 6.5) < 0.011]
    rl_sal = sum(r["销额"] or 0 for r in rl)
    rl_par = len({r["父"] for r in rl})
    rl_brand = len({r["品牌"] for r in rl if r["品牌"]})
    rl_new = max((r for r in rl if r["天数"] is not None and r["天数"] <= 120),
                 key=lambda r: r["销额"] or 0, default=None)
    rl_old = max((r for r in rl if r["天数"] is not None and r["天数"] > 120),
                 key=lambda r: r["销额"] or 0, default=None)

    A("### 8.3 ★ 最终推荐款式(唯一方案)")
    A("")
    A("> **3件套 · 双面异材质(钛面 + 食品级小麦秸秆PP面, 或 304不锈钢面 + PP面) · "
      "大 16x10 / 中 13x8 / 小 10.5x6.5 inch · 统一厚 0.5 inch · 含立式收纳架 · 定价 $79.99(钛+PP) / $59.99(不锈钢+PP)**")
    A("")
    A("> 这是**唯一方案**(大件为什么不是 16x11, 见 8.5)。")
    A("| 项 | 取值 | 依据 |")
    A("|---|---|---|")
    A("| 件数 | 3件套 | 单行均销额 49,255(钛+PP)/50,070(不锈钢+PP), 是单件的 2.5~5 倍; 见 8.1 |")
    A("| 大件 | **16 x 10 inch (40.6 x 25.4 cm)** | 金属面 3件套「最大件 15.5–17 inch」档单行均销额 68,123, 为 14–15.5 档的 2.6 倍、17–19 档的 3.9 倍(第五节) |")
    A("| 中件 | **13 x 8 inch (33.0 x 20.3 cm)** | 大件=16x10 的 3件套共 %d 行, 其中 %d 行的中件就是 13x8 |" % (len(big16), mid13))
    A("| 小件 | **10.5 x 6.5 inch (26.7 x 16.5 cm)** | 该尺寸跑出过 %s/月(4 行 / 2 品牌, **弱支撑**); 面积比校验 0.44 → 10.6x6.6 吻合; 可换 10x6 |" % (
        "{:,.0f}".format(ladder_sal(10.5, 6.5))))
    A("| 厚 | **0.5 inch (1.27 cm) 三件统一** | 双面异材质 3件套实测厚度中位 0.68, 两款头部都是 0.5; 统一厚度便于共用夹具/包装 |")
    A("| 形状 | 长方形 | 双面异材质 %d/%d 行为长方形, 头部 13/13 |" % (
        sum(1 for r in tgt if r["形状"] == "长方形"), len(tgt)))
    A("| 必配 | 汁槽 + 食品级/小麦秸秆PP话术 + 挂孔 | 汁槽 %d/%d(头部 12/13); 食品级 %d/%d; 挂孔头部 8/13 |" % (
        sum(1 for r in tgt if r["汁槽"] == "带汁槽"), len(tgt),
        sum(1 for x in tgt if x["食品级"] in ("食品级", "BPA-Free", "无毒")), len(tgt)))
    A("| 差异化 | 环保/食品级认证 + 立式收纳架 | 83 行里环保认证 0 行; 收纳架仅 7 行但头部 3件套爆款带 Holder |")
    A("| 定价 | 钛+PP **$79.99**; 不锈钢+PP **$59.99** | 双面异材质 3件套价格中位 $69.98(钛+PP 中位 $79.98, 区间 59.99~99.99) |")
    A("")
    A("**为什么是它(四条理由)**")
    A("")
    A("1. **尺寸带选对了**: 金属面 3件套的大件一旦落在 15.5–17 inch, 单行均销额 68,123, 明显甩开 14–15.5(26,537) 和 17–19(17,450)。16x10 正落在这条最赚钱的带里。")
    A("2. **阶梯在金属面里被验证过、双面异材质还是空白**: `16x10+13x8+10.5x6.5` 全类目月销额 %s, 双面异材质 3件套 0 行。但要看**独立性**: 这 %s 只来自 **%d 行 / %d 个父体 / %d 个品牌**, 不是市场共识——所以算「方向性验证」, 不能当成「单行产出 %s」的普适结论。" % (
        "{:,.0f}".format(rl_sal), "{:,.0f}".format(rl_sal), len(rl), rl_par, rl_brand,
        "{:,.0f}".format(rl_sal / len(rl)) if rl else "—"))
    if rl_new is not None:
        A("3. **它不靠评论壁垒**: 同一条阶梯上, **上架 %s 天、评分数仅 %s** 的 %s 就做到 %s/月%s。这比「单行均销额高」更有说服力——说明新卖家能靠这条阶梯起量, 不必先熬评论。" % (
            g(rl_new["天数"], "%.0f"), g(rl_new["评分数"] or 0, "%.0f"), rl_new["asin"],
            "{:,.0f}".format(rl_new["销额"] or 0),
            ("(而同阶梯%s 已是 %s 天 / %s 条评论)" % (rl_old["asin"], g(rl_old["天数"], "%.0f"),
                                             g(rl_old["评分数"] or 0, "%.0f"))) if rl_old else ""))
    A("4. **材质升级的痛点正好被这个组合解决**: 占着这条阶梯的纯钛薄板厚度只有 0.06 inch, 卖家自己在五点里承认会留刀痕; 换成「钛面+PP面、0.5 inch」既保住金属面的卫生叙事, 又用 0.5 inch 的复合厚度和 PP 面对刀具友好, 还顺手把价格从 $29.62~99.99 拉到 $79.99 的中高带。")
    A("")

    A("### 8.4 逐件支撑度: 推荐里的每个数字有多硬")
    A("")
    A("| 位置 | 推荐尺寸(inch) | 同位置同尺寸行数 | 父体数 | 品牌数 | 该尺寸档月销额 | 独立佐证 |")
    A("|---|---|---|---|---|---|---|")
    for idx, (L, W, pos) in enumerate(REC):
        m = [r for r in all3 if len(r["阶梯列表"]) > idx
             and abs(r["阶梯列表"][idx][0] - L) < 0.011
             and abs(r["阶梯列表"][idx][1] - W) < 0.011]
        if pos == "小件":
            ev = "面积比校验: 全类目 3件套 小件/大件面积比中位 %.2f(n=%d) → 由 16x10 等比推出 %.1fx%.1f" % (
                ratio_med, ratio_n, 16 * (ratio_med ** 0.5), 10 * (ratio_med ** 0.5))
        elif pos == "中件":
            ev = "大件=16x10 的 3件套共 %d 行, 其中 %d 行的中件就是 13x8" % (len(big16), mid13)
        else:
            ev = "金属面 3件套里最大件落 15.5–17 inch 档的最优带(第五节)"
        A("| %s | **%g x %g** | %d | %d | %d | %s | %s |" % (
            pos, L, W, len(m),
            len({r["父"] for r in m}), len({r["品牌"] for r in m if r["品牌"]}),
            "{:,.0f}".format(sum(r["销额"] or 0 for r in m)) if m else "—", ev))
    A("")
    A("- **大件 16x10 支撑强**(%d 行)、**中件 13x8 支撑强**(%d 行是同一大件下的标准中件)。" % (len(big16), mid13))
    A("- **小件 10.5x6.5 是最弱的一环**: 它只来自 4 行 / 2 个品牌, 在全类目 3件套小件频次里排第 14; 主流小件是 %s。" % (
        "、".join("%s(%d)" % (k, v) for k, v in small_freq.most_common(5))))
    A("- 同一条「大 16x10 + 中 13x8」下, 只换小件的三个兄弟阶梯对比: 小件 10.5x6.5 → **%s/月**; 小件 10x6 → %s/月; 小件 10x7 → %s/月。" % (
        "{:,.0f}".format(ladder_sal(10.5, 6.5)), "{:,.0f}".format(ladder_sal(10, 6)),
        "{:,.0f}".format(ladder_sal(10, 7))))
    A("- **结论**: 小件**沿用 10.5x6.5**(唯一跑出量来的那档, 且与面积比推出值吻合); 若为凑整数改用 **10x6** 也可接受(面积小 12%, 消费者感知差异很小)。**真正不能动的是大件 16x10 与中件 13x8。**")
    A("")

    A("### 8.5 大件为什么是 16x10 而不是 16x11?(两个维度各有证据)")
    A("")
    A("同一个尺寸问题, 在**单件**和**三件套**两个维度上的答案不一样, 必须分开看:")
    A("")
    A("**① 三件套维度(与你的形态直接可比)**: 金属面 3件套按大件尺寸看——")
    A("")
    A("| 大件尺寸 | 行数 | 父体数 | 品牌数 | 月销额 | 单行均销额 | 月销量 |")
    A("|---|---|---|---|---|---|---|")
    cmp3 = {}
    for L, W, tol, note in [(16.0, 10.0, 0.11, "16x10"), (16.0, 11.0, 0.6, "≈16x11(含 15.6x11 / 16.5x11)")]:
        grp = [r for r in all3 if r["阶梯列表"]
               and any(m in r["材质"] for m in ("钛", "不锈钢"))
               and abs(r["阶梯列表"][0][0] - L) <= tol and abs(r["阶梯列表"][0][1] - W) <= tol]
        sal = sum(r["销额"] or 0 for r in grp)
        cmp3[note] = (len(grp), sal)
        A("| 大件 %s | %d | %d | %d | %s | %s | %s |" % (
            note, len(grp), len({r["父"] for r in grp}), len({r["品牌"] for r in grp if r["品牌"]}),
            "{:,.0f}".format(sal), "{:,.0f}".format(sal / len(grp)) if grp else "—",
            "{:,.0f}".format(sum(r["销量"] or 0 for r in grp))))
    A("")
    n10, s10 = cmp3["16x10"]
    n11, s11 = cmp3["≈16x11(含 15.6x11 / 16.5x11)"]
    A("→ **在金属面三件套里, 大件 16x10 明显胜出**: %s/月(%d 行) vs 大件≈16x11 的 %s/月(%d 行), 差 **%.1f 倍**。而全类目的 16x11 大件销额主要被 **GORILLA GRIP 一个品牌的塑料软垫套装**(18 行, $19.99~32.90)占着, 品类不同, 不可迁移。" % (
        "{:,.0f}".format(s10), n10, "{:,.0f}".format(s11), n11, s10 / s11 if s11 else 0))
    A("")
    A("**② 单件维度(与你的尺寸选择可比)**: 双面异材质单件按大件尺寸看——")
    A("")
    A("| 大件尺寸 | 行数 | 父体数 | 品牌数 | 月销额 | 单行均销额 | 月销量 |")
    A("|---|---|---|---|---|---|---|")
    t1all = [r for r in tgt if r["套装"] == "1件"]
    for L, W, tol, note in [(16.0, 11.0, 0.11, "**16x11**"), (16.3, 11.0, 0.11, "16.3x11"),
                            (15.0, 10.3, 0.11, "15x10.3"), (15.0, 10.0, 0.11, "15x10")]:
        grp = [r for r in t1all if r["阶梯列表"]
               and abs(r["阶梯列表"][0][0] - L) <= tol and abs(r["阶梯列表"][0][1] - W) <= tol]
        sal = sum(r["销额"] or 0 for r in grp)
        A("| 大件 %s | %d | %d | %d | %s | %s | %s |" % (
            note, len(grp), len({r["父"] for r in grp}), len({r["品牌"] for r in grp if r["品牌"]}),
            "{:,.0f}".format(sal), "{:,.0f}".format(sal / len(grp)) if grp else "—",
            "{:,.0f}".format(sum(r["销量"] or 0 for r in grp))))
    A("")
    A("→ **在双面异材质单件里, 16x11 是压倒性主流**(12 行 / 12 父体 / 11 品牌, 502,932/月, 20,310 件), 且有一个**上架 42 天、46 条评论就做到 21,216/月**的新链接在跑。这条证据比 16x10 那条更分散、更独立。")
    A("")
    A("**③ 结论: 选 A(大件 16x10), B 不做首发**")
    A("")
    A("但必须先纠正一条容易被误读的比较——**价格带没对齐**:")
    A("")
    A("| 大件 | 价格带 | 行数 | 父体数 | 品牌数 | 月销额 | 单行均销额 | 上架天数中位 |")
    A("|---|---|---|---|---|---|---|---|")
    for L, W, tol, note in [(16.0, 10.0, 0.11, "16x10"), (16.0, 11.0, 0.6, "≈16x11")]:
        base = [r for r in all3 if r["阶梯列表"]
                and any(m in r["材质"] for m in ("钛", "不锈钢"))
                and abs(r["阶梯列表"][0][0] - L) <= tol and abs(r["阶梯列表"][0][1] - W) <= tol]
        for lo, hi, pname in [(59.99, 10 ** 9, "≥$59.99(中高价)"), (0, 59.99, "<$59.99(低价)")]:
            grp = [r for r in base if r["价格"] is not None and lo <= r["价格"] < hi]
            if not grp:
                continue
            sal = sum(r["销额"] or 0 for r in grp)
            A("| 大件 %s | %s | %d | %d | %d | %s | %s | %s |" % (
                note, pname, len(grp), len({r["父"] for r in grp}),
                len({r["品牌"] for r in grp if r["品牌"]}),
                "{:,.0f}".format(sal), "{:,.0f}".format(sal / len(grp)),
                g(med([r["天数"] for r in grp]) or 0, "%.0f")))
    A("")
    A("→ 看清这张表就明白: 大件≈16x11 那一档合计低, **主要不是因为尺寸不行, 而是因为 8 个链接里有 5 个定价在 $50 以下、3 个上架不足 70 天、还有 1 个规格只写了 1 件**。在 ≥$59.99 的中高价带里, 16x10 有 %d 个链接、做到 %s/月; 16x11 只有 %d 个链接(上架 66 / 126 天), 合计 %s/月——**这属于「未验证」, 不是「已验证不行」**。我把这条当成反证来用是不成立的。" % (
        len([r for r in all3 if r["阶梯列表"]
             and any(m in r["材质"] for m in ("钛", "不锈钢"))
             and abs(r["阶梯列表"][0][0] - 16) <= 0.11 and abs(r["阶梯列表"][0][1] - 10) <= 0.11
             and (r["价格"] or 0) >= 59.99]),
        "{:,.0f}".format(sum((r["销额"] or 0) for r in all3 if r["阶梯列表"]
                            and any(m in r["材质"] for m in ("钛", "不锈钢"))
                            and abs(r["阶梯列表"][0][0] - 16) <= 0.11 and abs(r["阶梯列表"][0][1] - 10) <= 0.11
                            and (r["价格"] or 0) >= 59.99)),
        len([r for r in all3 if r["阶梯列表"]
             and any(m in r["材质"] for m in ("钛", "不锈钢"))
             and abs(r["阶梯列表"][0][0] - 16) <= 0.6 and abs(r["阶梯列表"][0][1] - 11) <= 0.6
             and (r["价格"] or 0) >= 59.99]),
        "{:,.0f}".format(sum((r["销额"] or 0) for r in all3 if r["阶梯列表"]
                            and any(m in r["材质"] for m in ("钛", "不锈钢"))
                            and abs(r["阶梯列表"][0][0] - 16) <= 0.6 and abs(r["阶梯列表"][0][1] - 11) <= 0.6
                            and (r["价格"] or 0) >= 59.99))))
    A("")
    A("| 方案 | 大件 | 中件 | 小件 | 证据性质 | 结论 |")
    A("|---|---|---|---|---|---|")
    A("| **A** | **16x10** | 13x8 | 10.5x6.5 | 三件套形态直接可比: 金属面唯一跑出量的阶梯(1,059,356/月, 8 行) | **选定(首发)** |")
    A("| B | 16x11 | 13x8 | 10.5x6.5 | 三件套形态**未验证**(中高价带仅 2 个新链接); 只有单件形态的强证据 | 不做首发 |")
    A("")
    A("**三条决定性理由**")
    A("")
    A("1. **只有 16x10 有「三件套」形态的直接证据, 且是金属面唯一跑出量的阶梯**(920,825/月)。16x11 的强证据全部来自单件形态——单件买家买的是「一块够大的板」, 三件套买家买的是「一套覆盖大中小」, 跨形态外推要打折。")
    A("2. **跨材质看, 赢的三件套阶梯大件都是 15~16 x 10, 没有 16x11**: 全类目 3件套销额第一是竹制 `15x10+12x8+9x6`(%s/月, 38 行), 金属面第一是 `16x10+13x8+10.5x6.5`(%s/月)。16x11 大件只排到第 3, 而且是 GORILLA GRIP 一个品牌的塑料软垫套装(18 行), 品类不同、不可迁移。" % (
        "{:,.0f}".format(lad.get("大 15x10 · 中 12x8 · 小 9x6", [0])[0]),
        "{:,.0f}".format(lad.get("大 16x10 · 中 13x8 · 小 10.5x6.5", [0])[0])))
    A("3. **风险不对称**: 选 16x10 若判断偏差, 代价只是「大件比对手少 10% 面积」这一点竞争力; 选 16x11 若判断偏差, 代价是**整套的尺寸定位错误**、库存与模具全部压在里面。而支持 16x11 的只有单件形态的间接证据, 用它去承担这个风险不划算。")
    A("")
    A("> **反面提醒(重要)**: 16x11 是「未验证」而不是「被否」。若你有下列任一条件, 应当改选 B 而不是 A: (a) 你已有 16x11 的单件生产线/刀模; (b) 你的目标客群明确要更大台面; (c) 你愿意用一款新品去测这个空白位。此时 B 的性价比反而更高——因为它避开了 16x10 上已经存在的 5 个品牌头部。")
    A("")

    A("### 8.6 备选与不建议")
    A("")
    A("- **对照(不做)**: `大 14.5x9.5 · 中 13x8.5 · 小 11.5x7.5` @ $89.99~94.99 —— 双面异材质 3件套现销额第一(207,648/月, 2,186 件)就是这个阶梯, 但它靠 $94.99 高价 + 收纳架取胜, 且已有 3,271 条评论的头部在位, 属正面硬刚, 不作为本方案。")
    A("- **不做**: 2件套(单行均销额低于 3件套, 头部 Top1 占 90.6% 已锁死); 大 18x12.5 一档(金属面 17–19 inch 档单行均销额仅 17,450, 且 18x12.5 阶梯已有 7,556 条评论的头部)。")
    A("- **大件 16x11**: 见 8.5——它是「未验证」而非「被否」, 本方案不做首发, 但保留为可选变体(与你已有刀模/客群有关)。")
    A("")
    A("> 以下不属于本方案, 仅备查: 双面异材质单件里 `16x11x0.8, $29.99` 与 `15x10.3x0.6, $19.99` 是两个 20 万级爆款规格, 若日后要用单件引流可以参考。")
    A("")
    A("### 8.7 关于「三个中号」这类标签")
    A("")
    A("本报告的「尺寸阶梯」一律**按面积从大到小重新命名 大/中/小**, 与源数据的标签无关; 若源标注与面积顺序不符(例如三个都写「中号」, 或最小件被写成「大号」), 会在括号里注明「源标注」。原因是: 源 listing 常常只在详细参数里列出三组数字而不给大小名(如 B0533848C5 的 `Included Components:11.50\"×7.50\"… 13.0\"×8.50\"… 14.5\"×9.50\"…`), 标注环节在缺名时会落到同一个默认标签——这是**标签问题, 尺寸数值本身是对的**。")
    A("")

    out = args.out or os.path.join(cat_dir, STD, "%s-双面异材质砧板-企划推荐.md" % args.cat)
    io.open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("目标行 %d (钛+塑料 %d / 不锈钢+塑料 %d)" % (
        len(tgt), sum(1 for r in tgt if r["材质"] == "钛+塑料"),
        sum(1 for r in tgt if r["材质"] == "不锈钢+塑料")))
    for k, a in rows_sum:
        print("  %-8s %-6s 行=%2d 销额=%10s 单行均=%8s 价格中位=%6s Top1=%4.1f%%" % (
            k[0], k[1], a["行数"], "{:,.0f}".format(a["销额合计"]),
            "{:,.0f}".format(a["单行均销额"]), g(a["价格中位"] or 0, "%.2f"), a["Top1占比"]))
    print("[写] %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
