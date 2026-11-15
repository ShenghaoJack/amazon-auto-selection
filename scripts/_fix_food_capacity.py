# -*- coding: utf-8 -*-
"""把 3 行食水套装的容量从「取最大（水箱）」改为「食物容量」。

证据（商品标题原文）：
  行322 B02AC3BD41  Marchul 7L/29 Cups Automatic Cat Feeder & 8L Gravity Water Dispenser Set
                    → 食物 7 L，水 8 L   （原取最大=8 ❌）
  行325 B0CD1D2750  Pets Automatic Gravity Feeder Dogs 2.8L Food & 4L Water Dispenser Set
                    → 食物 2.8 L，水 4 L （原取最大=4 ❌）
  行330 B07C8736D1  ... 2.4L Dry Food and 2.6L Water Bowl
                    → 食物 2.4 L，水 2.6 L（原取最大=2.6 ❌）

同时改：容量（L）列、属性证据摘要的「容量（原文）」、属性证据审计的提取值+备注。
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-food-capacity.xlsx"
SEG = re.compile(r"容量（原文）=[^；]*")
MARK = "2026-09-30 人工修正"

# (变体ASIN, 当前列值, 食物容量, 水箱容量)
FIX = [
    ("B02AC3BD41", 8, 7, 8),
    ("B0CD1D2750", 4, 2.8, 4),
    ("B07C8736D1", 2.6, 2.4, 2.6),
]

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK), os.path.getsize(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
CAP, SUMCOL, ASINC = hdr["容量（L）"], hdr["属性证据摘要"], hdr["变体 ASIN"]

by_asin = {}
for r in range(2, ws.max_row + 1):
    by_asin.setdefault(str(ws.cell(row=r, column=ASINC).value or "").strip(), []).append(r)

done = []
for asin, expect, food, water in FIX:
    rows = by_asin.get(asin) or []
    assert len(rows) == 1, "%s 匹配到 %d 行" % (asin, len(rows))
    r = rows[0]
    cur = ws.cell(row=r, column=CAP).value
    assert float(cur) == float(expect), "%s 行%d 当前值 %r != 预期 %r" % (asin, r, cur, expect)
    new = int(food) if float(food).is_integer() else float(food)
    ws.cell(row=r, column=CAP).value = new
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    before = (SEG.search(s).group(0) if SEG.search(s) else "")
    sc.value = SEG.sub("容量（原文）=%s L" % new, s, count=1)
    done.append((r, asin, before, "容量（原文）=%s L" % new, food, water))

print("\n%-7s %-13s %-24s %-16s %s" % ("Excel行", "变体ASIN", "原摘要段", "改后", "食物/水"))
print("-" * 92)
for r, a, b, af, food, water in done:
    print("%-7d %-13s %-24s %-16s %s / %s" % (r, a, b, af, food, water))

# ---- 属性证据审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
food_of = {a: (f, w) for a, _, f, w in FIX}
n_aud = 0
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=h2["属性字段"]).value or "").strip()
    if f not in ("容量", "容量（L）"):
        continue
    a = str(aud.cell(row=r, column=h2["变体ASIN"]).value or "").strip()
    if a not in food_of:
        continue
    food, water = food_of[a]
    aud.cell(row=r, column=h2["提取值"]).value = int(food) if float(food).is_integer() else food
    st = str(aud.cell(row=r, column=h2["审核状态"]).value or "").strip()
    note = ("%s：口径改为「食物容量」→ %s L（该 listing 为食水套装，水容量 %s L）"
            % (MARK, food, water))
    aud.cell(row=r, column=h2["审核状态"]).value = (st + "；" + note) if st else note
    n_aud += 1
print("\n属性证据审计 同步 %d 条" % n_aud)

# ---- 提取说明
n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（容量口径=食物容量）"
n.cell(row=rr, column=2).value = (
    "食水套装的容量口径定为**食物容量**（不是两者取大）。据此修正 3 行："
    "行322 B02AC3BD41 8→7（水 8 L）、行325 B0CD1D2750 4→2.8（水 4 L）、"
    "行330 B07C8736D1 2.6→2.4（水 2.6 L）。"
    "其余 11 行食水套装的最大值本就是食物容量，无需改动。"
    "非食水套装的多值（同一容量换算成杯/加仑）按 L 口径取值，不受此口径影响。")
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

# ---- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
ci, si = h.index("容量（L）"), h.index("属性证据摘要")
print("\n===== 复核 =====")
for asin, _, food, water in FIX:
    for r in rows:
        if str(r[h.index("变体 ASIN")]).strip() == asin:
            m = SEG.search(str(r[si]))
            print("  %-13s 列=%-6s 摘要=%-16s（水 %s L）" % (asin, r[ci], m.group(0) if m else "?", water))
import collections
c = collections.Counter("" if x[ci] is None else str(x[ci]).strip() for x in rows)
print("  容量列: 非空取值 %d 种, 未明确 %d 行" % (len([k for k in c if k]), c.get("未明确", 0)))
left = [i for i, x in enumerate(rows, 2)
        if (SEG.search(str(x[si])) and len(re.findall(r"\d+(?:\.\d+)?", SEG.search(str(x[si])).group(0))) > 1)]
print("  摘要里仍多值的行 =", len(left))
