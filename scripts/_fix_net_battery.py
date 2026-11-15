# -*- coding: utf-8 -*-
"""处理两件明确的事：
  问题3：联网方式（GHz）里的 Bluetooth 不算联网方式 → 改为「未明确」（同步摘要与审计）
  问题5：删除「备用电池规格（数量×型号）」列（第 110 列），并清掉字段口径里的对应说明
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-net-battery-fix.xlsx"
MARK = "2026-09-30 人工修正"
SEG = re.compile(r"联网方式=[^；]*")

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK), os.path.getsize(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
NET, SUMCOL, ASINC = hdr["联网方式（GHz）"], hdr["属性证据摘要"], hdr["变体 ASIN"]

# ---------- 问题3
fixed = []
for r in range(2, ws.max_row + 1):
    if "Bluetooth" not in str(ws.cell(row=r, column=NET).value or ""):
        continue
    asin = str(ws.cell(row=r, column=ASINC).value or "").strip()
    old = str(ws.cell(row=r, column=NET).value)
    ws.cell(row=r, column=NET).value = "未明确"
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG.search(s):
        sc.value = SEG.sub("联网方式=未明确", s, count=1)
    fixed.append((r, asin, old))
print("\n【问题3】联网方式 Bluetooth → 未明确：%d 行" % len(fixed))
for r, a, o in fixed:
    print("   行%-4d %-13s %s → 未明确" % (r, a, o))

aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
n_aud = 0
for r in range(2, aud.max_row + 1):
    if str(aud.cell(row=r, column=h2["属性字段"]).value or "").strip() not in ("联网方式", "联网方式（GHz）"):
        continue
    a = str(aud.cell(row=r, column=h2["变体ASIN"]).value or "").strip()
    if a not in {x[1] for x in fixed}:
        continue
    aud.cell(row=r, column=h2["提取值"]).value = "未明确"
    st = str(aud.cell(row=r, column=h2["审核状态"]).value or "").strip()
    note = ("%s：Bluetooth 不算联网方式（蓝牙不是网络连接）→ 改为「未明确」" % MARK)
    aud.cell(row=r, column=h2["审核状态"]).value = (st + "；" + note) if st else note
    n_aud += 1
print("   审计同步 %d 条" % n_aud)

# ---------- 问题5：删列
DELNAME = "备用电池规格（数量×型号）"
dcol = hdr[DELNAME]
before_cols = ws.max_column
nonblank = sum(1 for r in range(2, ws.max_row + 1)
               if str(ws.cell(row=r, column=dcol).value or "").strip() not in ("", "未明确"))
ws.delete_cols(dcol, 1)
ws.auto_filter.ref = "A1:%s%d" % (openpyxl.utils.get_column_letter(ws.max_column), ws.max_row)
print("\n【问题5】已删除「%s」列（原第 %d 列，含 %d 行非「未明确」值）" % (DELNAME, dcol, nonblank))
print("   列数 %d → %d；autoFilter → %s" % (before_cols, ws.max_column, ws.auto_filter.ref))

# 字段口径里的对应说明行
fj = wb["字段口径"]
removed = None
for r in range(fj.max_row, 1, -1):
    if DELNAME in str(fj.cell(row=r, column=1).value or ""):
        removed = str(fj.cell(row=r, column=1).value)
        fj.delete_rows(r, 1)
print("   字段口径 删除说明行: %s" % (removed or "（未找到）"))

# 提取说明
n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（联网方式/删列）"
n.cell(row=rr, column=2).value = (
    "①「联网方式（GHz）」中 Bluetooth 不算联网方式（蓝牙非网络连接），1 行由 Bluetooth 改为「未明确」。"
    "② 删除「%s」列（原第 %d 列）：该列 %d 行是「未明确」、仅 %d 行有值，信息量过低；"
    "其证据仍保留在「属性证据审计」备查。" % (DELNAME, dcol, 415 - nonblank, nonblank))
wb.save(P)
print("   提取说明 追加第 %d 行；已保存" % rr)

# ---------- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
print("\n===== 复核 =====")
print("列数 = %d（应 121）；行数 = %d" % (len(h), len(rows)))
print("「%s」列还在吗: %s" % (DELNAME, DELNAME in h))
ci = h.index("联网方式（GHz）")
import collections
c = collections.Counter("" if r[ci] is None else str(r[ci]).strip() for r in rows)
print("联网方式分布:", dict(c.most_common()))
print("仍含 Bluetooth 的行 =", sum(1 for r in rows if "lue" in str(r[ci])))
si = h.index("属性证据摘要")
print("摘要里仍含「联网方式=Bluetooth」的行 =",
      sum(1 for r in rows if "联网方式=Bluetooth" in str(r[si] or "")))
