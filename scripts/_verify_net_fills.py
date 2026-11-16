# -*- coding: utf-8 -*-
"""只读：复核刚才回填的 20 行，打印其"正面证据"与"频段证据"的原文片段。"""
from __future__ import annotations

import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
TEXT_COLS = ["商品标题", "产品卖点", "详细参数", "图片OCR摘要", "图片核心卖点",
             "图片理解结论", "SKU", "属性证据摘要"]
FILLED = ["B01A69D32D", "B042EFA0E8", "B047043728", "B063EB0692", "B09568FC9B",
          "B0B85DF8AE", "B004818BF1", "B0729F1C71", "B040B1370F", "B0D70D1C70",
          "B01CA5FED0", "B0E582D628", "B03075AF72", "B0B5053791", "B09F967784",
          "B04629EC4C", "B06930F7EF", "B061EDA3DA", "B0E7E972E3", "B0C72A3E15"]
PAT = re.compile(r".{0,45}(wi[\s-]?fi|wlan|\bapp\b|smart\s*app|app[\s-]?control|"
                 r"app-controlled|2\.4\s*g|5\s*g|dual[\s-]?band|双频).{0,45}", re.I)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = list(it)
wb.close()
C = {h: i for i, h in enumerate(hdr)}


def g(r, k):
    i = C.get(k)
    v = r[i] if i is not None else None
    return "" if v is None else str(v).strip()


for r in rows:
    a = g(r, "变体 ASIN")
    if a not in FILLED:
        continue
    net = g(r, "联网方式（GHz）")
    print("=" * 104)
    print("%-13s [%s] 联网=%-18s APP=%s | %s" % (a, g(r, "产品类型"), net,
                                                 g(r, "APP远程控制"), g(r, "商品标题")[:52]))
    for c in TEXT_COLS:
        if c not in C:
            continue
        for m in PAT.finditer(g(r, c)):
            s = m.group(0).replace("\n", " ").strip()
            print("    [%-6s] %s" % (c[:6], s[:140]))
