# -*- coding: utf-8 -*-
"""只读：多值容量那 13 行的原始证据，判断「取最大」是否合理。"""
from __future__ import annotations

import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
NUM = re.compile(r"\d+(?:\.\d+)?")

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(h) for h in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]

aud = wb["属性证据审计"]
it2 = aud.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
ev = {}
for r in it2:
    if str(r[C2["属性字段"]]).strip() in ("容量", "容量（L）"):
        ev.setdefault(str(r[C2["变体ASIN"]]).strip(), []).append(
            " | ".join(str(r[C2[k]] or "")[:80]
                       for k in ("提取值", "证据原文", "证据来源", "证据定位")))
wb.close()

C = {h: i for i, h in enumerate(hdr)}
for n, r in rows:
    v = "" if r[C["容量（L）"]] is None else str(r[C["容量（L）"]]).strip()
    if len(NUM.findall(v)) <= 1:
        continue
    mx = max(float(x) for x in NUM.findall(v))
    print("=" * 104)
    print("行%-4d 取值=%-12s → 取最大=%-6s | 父 %s | 价 $%s"
          % (n, v, mx, r[C["父 ASIN"]], r[C["价格（USD）"]]))
    print("  标题: %s" % str(r[C["商品标题"]])[:150])
    print("  摘要: %s" % str(r[C["属性证据摘要"]])[:180])
    a = str(r[C["变体 ASIN"]]).strip()
    for e in ev.get(a, [])[:2]:
        print("  证据: %s" % e[:150])
