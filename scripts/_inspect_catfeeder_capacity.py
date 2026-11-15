# -*- coding: utf-8 -*-
"""只读：Automatic Cat Feeders 的「容量（L）」列全部取值，标出多值写法。"""
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
hdr = [str(h) for h in next(it)]
rows = list(it)
wb.close()
C = {h: i for i, h in enumerate(hdr)}
print("列:", [h for h in hdr if "容量" in h])

NUM = re.compile(r"\d+(?:\.\d+)?")
cnt = collections.Counter()
for r in rows:
    v = r[C["容量（L）"]]
    s = "" if v is None else str(v).strip()
    cnt[s] += 1

multi = {k: v for k, v in cnt.items() if len(NUM.findall(k)) > 1}
print("\n容量（L）非空取值 %d 种，其中**含 2 个以上数字**的 %d 种：\n"
      % (len([k for k in cnt if k]), len(multi)))
print("%-28s %5s   %s" % ("取值", "行数", "抽出的数字"))
print("-" * 74)
for k, v in sorted(multi.items(), key=lambda x: -x[1]):
    print("%-28s %5d   %s" % (k, v, NUM.findall(k)))
print("\n其余单值样例（%d 种）：" % len([k for k in cnt if k and k not in multi]))
for k, v in sorted(((k, n) for k, n in cnt.items() if k and k not in multi),
                   key=lambda x: -x[1])[:25]:
    print("   %-20s %4d" % (k, v))
