# -*- coding: utf-8 -*-
"""只读：核实「产品类型 = 其他非自动喂食用品」的行到底是不是喂食器。"""
from __future__ import annotations

import os

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(h) for h in next(it)]
C = {h: i for i, h in enumerate(hdr)}
rows = [(i + 2, r) for i, r in enumerate(it)]

picks = [(n, r) for n, r in rows if str(r[C["产品类型"]] or "").strip() == "其他非自动喂食用品"]
print("「其他非自动喂食用品」共 %d 行\n" % len(picks))

# 审计表里该分类的历史条数
aud = wb["属性证据审计"]
it2 = aud.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
aud_rows = [r for r in it2]
wb.close()

from collections import Counter
cnt = Counter(str(r[C2["提取值"]] or "").strip() for r in aud_rows
              if str(r[C2["属性字段"]] or "").strip() == "产品类型")
print("属性证据审计 中 产品类型 的取值分布（历史全量）:")
for k, v in cnt.most_common():
    print("   %-22s %4d" % (k or "(空)", v))

for n, r in picks:
    print("\n" + "=" * 100)
    print("Excel行 %d | 父 %s | 变体 %s" % (n, r[C["父 ASIN"]], r[C["变体 ASIN"]]))
    for k in ("商品标题", "类目路径", "小类目", "月销量（件/月）", "价格（USD）",
              "实际拆分数量（个）", "变体数（个）", "产品类型", "喂食机制",
              "容量（L）", "商品尺寸（cm，长×宽×高）", "商品重量（kg）",
              "实时Listing状态", "ASIN核验"):
        if k in C:
            v = r[C[k]]
            print("  %-18s: %s" % (k, ("" if v is None else str(v))[:160]))
    for k in ("产品卖点", "属性证据摘要", "图片理解结论", "图片核心卖点", "详细参数"):
        if k in C:
            v = r[C[k]]
            print("  %s: %s" % (k, ("" if v is None else str(v))[:400]))
    asin = str(r[C["变体 ASIN"]] or "").strip()
    ev = [x for x in aud_rows
          if str(x[C2["属性字段"]] or "").strip() == "产品类型"
          and str(x[C2["变体ASIN"]] or "").strip() == asin]
    print("  类型证据 %d 条:" % len(ev))
    for e in ev:
        print("     提取值=%s | 原文=%s | 来源=%s | 定位=%s | 置信度=%s"
              % (e[C2["提取值"]], str(e[C2["证据原文"]])[:120], e[C2["证据来源"]],
                 str(e[C2["证据定位"]])[:60], e[C2["置信度"]]))
