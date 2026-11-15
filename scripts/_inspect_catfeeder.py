# -*- coding: utf-8 -*-
"""只读：查看 Automatic Cat Feeders 属性提炼版里「产品类型」列的取值分布，
并列出「配件/耗材」类的行，供判断是否真耗材。"""
from __future__ import annotations

import collections
import os

import openpyxl

P = (r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
     r"\Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx")

print("存在:", os.path.exists(P), " 大小:", os.path.getsize(P) if os.path.exists(P) else 0)
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
print("sheets:", wb.sheetnames)
for name in wb.sheetnames:
    ws = wb[name]
    it = ws.iter_rows(values_only=True)
    try:
        hdr = list(next(it))
    except StopIteration:
        print("  [%s] 空" % name)
        continue
    rows = list(it)
    print("\n===== [%s] %d 行 x %d 列 =====" % (name, len(rows), len(hdr)))
    for i, h in enumerate(hdr):
        print("  [%2d] %s" % (i, h))
    # 找产品类型列
    idx = [i for i, h in enumerate(hdr) if "产品类型" in str(h)]
    for i in idx:
        c = collections.Counter(str(r[i]).strip() if i < len(r) and r[i] is not None
                                else "" for r in rows)
        print("\n  产品类型列 [%d] 分布 (%d 种):" % (i, len(c)))
        for v, n in c.most_common():
            print("     %-28s %4d" % (v or "(空)", n))
wb.close()
