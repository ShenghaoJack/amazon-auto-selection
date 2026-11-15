# -*- coding: utf-8 -*-
"""只读：体检这 5 列 —— 每日最大餐数 / 单份克重 / 联网方式 / 摄像头分辨率 / 备用电池规格。"""
from __future__ import annotations

import collections
import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

TARGETS = ["每日最大餐数（餐/天）", "单份克重（g/份）", "联网方式（GHz）",
           "摄像头分辨率（原标称）", "备用电池规格（数量×型号）"]

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = list(it)
wb.close()
C = {h: i for i, h in enumerate(hdr)}
print("总行数 =", len(rows), " 列数 =", len(hdr))
print("列定位:", {t: hdr.index(t) + 1 for t in TARGETS})

for t in TARGETS:
    i = C[t]
    cnt = collections.Counter("" if r[i] is None else str(r[i]).strip() for r in rows)
    blank = cnt.get("", 0)
    print("\n" + "=" * 96)
    print("【%s】 非空取值 %d 种，空 %d 行" % (t, len(cnt) - (1 if "" in cnt else 0), blank))
    for k, v in cnt.most_common(40):
        print("   %-30s %4d" % (k or "(空)", v))
    if len(cnt) > 41:
        print("   …（另 %d 种）" % (len(cnt) - 41))
