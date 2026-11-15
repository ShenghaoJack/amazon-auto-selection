# -*- coding: utf-8 -*-
"""只读：核对「摄像头分辨率（原标称）」—— 每个非「未明确」值的原文出处，
重点看 3MP 是否真实标称、与其他值（1080P/2K/4K）是否同一量纲。"""
from __future__ import annotations

import collections
import json
import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
COL = "摄像头分辨率（原标称）"
PAT = re.compile(r".{0,50}(1080\s*P|2\s*K|4\s*K|3\s*MP|5\s*MP|2\s*MP|\d+\s*MP|"
                 r"1920\s*[x×]\s*1080|2560\s*[x×]\s*1440|3840\s*[x×]\s*2160|"
                 r"2304\s*[x×]\s*1296|2048\s*[x×]\s*1536|720\s*P).{0,50}", re.I)
TXT = ["商品标题", "详细参数", "产品卖点", "图片OCR摘要", "图片核心卖点",
       "图片理解结论", "属性证据摘要"]

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}
model = {}
for f in ("scripts/_reannotate_result_all.json", "scripts/_reannotate_result.json"):
    if os.path.exists(f):
        for x in json.load(open(f, encoding="utf-8"))["results"]:
            if not x.get("error"):
                model.setdefault(int(x["row"]), x)

cnt = collections.Counter("" if r[C[COL]] is None else str(r[C[COL]]).strip() for n, r in rows)
print("【%s】取值分布：" % COL)
for k, v in cnt.most_common():
    print("   %-10s %4d" % (k or "(空)", v))

print("\n=== 每个非「未明确」值的原文出处（逐行）===")
for n, r in rows:
    v = "" if r[C[COL]] is None else str(r[C[COL]]).strip()
    if not v or v == "未明确":
        continue
    hits = []
    for c in TXT:
        if c not in C:
            continue
        s = "" if r[C[c]] is None else str(r[C[c]])
        for m in PAT.finditer(s):
            frag = m.group(0).replace("\n", " ").strip()
            if frag not in hits:
                hits.append("[%s] %s" % (c[:6], frag))
    mm = model.get(n)
    print("=" * 104)
    print("行%-4d %-13s 表内值 = %-8s | 产品类型=%s"
          % (n, str(r[C["变体 ASIN"]]).strip(), v, r[C["产品类型"]]))
    print("   标题: %s" % str(r[C["商品标题"]])[:100])
    if hits:
        for h in hits[:4]:
            print("   %s" % h[:130])
    else:
        print("   ⚠ 行内文本找不到分辨率字样")
    if mm:
        print("   重跑判定=%s | 依据=%s" % (mm["new"].get(COL), str(mm.get("why") or "")[:110]))
