# -*- coding: utf-8 -*-
"""只读：食碗材质列现状 + 未明确行的可得证据。"""
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
# 审计表
a = wb["属性证据审计"]
it2 = a.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
aud = collections.defaultdict(list)
for r in it2:
    aud[str(r[C2["变体ASIN"]]).strip()].append(
        (str(r[C2["属性字段"]]).strip(), str(r[C2["提取值"]]),
         str(r[C2["证据原文"]]), str(r[C2["证据来源"]])))
wb.close()
C = {h: i for i, h in enumerate(hdr)}
COL = [h for h in hdr if "食碗材质" in h][0]
print("列名:", COL, " 第", C[COL] + 1, "列")

cnt = collections.Counter("" if r[C[COL]] is None else str(r[C[COL]]).strip() for r, _ in
                          [(r, n) for n, r in rows])
cnt = collections.Counter("" if r[C[COL]] is None else str(r[C[COL]]).strip() for n, r in rows)
print("\n取值分布（%d 种）:" % len(cnt))
for k, v in cnt.most_common():
    print("   %-24s %4d" % (k or "(空)", v))

# 未明确行的证据可得性
MAT = re.compile(r"stainless\s*steel|不锈钢|ceramic|陶瓷|plastic|塑料|silicone|硅胶|"
                 r"melamine|密胺|bamboo|竹|metal|金属|abs|pp\b|304", re.I)
TXT = ["商品标题", "详细参数", "产品卖点", "图片OCR摘要", "图片核心卖点",
       "图片理解结论", "属性证据摘要"]
has_ev = 0
n_unk = 0
samples = []
for n, r in rows:
    v = "" if r[C[COL]] is None else str(r[C[COL]]).strip()
    if v not in ("", "未明确"):
        continue
    n_unk += 1
    a_ = str(r[C["变体 ASIN"]]).strip()
    ev = [t for f, val, t, src in aud.get(a_, []) if "食碗材质" in f]
    blob = " || ".join("" if r[C[c]] is None else str(r[C[c]]) for c in TXT if c in C)
    m = MAT.findall(blob) + [x for e in ev for x in MAT.findall(e)]
    if m:
        has_ev += 1
        if len(samples) < 15:
            samples.append((n, a_, set(x.lower() for x in m), ev[:1], blob[:110]))
print("\n食碗材质=未明确 共 %d 行；其中**文本/审计里能找到材质词**的 %d 行" % (n_unk, has_ev))
for n, a_, kinds, ev, blob in samples:
    print("   行%-4d %-13s 命中=%-28s 审计证据=%s" % (n, a_, sorted(kinds)[:4],
                                                (ev[0][:60] if ev else "无")))
    print("        文本片段: %s" % blob.replace("\n", " ")[:100])
print("\n「不锈钢（材质字段）」写法出现的行数:")
c2 = sum(1 for n, r in rows if "材质字段" in str(r[C[COL]] or ""))
print("   ", c2)
