# -*- coding: utf-8 -*-
"""只读：① 读「提取说明」「字段口径」里对产品类型的定义；
② 对 31 行做关键词审计：是主品(feeder)还是真耗材(accessory/consumable)。"""
from __future__ import annotations

import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
P = os.path.join(DESK, "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx")
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)

for s in ("提取说明", "字段口径"):
    ws = wb[s]
    print("=" * 96)
    print("【%s】" % s)
    for r in ws.iter_rows(values_only=True):
        cells = [str(c).strip() for c in r if c is not None and str(c).strip()]
        if cells:
            print("   " + " | ".join(cells)[:220])

ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(h) for h in next(it)]
C = {h: i for i, h in enumerate(hdr)}
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()

MAIN = re.compile(r"feeder|food dispenser|feed\s*&\s*water|water fountain", re.I)
CONS = re.compile(r"\b(replacement|refill|spare|consumable|desiccant bag[s]? only|"
                  r"pack of \d+ desiccant|filters? only|bowl only|tray only|"
                  r"silicone mat|power adapter|charging cable|cleaning brush)\b", re.I)

print("=" * 96)
print("【31 行关键词审计】 主品关键词=feeder/dispenser/fountain；耗材关键词=replacement/refill/spare/filter only/…")
print("%-8s %-13s %-5s %-5s %-6s %s" % ("Excel行", "变体ASIN", "主品", "耗材", "判定", "标题首段"))
print("-" * 126)
n_main = n_cons = 0
for n, r in rows:
    if str(r[C["产品类型"]] or "").strip() != "配件/耗材":
        continue
    t = str(r[C["商品标题"]] or "")
    m, c = bool(MAIN.search(t)), bool(CONS.search(t))
    if c and not m:
        verdict, n_cons = "真耗材?", n_cons + 1
    elif m:
        verdict, n_main = "主品(喂食器)", n_main + 1
    else:
        verdict = "存疑"
    print("%-8d %-13s %-5s %-5s %-13s %s"
          % (n, str(r[C["变体 ASIN"]] or "")[:13], "Y" if m else "-",
             "Y" if c else "-", verdict, t[:58]))
print("-" * 126)
print("判定：主品(自动喂食器) = %d 行；疑似真耗材 = %d 行" % (n_main, n_cons))
