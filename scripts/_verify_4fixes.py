# -*- coding: utf-8 -*-
"""复核：四处改动是否只影响该影响的列（按列名对齐，容忍删列导致的位移）。"""
from __future__ import annotations

import os
import re
import zipfile

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
B = P + ".bak-before-meals-range-net-battery.xlsx"


def grab(p):
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    h = [str(x) for x in next(it)]
    rows = list(it)
    wb.close()
    return h, rows


nh, nr = grab(P)
oh, orr = grab(B)


def same(x, y):
    ex = x is None or str(x).strip() == ""
    ey = y is None or str(y).strip() == ""
    if ex and ey:
        return True
    if ex != ey:
        return False
    return str(x).strip() == str(y).strip()


print("列数 新/旧 = %d / %d   行数 = %d / %d" % (len(nh), len(oh), len(nr), len(orr)))
print("旧有新无（=被删的列）:", [c for c in oh if c not in nh])
print("新有旧无:", [c for c in nh if c not in oh])
diff = {}
for i, h in enumerate(nh):
    if h not in oh:
        continue
    j = oh.index(h)
    d = sum(1 for a, b in zip(nr, orr) if not same(a[i], b[j]))
    if d:
        diff[h] = d
print("\n值真正不同的列 =", diff)
print("合计改动格数 =", sum(diff.values()))

za, zb = zipfile.ZipFile(B), zipfile.ZipFile(P)
for tag, z in (("旧", za), ("新", zb)):
    s = z.read("xl/worksheets/sheet1.xml").decode("utf-8", errors="replace")
    rel = z.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
    print("%s 条目=%d DISPIMG=%d 超链接=%d autoFilter=%s freeze=%s"
          % (tag, len(z.namelist()), s.count("DISPIMG"), rel.count("hyperlink"),
             (re.search(r'<autoFilter[^>]*', s).group(0)[:30] if "<autoFilter" in s else "无"),
             "yes" if "<pane" in s else "no"))
print("大小 新/旧 =", os.path.getsize(P), "/", os.path.getsize(B))

# 字段口径 / 提取说明 现状
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
fj = wb["字段口径"]
print("\n字段口径 里的「每餐份数范围」行:",
      [r for r in fj.iter_rows(values_only=True) if r and "每餐份数范围" in str(r[0] or "")])
print("字段口径 还有「备用电池」行吗:",
      any("备用电池" in str(r[0] or "") for r in fj.iter_rows(values_only=True)))
wb.close()
