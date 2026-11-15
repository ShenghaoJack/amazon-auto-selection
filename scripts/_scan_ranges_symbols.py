# -*- coding: utf-8 -*-
"""只读：全工作簿搜索「范围写法」与「>/≥/≤」符号，定位用户看到的东西；
并查「有克重证据但列值为未明确」的行有多少。"""
from __future__ import annotations

import os
import re
from collections import Counter

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

SYM = re.compile(r"[>≥≤＜<]")
RANGE = re.compile(r"\d+(?:\.\d+)?\s*[-~–—至]\s*\d+(?:\.\d+)?")
GRAM = re.compile(r"(\d+(?:\.\d+)?)\s*(?:~|-|–|—|至)?\s*(\d+(?:\.\d+)?)?\s*g\b", re.I)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
print("sheets:", wb.sheetnames)
for name in wb.sheetnames:
    ws = wb[name]
    it = ws.iter_rows(values_only=True)
    try:
        hdr = [str(x) for x in next(it)]
    except StopIteration:
        continue
    hits_sym, hits_range = Counter(), Counter()
    samples = {}
    for r in it:
        for j, v in enumerate(r):
            s = "" if v is None else str(v)
            col = hdr[j] if j < len(hdr) else "?"
            if SYM.search(s):
                hits_sym[col] += 1
                samples.setdefault(("sym", col), s[:110])
            if RANGE.search(s):
                hits_range[col] += 1
                samples.setdefault(("rng", col), s[:110])
    if hits_sym or hits_range:
        print("\n===== sheet [%s] =====" % name)
        if hits_sym:
            print("  含 > ≥ ≤ 的列:", dict(hits_sym))
            for (k, col), s in samples.items():
                if k == "sym":
                    print("     [%s] 例: %s" % (col, s))
        if hits_range:
            print("  含范围写法的列:", dict(hits_range))
            for (k, col), s in samples.items():
                if k == "rng":
                    print("     [%s] 例: %s" % (col, s))
wb.close()

# ---- 有克重证据但列值未明确的行
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = list(it)
C = {h: i for i, h in enumerate(hdr)}
wb.close()
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
a = wb["属性证据审计"]
it2 = a.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
ev_rows = []
for r in it2:
    if str(r[C2["属性字段"]]).strip() in ("单份克重", "单份克重（g/份）"):
        ev_rows.append((str(r[C2["变体ASIN"]]).strip(), str(r[C2["提取值"]]),
                        str(r[C2["证据原文"]]), str(r[C2["证据来源"]])))
wb.close()
print("\n审计里 单份克重 的记录 = %d 条" % len(ev_rows))
val = {}
for i, r in enumerate(rows, start=2):
    val[str(r[C["变体 ASIN"]]).strip()] = (i, str(r[C["单份克重（g/份）"]]).strip(),
                                           str(r[C["商品标题"]])[:70])
cand = []
for a_, v, txt, src in ev_rows:
    info = val.get(a_)
    if not info:
        continue
    if info[1] == "未明确" and GRAM.search(txt or ""):
        cand.append((info[0], a_, GRAM.findall(txt)[:3], src, (txt or "")[:110]))
print("列值为「未明确」但证据里出现 g 数字的行 = %d" % len(cand))
for c in cand[:20]:
    print("   行%-4d %-13s 数字=%s 来源=%s | %s" % (c[0], c[1], c[2], c[3], c[4]))
