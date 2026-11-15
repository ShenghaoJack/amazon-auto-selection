# -*- coding: utf-8 -*-
"""只读：从「产品类型」到「适用颗粒尺寸（mm）」共 31 列的取值现状与「未明确」缺口。"""
from __future__ import annotations

import collections
import os

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = list(it)
wb.close()
i0 = hdr.index("产品类型")
i1 = hdr.index("适用颗粒尺寸（mm）")
cols = hdr[i0:i1 + 1]
print("区间：第 %d 列「%s」→ 第 %d 列「%s」，共 %d 列；数据 %d 行\n"
      % (i0 + 1, cols[0], i1 + 1, cols[-1], len(cols), len(rows)))

UNK = {"", "未明确", "未知", "待确认"}
print("%-22s %6s %7s %7s %8s  %s" % ("列", "非空", "未明确", "缺失率", "唯一值", "取值样例"))
print("-" * 118)
tot_unk = 0
for j in range(i0, i1 + 1):
    vals = ["" if r[j] is None else str(r[j]).strip() for r in rows]
    unk = sum(1 for v in vals if v in UNK)
    tot_unk += unk
    uq = [v for v in dict.fromkeys(vals) if v not in UNK]
    print("%-22s %6d %7d %6.1f%% %8d  %s"
          % (hdr[j], len(vals) - unk, unk, 100.0 * unk / len(vals), len(uq),
             "、".join(uq[:6])[:44]))
print("-" * 118)
print("区间内「未明确/空」合计 = %d 格（%d 列 × %d 行 = %d 格，占 %.1f%%）"
      % (tot_unk, len(cols), len(rows), len(cols) * len(rows),
         100.0 * tot_unk / (len(cols) * len(rows))))
