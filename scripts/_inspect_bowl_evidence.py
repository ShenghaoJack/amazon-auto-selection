# -*- coding: utf-8 -*-
"""只读：食碗材质 的「应有词表」是什么；169 行未明确里各有多少有「碗相关」证据。"""
from __future__ import annotations

import collections
import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
a = wb["属性证据审计"]
it2 = a.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
aud_rows = collections.defaultdict(list)
allvals = collections.Counter()
for r in it2:
    f = str(r[C2["属性字段"]]).strip()
    if "食碗材质" not in f:
        continue
    allvals[str(r[C2["提取值"]]).strip()] += 1
    aud_rows[str(r[C2["变体ASIN"]]).strip()].append(
        (str(r[C2["提取值"]]).strip(), str(r[C2["证据原文"]]), str(r[C2["证据来源"]])))
wb.close()
C = {h: i for i, h in enumerate(hdr)}
COL = [h for h in hdr if "食碗材质" in h][0]

print("审计里「食碗材质」的提取值分布（= 该字段实际用过的词表）:")
for k, v in allvals.most_common():
    print("   %-26s %4d" % (k or "(空)", v))

BOWL = re.compile(r"bowl|tray|dish|食碗|碗|盆", re.I)
MAT = re.compile(r"stainless\s*steel|不锈钢|ceramic|陶瓷|plastic|塑料|silicone|硅胶|"
                 r"melamine|密胺|bamboo|竹|metal|金属|abs\b|pp\b|304|acrylonitrile", re.I)
TXT = ["商品标题", "详细参数", "产品卖点", "图片OCR摘要", "图片核心卖点",
       "图片理解结论", "属性证据摘要"]

cat = collections.Counter()
detail = collections.defaultdict(list)
for n, r in rows:
    v = "" if r[C[COL]] is None else str(r[C[COL]]).strip()
    if v != "未明确":
        continue
    asin = str(r[C["变体 ASIN"]]).strip()
    evs = aud_rows.get(asin, [])
    bowl_ev, gen_ev, none_ev = [], [], []
    for val, txt, src in evs:
        if "未找到明确证据" in (txt or "") or not (txt or "").strip():
            none_ev.append(txt)
        elif BOWL.search(txt or ""):
            bowl_ev.append((val, txt, src))
        elif MAT.search(txt or ""):
            gen_ev.append((val, txt, src))
    # 行内文本里 bowl 与材质词同现
    blob = " || ".join("" if r[C[c]] is None else str(r[C[c]]) for c in TXT if c in C)
    inline_bowl = bool(BOWL.search(blob)) and bool(MAT.search(blob))
    if bowl_ev:
        cat["① 审计证据含「碗」+材质"] += 1
        detail["①"].append((n, asin, bowl_ev[0]))
    elif inline_bowl:
        cat["② 行文本里「碗」与材质词同现"] += 1
        detail["②"].append((n, asin, (round(0), blob[:90], "")))
    elif gen_ev:
        cat["③ 只有产品级材质（未提碗）"] += 1
        detail["③"].append((n, asin, gen_ev[0]))
    else:
        cat["④ 无任何材质证据"] += 1
        detail["④"].append((n, asin, ("", blob[:90], "")))

print("\n169 行未明确的证据可得性:")
for k, v in cat.most_common():
    print("   %-32s %4d" % (k, v))
print()
for k in ("①", "②", "③", "④"):
    print("--- %s 类样例 ---" % k)
    for n, asin, ev in detail[k][:6]:
        print("   行%-4d %-13s val=%-14s %s" % (n, asin, ev[0], str(ev[1])[:92].replace("\n", " ")))
