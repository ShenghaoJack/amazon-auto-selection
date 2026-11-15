# -*- coding: utf-8 -*-
"""只读：对 255 行「联网方式=未明确」做文本分诊（免费），筛出必须看图的。

文本信号：
  频段明确：2.4G / 5G / dual band / 2.4/5GHz / 双频
  联网但无频段：WiFi / Wi-Fi / WLAN / APP / App Control / smart app / Bluetooth
"""
from __future__ import annotations

import collections
import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

TEXT_COLS = ["商品标题", "产品卖点", "详细参数", "图片OCR摘要", "图片核心卖点",
             "图片理解结论", "SKU", "属性证据摘要"]
BAND5 = re.compile(r"5\s*G(?:Hz)?\b|5GHz|5\.8\s*G", re.I)
BAND24 = re.compile(r"2\.4\s*G(?:Hz)?\b|2\.4GHz", re.I)
BAND_ANY = re.compile(r"dual[\s-]?band|双频|2\.4\s*/\s*5|2\.4G\s*(?:&|/|和)\s*5G", re.I)
WIFI = re.compile(r"wi[\s-]?fi|wlan|无线网络|蓝牙|bluetooth|\bapp\b|smart\s*app|app\s*control|"
                  r"remote\s*control|远程控制|手机", re.I)
GRAV = ("重力式喂食器", "重力式食水套装")

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


buckets = collections.Counter()
detail = collections.defaultdict(list)
for n, r in rows:
    if g(r, "联网方式（GHz）") not in ("", "未明确"):
        continue
    blob = " || ".join(g(r, c) for c in TEXT_COLS if c in C)
    band = ""
    if BAND_ANY.search(blob) or (BAND5.search(blob) and BAND24.search(blob)):
        band = "Wi‑Fi 2.4/5 GHz"
    elif BAND5.search(blob) and not BAND24.search(blob):
        band = "Wi‑Fi 5 GHz"
    elif BAND24.search(blob) and not BAND5.search(blob):
        band = "Wi‑Fi 2.4 GHz"
    elif WIFI.search(blob):
        band = "Wi‑Fi（频段未明确）"
    typ = g(r, "产品类型")
    key = ("%s | %s" % (typ, band or "无文本信号"))
    buckets[(typ, band or "★无文本信号")] += 1
    detail[(typ, band or "★无文本信号")].append((n, g(r, "变体 ASIN"), g(r, "商品标题")[:70]))

print("未明确联网的行，按 产品类型 × 文本可判结果 分组：\n")
for (typ, b), c in sorted(buckets.items(), key=lambda x: (-x[1], x[0][0])):
    mark = "  ← 需看图" if b.startswith("★") else ""
    print("  %-14s %-18s %3d 行%s" % (typ, b, c, mark))
print()
tot_need = sum(c for (t, b), c in buckets.items() if b.startswith("★"))
tot_text = sum(c for (t, b), c in buckets.items() if not b.startswith("★"))
print("文本能直接判定 = %d 行；必须看图 = %d 行" % (tot_text, tot_need))
print()
print("★ 需看图的明细（前 30）:")
k = 0
for (typ, b), lst in sorted(detail.items()):
    if not b.startswith("★"):
        continue
    for n, a, t in lst:
        print("   行%-4d %-13s %-14s %s" % (n, a, typ, t))
        k += 1
        if k >= 30:
            break
    if k >= 30:
        break
