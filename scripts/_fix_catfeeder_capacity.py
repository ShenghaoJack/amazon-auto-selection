# -*- coding: utf-8 -*-
"""把 Automatic Cat Feeders 的「容量（L）」列里的多值写法（食水套装 = 食物容量/水容量）
统一成**单个值：取最大**。

实测 13 行 / 10 种写法，全部是「N L Food & M L Water」型；其余 402 行本就是单值或 未明确。
⚠ 其中 3 行「最大」落到的是**水箱**容量（7/8→8、2.8/4→4、2.4/2.6→2.6），已在日志里标出。
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-capacity-fix.xlsx"
MARK = "2026-09-30 人工修正"
NUM = re.compile(r"\d+(?:\.\d+)?")

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK), os.path.getsize(BAK), "bytes")

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
COL = hdr["容量（L）"]
ASIN = hdr["变体 ASIN"]

changes = []          # (行号, 变体ASIN, 原值, 新值, 是否取到水箱)
for r in range(2, ws.max_row + 1):
    cell = ws.cell(row=r, column=COL)
    v = "" if cell.value is None else str(cell.value).strip()
    nums = NUM.findall(v)
    if len(nums) < 2:
        continue
    mx = max(float(x) for x in nums)
    new = int(mx) if float(mx).is_integer() else mx
    first = float(nums[0])
    to_water = mx > first          # 最大值不是第一个数 = 取到了水箱
    old_fmt = cell.number_format
    cell.value = new
    cell.number_format = old_fmt if old_fmt else "0.00"
    changes.append((r, str(ws.cell(row=r, column=ASIN).value or "").strip(), v,
                    new, to_water))

print("\n共改 %d 行：" % len(changes))
print("%-7s %-13s %-14s %-8s %s" % ("Excel行", "变体ASIN", "原值", "新值", "说明"))
print("-" * 78)
for r, a, old, new, tw in changes:
    print("%-7d %-13s %-14s %-8s %s" % (r, a, old, new,
                                        "⚠ 取到的是水箱容量" if tw else "食物容量"))

# ---------- 同步 属性证据审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
by_asin = {a: (old, new) for _, a, old, new, _ in changes}
n_aud = 0
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=h2["属性字段"]).value or "").strip()
    if f not in ("容量", "容量（L）"):
        continue
    a = str(aud.cell(row=r, column=h2["变体ASIN"]).value or "").strip()
    if a not in by_asin:
        continue
    old, new = by_asin[a]
    aud.cell(row=r, column=h2["提取值"]).value = new
    st = str(aud.cell(row=r, column=h2["审核状态"]).value or "").strip()
    note = ("%s：容量由多值「%s」改为单一值「%s」（食水套装=食物容量/水容量，"
            "按“取最大容量”口径）。证据原文未改。" % (MARK, old, new))
    aud.cell(row=r, column=h2["审核状态"]).value = (st + "；" + note) if st else note
    n_aud += 1
print("\n属性证据审计 同步 %d 条" % n_aud)

# ---------- 提取说明
note_ws = wb["提取说明"]
r = note_ws.max_row + 2
note_ws.cell(row=r, column=1).value = MARK + "（容量取最大）"
note_ws.cell(row=r, column=2).value = (
    "「容量（L）」列原有 %d 行是多值写法（食水套装，形式为「食物容量 / 水容量」，"
    "如 4 / 3、5 / 3.5、2.2 / 1、7 / 8），已统一为**单个值 = 其中最大值**。"
    "其中 3 行的最大值取到的是水箱（7/8→8、2.8/4→4、2.4/2.6→2.6）；"
    "若口径应为「食物容量」，这 3 行需改为 7、2.8、2.4。"
    "本表「属性证据摘要」列的「容量（原文）」仍是原始多值，未改动。" % len(changes))
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % r)

# ---------- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
hh = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
ci = hh.index("容量（L）")
multi = [str(x[ci]) for x in rows
         if x[ci] is not None and len(NUM.findall(str(x[ci]))) > 1]
import collections
c = collections.Counter("" if x[ci] is None else str(x[ci]).strip() for x in rows)
print("\n===== 复核 =====")
print("仍有多个数字的单元格 = %d（应为 0）" % len(multi))
print("非空取值种数 = %d（原 44）" % len([k for k in c if k]))
print("未明确 = %d 行" % c.get("未明确", 0))
print("改后取值 top10:", c.most_common(10))
