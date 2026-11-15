# -*- coding: utf-8 -*-
"""功能家族轴测试 —— 判断一个"功能"列是把市场切成互斥产品家族, 还是只是同族内的加分项。

判据(跨 4 个类目实测校准过):
  · 品牌共享度 = |共享品牌| / min(|A品牌|,|B品牌|)
        低(<0.3) 是**强信号**(不同厂家走不同技术路线)
  · 价格重叠度 = 两水平价格直方图的重叠面积占比
        会被"同族内还有低端/高端"污染(实测: HEPA过滤器 $229.99 vs HEPA $107.97 本是同族)
        → 只作辅助证据, 不单独判定
  · **必须在归并之后做**(HEPA / True HEPA / H13 HEPA / HEPA过滤器 本是同一水平)

用法:
  python scripts/probe_family_axis.py --cat "Salt and Pepper Mill Sets-US-202608" --col operation_mode
"""
from __future__ import annotations

import argparse
import collections
import io
import os

import openpyxl

REV = "变体月销额"


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def overlap(a, b, width=2.0):
    if not a or not b:
        return None
    lo, hi = min(min(a), min(b)), max(max(a), max(b))
    if hi <= lo:
        return 1.0
    nb = max(4, int((hi - lo) / width) + 1)

    def hist(xs):
        h = [0] * nb
        for x in xs:
            h[min(nb - 1, int((x - lo) / (hi - lo) * nb))] += 1
        s = sum(h) or 1
        return [v / s for v in h]
    return sum(min(x, y) for x, y in zip(hist(a), hist(b)))


def analyze(cat, col, min_rows=20, root="."):
    p = os.path.join(root, "data", cat, "%s-产品属性标注.xlsx" % cat)
    if not os.path.exists(p):
        p = os.path.join(root, "data", cat, "18_标准化", "%s-属性标注-标准化交付.xlsx" % cat)
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    ws = wb["产品属性标注"] if "产品属性标注" in wb.sheetnames else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    if col not in hdr:
        return ["  ! 无此列: %s" % col]
    j, jr = hdr.index(col), hdr.index(REV)
    jp = hdr.index("价格($)") if "价格($)" in hdr else None
    jb = hdr.index("品牌") if "品牌" in hdr else None
    by = collections.defaultdict(lambda: {"price": [], "rev": 0.0, "brand": set(), "n": 0})
    for r in rows[1:]:
        v = str(r[j]).strip() if j < len(r) and r[j] is not None else ""
        if v in ("", "None", "nan", "未明确"):
            continue
        g = by[v]
        g["n"] += 1
        g["rev"] += (num(r[jr]) or 0) if jr < len(r) else 0
        if jp is not None and jp < len(r) and num(r[jp]):
            g["price"].append(num(r[jp]))
        if jb is not None and jb < len(r) and r[jb]:
            g["brand"].add(str(r[jb]).strip())
    levels = sorted(((k, v) for k, v in by.items() if v["n"] >= min_rows),
                    key=lambda kv: -kv[1]["n"])
    out = ["== %s · 「%s」: %d 个水平(行数≥%d)" % (cat, col, len(levels), min_rows)]
    for k, v in levels:
        ps = sorted(v["price"])
        out.append("   %-16s 行=%4d 销额=%12s 价格中位=%s 品牌=%d" % (
            k[:16], v["n"], "{:,.0f}".format(v["rev"]),
            "%.2f" % ps[len(ps) // 2] if ps else "—", len(v["brand"])))
    for i in range(len(levels)):
        for k2 in range(i + 1, len(levels)):
            a, b = levels[i], levels[k2]
            ov = overlap(a[1]["price"], b[1]["price"])
            shr = len(a[1]["brand"] & b[1]["brand"]) / max(
                1, min(len(a[1]["brand"]), len(b[1]["brand"])))
            if shr < 0.3:
                v = "→ **家族轴候选**(品牌几乎不共享)"
            elif ov is not None and ov > 0.7:
                v = "→ 同族内水平(可作枚举/阶梯行)"
            else:
                v = "→ 需人工判"
            out.append("   %s vs %s: 价格重叠=%s 品牌共享=%.2f %s" % (
                a[0][:10], b[0][:10], "%.2f" % ov if ov is not None else "—", shr, v))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True)
    ap.add_argument("--col", required=True)
    ap.add_argument("--min-rows", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = "\n".join(analyze(args.cat, args.col, args.min_rows, root))
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
