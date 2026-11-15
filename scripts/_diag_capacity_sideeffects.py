# -*- coding: utf-8 -*-
"""诊断：容量列修正后，其它列到底有没有真的被改动；DISPIMG 计数差异是什么。"""
from __future__ import annotations

import os
import re
import zipfile

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
B = P + ".bak-before-capacity-fix.xlsx"


def grab(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x) for x in next(it)]
    rows = list(it)
    wb.close()
    return hdr, rows


nh, nr = grab(P)
oh, orr = grab(B)
ci = nh.index("容量（L）")


def same(x, y):
    ex = x is None or str(x).strip() == ""
    ey = y is None or str(y).strip() == ""
    if ex and ey:
        return True
    if ex != ey:
        return False
    return str(x).strip() == str(y).strip()


print("== 把 None 与空串视为等价后，仍真有不同的列 ==")
real = {}
for i, h in enumerate(nh):
    if i == ci:
        continue
    d = sum(1 for a, b in zip(nr, orr) if not same(a[i], b[i]))
    if d:
        real[h] = d
print(real or "无 ✅")

print("\n== 仅『None vs 空串』表示不同的列 ==")
for i, h in enumerate(nh):
    if i == ci:
        continue
    d = 0
    for a, b in zip(nr, orr):
        x, y = a[i], b[i]
        if (x is None) != (y is None) and (x is None or y is None):
            other = y if x is None else x
            if str(other).strip() == "":
                d += 1
    if d:
        print("   %-22s %d 行" % (h, d))

print("\n== DISPIMG 细节 ==")
for tag, p in (("旧(改容量前)", B), ("新", P)):
    s = zipfile.ZipFile(p).read("xl/worksheets/sheet1.xml").decode("utf-8", errors="replace")
    m = re.search(r'<c r="E2".*?</c>', s)
    print("%-12s DISPIMG 出现 = %-4d  E2 = %s" % (tag, s.count("DISPIMG"), m.group(0)[:170] if m else "?"))
    print("              <v> 里带 DISPIMG 文本的次数 = %d" % s.count("<v>=DISPIMG"))
