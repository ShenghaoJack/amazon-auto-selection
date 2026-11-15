# -*- coding: utf-8 -*-
"""修正版：删除 Sheet1 行180（其他非自动喂食用品），并**精确重建超链接**。

上一版的问题：openpyxl 的 delete_rows 只移动单元格值，`ws._hyperlinks` 里的
ref 坐标不动 → 第180行之下 12 条超链接错位（核验报 19 处不一致）。
本版从**删行前备份**重做，按 (行,列) 映射把超链接重建到正确单元格：
  行 < 180 不变；行 == 180 丢弃（该行已被删）；行 > 180 → 行-1。
"""
from __future__ import annotations

import os
from collections import Counter

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
BASE = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, BASE)
SRC = P + ".bak-before-delete-others.xlsx"      # 删行前状态（已存在）
TARGET_ROW, TARGET_ASIN, TARGET_VAL = 180, "B08B9FCE1F", "其他非自动喂食用品"
MARK = "2026-09-29 人工处理"

wb = openpyxl.load_workbook(SRC)
ws = wb["Sheet1"]
hdr = {str(c.value): i + 1 for i, c in enumerate(ws[1])}
last, maxcol = ws.max_row, ws.max_column

hits = [r for r in range(2, last + 1)
        if str(ws.cell(row=r, column=hdr["产品类型"]).value or "").strip() == TARGET_VAL]
assert hits == [TARGET_ROW], "期望仅第 %d 行，实际 %s" % (TARGET_ROW, hits)
assert str(ws.cell(row=TARGET_ROW, column=hdr["变体 ASIN"]).value).strip() == TARGET_ASIN

links = [(c.row, c.column, c.hyperlink.target)
         for row in ws.iter_rows() for c in row if c.hyperlink]
snap = {r: (str(ws.cell(row=r, column=hdr["变体 ASIN"]).value or ""),
            str(ws.cell(row=r, column=hdr["商品标题"]).value or "")[:40])
        for r in range(TARGET_ROW + 1, last + 1)}
print("源(删行前): %d 数据行, %d 条超链接" % (last - 1, len(links)))

# ---------- 删行
ws.delete_rows(TARGET_ROW, 1)
ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(maxcol), last - 1)

# ---------- 精确重建超链接
ws._hyperlinks = []
for row in ws.iter_rows():
    for c in row:
        c._hyperlink = None
kept = 0
for (r, col, tgt) in links:
    if r == TARGET_ROW:
        continue
    nr = r - 1 if r > TARGET_ROW else r
    cell = ws.cell(row=nr, column=col)
    h = Hyperlink(ref=cell.coordinate, target=tgt)
    ws._hyperlinks.append(h)
    cell._hyperlink = h
    kept += 1
print("重建超链接 %d 条（丢弃被删行的 %d 条）" % (kept, len(links) - kept))

# ---------- 审计标记 + 提取说明
aud = wb["属性证据审计"]
h2 = {str(c.value): i + 1 for i, c in enumerate(aud[1])}
n_aud = 0
for r in range(2, aud.max_row + 1):
    if str(aud.cell(row=r, column=h2["属性字段"]).value or "").strip() != "产品类型":
        continue
    if str(aud.cell(row=r, column=h2["变体ASIN"]).value or "").strip() != TARGET_ASIN:
        continue
    st = str(aud.cell(row=r, column=h2["审核状态"]).value or "").strip()
    note = ("%s：已从交付表删除（原判「%s」。实为 SAROSORA 不锈钢双碗/护颈碗 $10.99，"
            "属耐用配件而非耗材，但确非自动喂食器，故按口径剔除；证据在此保留备查）"
            % (MARK, TARGET_VAL))
    aud.cell(row=r, column=h2["审核状态"]).value = (st + "；" + note) if st else note
    n_aud += 1
note_ws = wb["提取说明"]
r = note_ws.max_row + 2
note_ws.cell(row=r, column=1).value = MARK + "（删行）"
note_ws.cell(row=r, column=2).value = (
    "删除 Sheet1 中「产品类型 = 其他非自动喂食用品」的行共 1 行：变体 %s"
    "（SAROSORA Cat Feeder Stainless Steel Bowl，$10.99，父 ASIN B0EC63910C 下食碗变体）。"
    "该行非自动喂食器、也非耗材，按「只保留自动喂食器」口径剔除；删除后 Sheet1 为 %d 行。"
    "证据仍保留在「属性证据审计」备查。" % (TARGET_ASIN, last - 1))
wb.save(P)
print("已保存；审计标记 %d 条，提取说明追加第 %d 行" % (n_aud, r))

# ---------- 核验
wb2 = openpyxl.load_workbook(P)
w2 = wb2["Sheet1"]
hdr2 = {str(c.value): i + 1 for i, c in enumerate(w2[1])}
print("\n===== 核验 =====")
print("数据行 = %d（期望 %d）  产品类型分布 = %s"
      % (w2.max_row - 1, last - 1,
         dict(Counter(str(w2.cell(row=r, column=hdr2["产品类型"]).value or "").strip()
                      for r in range(2, w2.max_row + 1)))))
print("autoFilter =", w2.auto_filter.ref)
nl = [(c.row, c.column, c.hyperlink.target)
      for row in w2.iter_rows() for c in row if c.hyperlink]
print("超链接 = %d（期望 %d）" % (len(nl), kept))
exp = {(r - 1 if r > TARGET_ROW else r, col, t) for (r, col, t) in links
       if r != TARGET_ROW}
print("超链接 (行,列,目标) 集合完全一致 =", set(nl) == exp)
bad = [r for r, (a, t) in snap.items()
       if str(w2.cell(row=r - 1, column=hdr2["变体 ASIN"]).value or "") != a
       or str(w2.cell(row=r - 1, column=hdr2["商品标题"]).value or "")[:40] != t]
print("第180行之下数据上移 1 后错位行数 = %d（期望 0）" % len(bad))
print("DISPIMG 保留 =", str(w2.cell(row=2, column=5).value)[:34])
wb2.close()
