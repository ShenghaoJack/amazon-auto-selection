# -*- coding: utf-8 -*-
"""只读：核对「单份克重（g/份）」里的异常值（120 / 330 等），追出模型当时的依据。"""
from __future__ import annotations

import collections
import csv
import json
import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
COL = "单份克重（g/份）"

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
a = wb["属性证据审计"]
it2 = a.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
aud = collections.defaultdict(list)
for r in it2:
    if COL.replace("（g/份）", "") in str(r[C2["属性字段"]]):
        aud[str(r[C2["变体ASIN"]]).strip()].append(
            (str(r[C2["提取值"]]), str(r[C2["证据原文"]]), str(r[C2["证据来源"]]),
             str(r[C2["审核状态"]])))
wb.close()
C = {h: i for i, h in enumerate(hdr)}

cnt = collections.Counter("" if r[C[COL]] is None else str(r[C[COL]]).strip() for n, r in rows)
print("【%s】共 %d 种取值（非「未明确」的）：" % (COL, len([k for k in cnt if k and k != "未明确"])))
for k, v in sorted(((k, v) for k, v in cnt.items() if k and k != "未明确"),
                   key=lambda x: -float(x[0]) if re.match(r"^[\d.]+$", x[0]) else 0):
    print("   %-10s %4d" % (k, v))
print("   未明确    %4d" % cnt.get("未明确", 0))

# 模型依据（全图版）
model = {}
for f in ("scripts/_reannotate_result_all.json", "scripts/_reannotate_result.json"):
    if os.path.exists(f):
        d = json.load(open(f, encoding="utf-8"))
        for x in d["results"]:
            if not x.get("error"):
                model.setdefault(int(x["row"]), x)
print("\n=== 异常值逐行核对（>=30 g/份 或 明显不合理）===")
for n, r in rows:
    v = "" if r[C[COL]] is None else str(r[C[COL]]).strip()
    if not re.match(r"^[\d.]+$", v):
        continue
    if float(v) < 30:
        continue
    asin = str(r[C["变体 ASIN"]]).strip()
    print("=" * 104)
    print("行%-4d %-13s 单份克重 = %s | 容量=%s | %s"
          % (n, asin, v, r[C["容量（L）"]] if "容量（L）" in C else "-",
             str(r[C["商品标题"]])[:70]))
    print("   详细参数: %s" % str(r[C["详细参数"]])[:150])
    for e in aud.get(asin, [])[:2]:
        print("   审计: 值=%s 原文=%s 来源=%s" % (e[0], str(e[1])[:110], e[2]))
    m = model.get(n)
    if m:
        print("   重跑判定: 单份克重=%s | 依据=%s"
              % (m["new"].get(COL), str(m.get("why") or "")[:120]))
