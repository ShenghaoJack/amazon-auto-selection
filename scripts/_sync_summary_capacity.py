# -*- coding: utf-8 -*-
"""把「属性证据摘要」里 `容量（原文）=` 的多值写法同步成单值。

78 行分两类，**处理规则不同**：
  A. 真·两个容量（食水套装「食物/水」）：4 L / 3 L、2.2 L / 1 L、7 L / 29 Cups / 8 L
     → 取最大（与容量列已改的口径一致）
  B. 同一容量换两种单位复述：20 Cups / 5 L、1 Gallon / 3.8 L、4 L / 4 liters
     → **不能取最大数字**（20 Cups / 5 L 会变成 20），统一成 L 口径的值

所以本脚本一律按 `容量（L）` 列的**现值**回写：`容量（原文）={<列值>} L`，
保证摘要与列完全一致；原始多值仍留在「属性证据审计」的审核状态里可追溯。
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-summary-sync.xlsx"
NUM = re.compile(r"\d+(?:\.\d+)?")
SEG = re.compile(r"容量（原文）=[^；]*")
MARK = "2026-09-30 人工修正"

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK), os.path.getsize(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
SUMMARY, CAP, ASIN = hdr["属性证据摘要"], hdr["容量（L）"], hdr["变体 ASIN"]

changed, skipped = [], []
for r in range(2, ws.max_row + 1):
    sc = ws.cell(row=r, column=SUMMARY)
    s = "" if sc.value is None else str(sc.value)
    m = SEG.search(s)
    if not m or len(NUM.findall(m.group(0))) < 2:
        continue
    v = ws.cell(row=r, column=CAP).value
    if v is None or isinstance(v, str) or not isinstance(v, (int, float)):
        skipped.append((r, str(ws.cell(row=r, column=ASIN).value or ""), m.group(0), v))
        continue
    num = int(v) if float(v).is_integer() else float(v)
    new_seg = "容量（原文）=%s L" % num
    sc.value = SEG.sub(new_seg, s, count=1)
    changed.append((r, str(ws.cell(row=r, column=ASIN).value or "").strip(), m.group(0), new_seg))

print("\n同步 %d 行；跳过（列值非数值）%d 行" % (len(changed), len(skipped)))
print("%-7s %-13s %-26s %s" % ("Excel行", "变体ASIN", "原摘要段", "改后"))
print("-" * 88)
for r, a, old, new in changed[:20]:
    print("%-7d %-13s %-26s %s" % (r, a, old, new))
if len(changed) > 20:
    print("   …（其余 %d 行同理）" % (len(changed) - 20))
for r, a, old, v in skipped:
    print("  ⚠ 跳过 行%d %s  %s  列值=%r" % (r, a, old, v))

note = wb["提取说明"]
rr = note.max_row + 2
note.cell(row=rr, column=1).value = MARK + "（摘要同步）"
note.cell(row=rr, column=2).value = (
    "「属性证据摘要」列里 `容量（原文）=` 为多值的 %d 行已同步为单值（按容量列的现值，统一 L 口径）。"
    "两类多值的含义不同：① 食水套装的「食物容量/水容量」（如 4 L / 3 L）取最大；"
    "② 同一容量换两种单位复述（如 20 Cups / 5 L、1 Gallon / 3.8 L）**不是两个容量**，"
    "故不能取最大数字，一律取 L 口径值。原始多值保留在「属性证据审计」的审核状态里。"
    % len(changed))
wb.save(P)
print("\n提取说明 追加第 %d 行；已保存" % rr)

# ---- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
ci = h.index("属性证据摘要")
left = []
for i, r in enumerate(rows, start=2):
    s = "" if r[ci] is None else str(r[ci])
    m = SEG.search(s)
    if m and len(NUM.findall(m.group(0))) >= 2:
        left.append(i)
print("复核：摘要里仍多值的行 = %d（应为 0）" % len(left))
