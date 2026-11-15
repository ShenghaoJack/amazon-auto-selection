# -*- coding: utf-8 -*-
"""把行 206（Meikuler 5.2L Food Feeder and Water Dispenser Set，White）的
产品类型由「其他非自动喂食用品」改为「程序定时干粮喂食器」，
与同父同款同容量的 Navy 变体（行 209 B0D9DA014F）对齐。

行 180（SAROSORA 不锈钢食碗）= 实质为非自动喂食用品、且非耗材 → 按决定保持不动。
"""
from __future__ import annotations

import os
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-typefix2.xlsx"
MARK = "2026-09-29 人工修正"

ROW, ASIN, NEW = 206, "B097AEE363", "程序定时干粮喂食器"

shutil.copy2(P, BAK)
print("① 已备份 ->", os.path.basename(BAK), os.path.getsize(BAK), "bytes")

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = [str(c.value) if c.value is not None else "" for c in ws[1]]
C = {h: i + 1 for i, h in enumerate(hdr)}

# --- 定位保护：行号 + ASIN + 旧值 三重校验，避免错改
a_cell = ws.cell(row=ROW, column=C["变体 ASIN"]).value
t_cell = ws.cell(row=ROW, column=C["产品类型"]).value
assert str(a_cell).strip() == ASIN, "行 %d 的变体ASIN 不是 %s，实际 %r" % (ROW, ASIN, a_cell)
assert str(t_cell).strip() == "其他非自动喂食用品", "行 %d 旧值不是预期值: %r" % (ROW, t_cell)
ws.cell(row=ROW, column=C["产品类型"]).value = NEW
print("② Sheet1 行 %d（%s）: %s -> %s" % (ROW, ASIN, t_cell, NEW))

# --- 同步属性证据审计
aud = wb["属性证据审计"]
h2 = [str(c.value) if c.value is not None else "" for c in aud[1]]
C2 = {h: i + 1 for i, h in enumerate(h2)}
n = 0
for r in range(2, aud.max_row + 1):
    if str(aud.cell(row=r, column=C2["属性字段"]).value or "").strip() != "产品类型":
        continue
    if str(aud.cell(row=r, column=C2["变体ASIN"]).value or "").strip() != ASIN:
        continue
    old = str(aud.cell(row=r, column=C2["提取值"]).value or "").strip()
    aud.cell(row=r, column=C2["提取值"]).value = NEW
    st = str(aud.cell(row=r, column=C2["审核状态"]).value or "").strip()
    note = ("%s：原判「%s」→「%s」（同父同款 Navy 变体 B0D9DA014F 已判「%s」，"
            "仅颜色不同却标签不一致，属提取抖动）" % (MARK, old, NEW, NEW))
    aud.cell(row=r, column=C2["审核状态"]).value = (st + "；" + note) if st else note
    n += 1
print("③ 属性证据审计 同步 %d 条" % n)
assert n >= 1, "审计表未找到对应记录"

# --- 提取说明 追加
note_ws = wb["提取说明"]
r = note_ws.max_row + 2
note_ws.cell(row=r, column=1).value = MARK + "（补）"
note_ws.cell(row=r, column=2).value = (
    "产品类型列：行206 变体 B097AEE363 由「其他非自动喂食用品」改为「程序定时干粮喂食器」，"
    "与同父同款同容量（5.2L）的 Navy 变体 B0D9DA014F 对齐（两者标题仅差颜色）。"
    "行180 变体 B08B9FCE1F（SAROSORA 不锈钢双碗/护颈碗，$10.99）实质为非自动喂食用品、"
    "且属耐用配件而非耗材，故保持原标签不动。")
wb.save(P)
print("④ 提取说明 追加第 %d 行；已保存" % r)
