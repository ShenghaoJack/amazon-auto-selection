# -*- coding: utf-8 -*-
"""撤销 2 处误填的联网方式（复核发现）：
  · B01A69D32D（行67 重力式喂食器）：唯一"证据"是 OCR 里的 `45g`，被正则 `5\\s*g` 误当成 5G。无任何联网证据。
  · B0D70D1C70（行223 程序定时干粮喂食器）：证据只有 Bluetooth（`Bluetooth APP Control`），
    按既定口径「Bluetooth 不算联网方式」应保持未明确。
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-net-revert2.xlsx"
MARK = "2026-09-30 人工修正"
REVERT = {"B01A69D32D": "误填：唯一线索是 OCR 的 `45g`，被正则误判为 5G；无联网证据",
          "B0D70D1C70": "误填：证据仅 Bluetooth；按口径蓝牙不算联网方式"}
SEG = re.compile(r"联网方式=[^；]*")

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
NET, SUMCOL, ASINC = hdr["联网方式（GHz）"], hdr["属性证据摘要"], hdr["变体 ASIN"]

done = []
for r in range(2, ws.max_row + 1):
    a = str(ws.cell(row=r, column=ASINC).value or "").strip()
    if a not in REVERT:
        continue
    old = str(ws.cell(row=r, column=NET).value or "")
    ws.cell(row=r, column=NET).value = "未明确"
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG.search(s):
        sc.value = SEG.sub("联网方式=未明确", s, count=1)
    done.append((r, a, old))
    print("  行%-4d %-13s %s → 未明确   （%s）" % (r, a, old, REVERT[a]))

aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
n = 0
for r in range(2, aud.max_row + 1):
    if str(aud.cell(row=r, column=F).value or "").strip() not in ("联网方式", "联网方式（GHz）"):
        continue
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    if a not in REVERT:
        continue
    aud.cell(row=r, column=V).value = "未明确"
    st = str(aud.cell(row=r, column=S).value or "").strip()
    note = "%s：撤销回填 → 未明确（%s）" % (MARK, REVERT[a])
    aud.cell(row=r, column=S).value = (st + "；" + note) if st else note
    n += 1
print("审计同步 %d 条" % n)

nn = wb["提取说明"]
rr = nn.max_row + 2
nn.cell(row=rr, column=1).value = MARK + "（撤销误填）"
nn.cell(row=rr, column=2).value = (
    "撤销 2 处联网方式回填：① B01A69D32D（重力式喂食器）—— 唯一线索是图片 OCR 里的 `45g`，"
    "被正则 `5\\s*g` 误当成 5G，无任何联网证据；② B0D70D1C70 —— 证据仅 Bluetooth，"
    "按「蓝牙不算联网方式」口径保持「未明确」。"
    "另注：B047043728 / B063EB0692 / B01CA5FED0 三行的文本是"
    "「doesn't need to be connected to WiFi to set up … WiFi disconnection」——"
    "语义为「不依赖 WiFi 也能用」但同时提到 WiFi 断开，属**存疑**，已按「存在 WiFi」回填，可据口径调整。")
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
import collections  # noqa: E402
ni = h.index("联网方式（GHz）")
print("\n复核 联网方式分布:", dict(collections.Counter(
    "" if r[ni] is None else str(r[ni]).strip() for r in rows).most_common()))
