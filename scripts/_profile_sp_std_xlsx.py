# -*- coding: utf-8 -*-
"""只读体检：步骤18 标准化交付 xlsx 的每列空值分布。"""
from __future__ import annotations

import collections
import os
import sys

import openpyxl

CAT = "Salt and Pepper Mill Sets-US-202608"
P = os.path.join("data", CAT, "18_标准化",
                 "%s-属性标注-标准化交付.xlsx" % CAT)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
print("sheets:", wb.sheetnames)
for name in wb.sheetnames:
    ws = wb[name]
    it = ws.iter_rows(values_only=True)
    try:
        hdr = next(it)
    except StopIteration:
        print("  [%s] 空表" % name)
        continue
    n = 0
    blank = [0] * len(hdr)
    total = collections.Counter()
    samples: dict[int, list] = {}
    for row in it:
        n += 1
        for i in range(len(hdr)):
            v = row[i] if i < len(row) else None
            s = "" if v is None else str(v).strip()
            if s == "":
                blank[i] += 1
            else:
                total[i] += 1
                samples.setdefault(i, [])
                if len(samples[i]) < 3 and s not in samples[i]:
                    samples[i].append(s)
    print("\n===== sheet [%s]  %d 行 × %d 列 =====" % (name, n, len(hdr)))
    print("%-4s %-24s %8s %8s %8s  %s"
          % ("列", "表头", "非空", "空", "空占比", "样例"))
    print("-" * 108)
    for i, h in enumerate(hdr):
        if n == 0:
            continue
        rate = 100.0 * blank[i] / n
        flag = "  <<<" if rate > 50 else ""
        print("[%2d] %-24s %8d %8d %7.1f%%  %s%s"
              % (i, str(h)[:24], total[i], blank[i], rate,
                 " | ".join(str(s)[:16] for s in samples.get(i, [])[:2]), flag))
wb.close()
