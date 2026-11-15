# -*- coding: utf-8 -*-
"""只读：列出「产品类型 = 配件/耗材」的 31 行及其证据，供逐条判断是否真耗材。"""
from __future__ import annotations

import csv
import os

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(h) for h in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]        # i+2 = Excel 行号
C = {h: i for i, h in enumerate(hdr)}


def g(r, name):
    i = C.get(name)
    if i is None or i >= len(r) or r[i] is None:
        return ""
    return str(r[i]).strip()


# ---- 属性证据审计里 产品类型 的证据
aud = {}
ws2 = wb["属性证据审计"]
it2 = ws2.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
for r in it2:
    if len(r) <= max(C2.values()):
        continue
    if str(r[C2["属性字段"]]).strip() != "产品类型":
        continue
    asin = str(r[C2["变体ASIN"]] or "").strip()
    ev = " | ".join(str(r[C2[k]] or "").strip()
                    for k in ("提取值", "证据原文", "证据来源", "证据定位", "置信度")
                    if k in C2)
    aud.setdefault(asin, []).append(ev)

wb.close()

picks = [(n, r) for n, r in rows if g(r, "产品类型") == "配件/耗材"]
print("配件/耗材 共 %d 行\n" % len(picks))

out = os.path.join("scripts", "_catfeeder_配件耗材_逐行.csv")
with open(out, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Excel行", "父ASIN", "变体ASIN", "商品标题", "类目路径", "小类目",
                "月销量", "价格USD", "实际拆分数量", "容量L", "喂食机制",
                "产品卖点", "属性证据摘要", "产品类型证据"])
    for n, r in picks:
        w.writerow([n, g(r, "父 ASIN"), g(r, "变体 ASIN"), g(r, "商品标题"),
                    g(r, "类目路径"), g(r, "小类目"), g(r, "月销量（件/月）"),
                    g(r, "价格（USD）"), g(r, "实际拆分数量（个）"),
                    g(r, "容量（L）"), g(r, "喂食机制"),
                    g(r, "产品卖点")[:300], g(r, "属性证据摘要")[:300],
                    " || ".join(aud.get(g(r, "变体 ASIN"), []))[:300]])
print("已导出逐行明细 ->", out, "\n")

for n, r in picks:
    print("=" * 100)
    print("Excel行 %d | 父 %s | 变体 %s | 月销 %s | 价 %s | 拆分 %s"
          % (n, g(r, "父 ASIN"), g(r, "变体 ASIN"), g(r, "月销量（件/月）"),
             g(r, "价格（USD）"), g(r, "实际拆分数量（个）")))
    print("  标题  : %s" % g(r, "商品标题")[:190])
    print("  类目  : %s" % g(r, "类目路径")[:150])
    print("  小类目: %s" % g(r, "小类目"))
    print("  卖点  : %s" % g(r, "产品卖点")[:190])
    print("  摘要  : %s" % g(r, "属性证据摘要")[:190])
    ev = aud.get(g(r, "变体 ASIN"), [])
    print("  类型证据: %s" % (ev[0][:190] if ev else "(无)"))
