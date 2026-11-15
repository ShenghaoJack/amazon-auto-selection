# -*- coding: utf-8 -*-
"""只读：属性证据摘要 里「容量（原文）=」为多值的行，与容量列已改的行对照。"""
from __future__ import annotations

import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
NUM = re.compile(r"\d+(?:\.\d+)?")
SEG = re.compile(r"容量（原文）=([^；]*)")

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}

hits = []
for n, r in rows:
    s = "" if r[C["属性证据摘要"]] is None else str(r[C["属性证据摘要"]])
    m = SEG.search(s)
    if not m:
        continue
    seg = m.group(1)
    if len(NUM.findall(seg)) >= 2:
        hits.append((n, str(r[C["变体 ASIN"]]).strip(), seg.strip(),
                     r[C["容量（L）"]], s))

print("摘要里「容量（原文）=」多值的行 = %d\n" % len(hits))
print("%-7s %-13s %-24s %-8s %s" % ("Excel行", "变体ASIN", "摘要原文段", "列现值", "整段摘要"))
print("-" * 120)
for n, a, seg, col, s in hits:
    print("%-7d %-13s %-24s %-8s %s" % (n, a, seg, col, s[:60]))
mx = max((float(x) for _, _, seg, _, _ in hits for x in NUM.findall(seg)), default=0)
print("\n涉及分隔符:", sorted({c for _, _, seg, _, _ in hits for c in seg if not c.isalnum()
                              and c not in ". "}))
print("单位写法:", sorted({x for _, _, seg, _, _ in hits for x in re.findall(r"[A-Za-z]+", seg)}))
