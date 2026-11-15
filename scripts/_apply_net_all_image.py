# -*- coding: utf-8 -*-
"""全图版网络扫描的净新增：1 行（行165 B003A64E6A，图中有手机同框）。
另 3 行命中为假阳性，明确剔除并记录原因。"""
from __future__ import annotations

import json
import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-net-all-image.xlsx"
R = os.path.join("scripts", "_net_sweep_result_all.json")
MARK = "2026-09-30 人工修正"
SEG = re.compile(r"联网方式=[^；]*")
REJECT = {
    "B0CC07E219": "图上是「No App」图标（否定），模型误判为有 App",
    "B0E6099D5D": "唯一线索是「Smart Warming」字样，与联网无关，属误判",
    "B0B50AA7D2": "图上写「No WIFI Needed」（否定），模型误判为有 WiFi",
}
TIMED, NETTED = "程序定时干粮喂食器", "联网智能干粮喂食器"

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))
d = json.load(open(R, encoding="utf-8"))
hits = [x for x in d["results"] if x.get("phone") or x.get("wifi")]
print("扫描命中 %d 行：" % len(hits))
apply_, reject = [], []
for x in hits:
    a = x["asin"]
    if a in REJECT:
        reject.append((x["row"], a, REJECT[a]))
    else:
        apply_.append(x)
for r, a, why in reject:
    print("   ⊘ 剔除 行%-4d %-13s %s" % (r, a, why))
print("   → 实际应用 %d 行" % len(apply_))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
NET, TYPE, SUMCOL, ASINC = (hdr["联网方式（GHz）"], hdr["产品类型"],
                            hdr["属性证据摘要"], hdr["变体 ASIN"])
by_asin = {str(ws.cell(row=r, column=ASINC).value or "").strip(): r
           for r in range(2, ws.max_row + 1)}
done = []
for x in apply_:
    a = x["asin"]
    r = by_asin.get(a)
    if r is None:
        continue
    cur = str(ws.cell(row=r, column=NET).value or "").strip()
    if cur not in ("", "未明确"):
        continue
    ws.cell(row=r, column=NET).value = "Wi‑Fi（频段未明确）"
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG.search(s):
        sc.value = SEG.sub("联网方式=Wi‑Fi（频段未明确）", s, count=1)
    old_type = str(ws.cell(row=r, column=TYPE).value or "").strip()
    if old_type == TIMED:
        ws.cell(row=r, column=TYPE).value = NETTED
    done.append((r, a, old_type, str(x.get("ev") or "")[:50]))
for r, a, ot, ev in done:
    print("   ✓ 行%-4d %-13s 联网=Wi‑Fi（频段未明确）%s | %s"
          % (r, a, "，类型 %s→%s" % (ot, NETTED) if ot == TIMED else "", ev))

aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
na = 0
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=F).value or "").strip()
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    if a not in {x[1] for x in done}:
        continue
    st = str(aud.cell(row=r, column=S).value or "").strip()
    if f in ("联网方式", "联网方式（GHz）"):
        aud.cell(row=r, column=V).value = "Wi‑Fi（频段未明确）"
        aud.cell(row=r, column=S).value = (st + "；" if st else "") + \
            "%s：全图复核见手机同框 → 回填 Wi‑Fi（频段未明确）" % MARK
        na += 1
    elif f == "产品类型":
        aud.cell(row=r, column=V).value = NETTED
        aud.cell(row=r, column=S).value = (st + "；" if st else "") + \
            "%s：有联网证据 → 改为 %s" % (MARK, NETTED)
        na += 1
print("审计同步 %d 条" % na)

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（联网·全图复核）"
n.cell(row=rr, column=2).value = (
    "对剩余「联网方式=未明确 且 非重力式」的 129 行做了**全图版**复核（此前误限 8 张，本表平均 7.8 张）。"
    "命中 4 行，其中 3 行是假阳性已剔除（B0CC07E219「No App」图标、B0B50AA7D2「No WIFI Needed」、"
    "B0E6099D5D 仅「Smart Warming」字样）；净新增 1 行：B003A64E6A 图中有手机同框 → "
    "联网方式回填 Wi‑Fi（频段未明确）。")
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)
