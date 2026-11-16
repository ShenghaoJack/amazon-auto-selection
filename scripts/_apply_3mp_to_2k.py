# -*- coding: utf-8 -*-
"""按口径把「摄像头分辨率（原标称）」里的 3MP 归并为 2K（3 行），并写入字段口径。
证据列（图片核心卖点/图片理解结论）保留原始视觉输出，只追加修正标记。
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-3mp-to-2k.xlsx"
MARK = "2026-09-30 人工修正"
SEG_RES = re.compile(r"摄像头分辨率=[^；]*")
TARGETS = ["B032DE4977", "B03DE062D5", "B0CE7339B1"]

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
RES, SUMCOL, ASINC = (hdr["摄像头分辨率（原标称）"], hdr["属性证据摘要"],
                      hdr["变体 ASIN"])
SUBJ, CONC = hdr["图片核心卖点"], hdr["图片理解结论"]
by_asin = {str(ws.cell(row=r, column=ASINC).value or "").strip(): r
           for r in range(2, ws.max_row + 1)}

done = []
for a in TARGETS:
    r = by_asin[a]
    old = str(ws.cell(row=r, column=RES).value or "").strip()
    ws.cell(row=r, column=RES).value = "2K"
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    sc.value = (SEG_RES.sub("摄像头分辨率=2K", s, count=1) if SEG_RES.search(s)
                else (s + "；摄像头分辨率=2K" if s else "摄像头分辨率=2K"))
    for col in (SUBJ, CONC):
        c = ws.cell(row=r, column=col)
        v = "" if c.value is None else str(c.value)
        m = "[%s：原标称 3MP，按口径归并为 2K]" % MARK
        if m not in v:
            c.value = (v + "；" + m) if v else m
    done.append((r, a, old))
print("\n3MP → 2K：")
for r, a, o in done:
    print("   行%-4d %-13s %s → 2K" % (r, a, o))

# ---------- 审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
na = 0
for r in range(2, aud.max_row + 1):
    fld = str(aud.cell(row=r, column=F).value or "").strip()
    if "摄像头分辨率" not in fld:
        continue
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    if a not in TARGETS:
        continue
    old = str(aud.cell(row=r, column=V).value or "").strip()
    aud.cell(row=r, column=V).value = "2K"
    st = str(aud.cell(row=r, column=S).value or "").strip()
    note = ("%s：原标称 3MP → 按口径归并为 2K（3MP 与 2K 在厂商标称中常指同一档）"
            % MARK)
    aud.cell(row=r, column=S).value = (st + "；" + note) if st else note
    na += 1
    print("   审计 行%-4d %-13s %s → 2K" % (r, a, old))
print("审计同步 %d 条" % na)

# ---------- 字段口径
fj = wb["字段口径"]
target_key = "摄像头分辨率（原标称）"
found = False
for r in range(2, fj.max_row + 1):
    if str(fj.cell(row=r, column=1).value or "").strip() == target_key:
        fj.cell(row=r, column=2).value = (
            "记录**厂商原标称**（1080P / 2K / 3MP / 4K…），不换算。"
            "**口径（2026-09-30）**：`3MP` 一律归并为 `2K`（厂商标称中二者常指同一档；"
            "实测行254 的 3MP 自证 = 2048x1536、行258 的「2K」卖点又写 2MP）。"
            "本列不是统一清晰度刻度，统计时不要按字面把 2K 与 3MP 当两个档位。")
        found = True
        break
if not found:
    rr0 = fj.max_row + 1
    fj.cell(row=rr0, column=1).value = target_key
    fj.cell(row=rr0, column=2).value = (
        "记录**厂商原标称**（1080P / 2K / 3MP / 4K…），不换算。"
        "**口径（2026-09-30）**：`3MP` 一律归并为 `2K`。")
print("字段口径 %s" % ("已更新" if found else "已新增一行"))

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（3MP→2K）"
n.cell(row=rr, column=2).value = (
    "按口径把「摄像头分辨率（原标称）」中 3 行的 `3MP` 归并为 `2K`（行270 B032DE4977、"
    "行271 B03DE062D5、行416 B0CE7339B1）。归并理由：厂商标称里 3MP 与 2K 常指同一档"
    "（行254 图片自证 `3MP 2048x1536`；行258/259 标题写 2K、卖点写 2MP）。"
    "该口径已写入「字段口径」；证据列（图片核心卖点/图片理解结论）保留原始视觉输出并加修正标记。"
    "不新增归一化等效像素列。")
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

# ---------- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
import collections  # noqa: E402
ri = h.index("摄像头分辨率（原标称）")
print("\n===== 复核 =====")
for k, v in collections.Counter("" if r[ri] is None else str(r[ri]).strip()
                                for r in rows).most_common():
    print("   %-10s %4d" % (k or "(空)", v))
print("仍为 3MP 的行数 =", sum(1 for r in rows if str(r[ri] or "").strip() == "3MP"))
