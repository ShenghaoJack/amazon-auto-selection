# -*- coding: utf-8 -*-
"""只读：打印 255 行「联网方式=未明确」里文本有联网信号的行的**匹配上下文**，供人工确认。"""
from __future__ import annotations

import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
TEXT_COLS = ["商品标题", "产品卖点", "详细参数", "图片OCR摘要", "图片核心卖点",
             "图片理解结论", "SKU", "属性证据摘要"]
PAT = re.compile(r".{0,32}(wi[\s-]?fi|wlan|\bapp\b|smart\s*app|app\s*control|"
                 r"remote\s*control|远程控制|手机|蓝牙|bluetooth|2\.4\s*g|5\s*g|dual[\s-]?band|双频)"
                 r".{0,32}", re.I)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}


def g(r, k):
    i = C.get(k)
    v = r[i] if i is not None else None
    return "" if v is None else str(v).strip()


hit = []
for n, r in rows:
    if g(r, "联网方式（GHz）") not in ("", "未明确"):
        continue
    ctx = []
    for c in TEXT_COLS:
        if c not in C:
            continue
        for m in PAT.finditer(g(r, c)):
            s = m.group(0).replace("\n", " ").strip()
            if s and s not in ctx:
                ctx.append("[%s] %s" % (c[:6], s))
    if ctx:
        hit.append((n, r, ctx))

print("文本有联网信号的行 = %d\n" % len(hit))
for n, r, ctx in hit:
    print("行%-4d %-13s %-14s APP=%-4s 机制=%-6s" % (
        n, g(r, "变体 ASIN"), g(r, "产品类型"), g(r, "APP远程控制"), g(r, "喂食机制")))
    for c in ctx[:3]:
        print("      %s" % c[:150])
