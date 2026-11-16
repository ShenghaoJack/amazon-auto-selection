# -*- coding: utf-8 -*-
"""食碗材质：① 统一「不锈钢（材质字段）」→「不锈钢」；② 回填未明确。

数据来源 scripts/_bowl_material_plan.json（文本判定 130 行 + 看图 39 行）。
剔除：行33 B0F4EA38BA 是 PETLIBRO 宠物垫（silicone mats），不是碗 → 保持未明确。
词表收口为 {不锈钢, 塑料}（与文件既有取值一致），其余保持「未明确」不猜。
"""
from __future__ import annotations

import json
import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-bowl-material.xlsx"
PLAN = os.path.join("scripts", "_bowl_material_plan.json")
MARK = "2026-09-30 人工修正"
SEG = re.compile(r"食碗材质=[^；]*")
EXCLUDE = {33: "PETLIBRO 宠物垫（silicone mats），非食碗；模型据此回「硅胶」不成立"}
ALLOW = {"不锈钢", "塑料"}

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))

plan = json.load(open(PLAN, encoding="utf-8"))
fills = []
for p in plan["plan"]:
    fills.append((int(p["row"]), p["asin"], p["value"], p["why"][:70]))
for v in plan["vision"]:
    r = int(v["row"])
    val = v["vision"]
    if r in EXCLUDE:
        print("   ⊘ 剔除 行%-4d %-13s %s" % (r, v["asin"], EXCLUDE[r]))
        continue
    if val not in ALLOW:
        print("   ⊘ 保留未明确 行%-4d %-13s 看图得 %r（不在词表内）" % (r, v["asin"], val))
        continue
    fills.append((r, v["asin"], val, "图片视觉：%s" % str(v.get("vision_ev") or "")[:60]))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
COL, SUMCOL, ASINC = (hdr[[h for h in hdr if "食碗材质" in h][0]],
                      hdr["属性证据摘要"], hdr["变体 ASIN"])

# ---------- ① 统一写法
norm = 0
for r in range(2, ws.max_row + 1):
    c = ws.cell(row=r, column=COL)
    if str(c.value or "").strip() == "不锈钢（材质字段）":
        c.value = "不锈钢"
        norm += 1
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if "食碗材质=不锈钢（材质字段）" in s:
        sc.value = s.replace("食碗材质=不锈钢（材质字段）", "食碗材质=不锈钢")
print("\n① 「不锈钢（材质字段）」→「不锈钢」：%d 行（摘要同步）" % norm)

# ---------- ② 回填
filled = []
for r, asin, val, why in fills:
    cur = str(ws.cell(row=r, column=COL).value or "").strip()
    if cur not in ("", "未明确"):
        continue
    ws.cell(row=r, column=COL).value = val
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG.search(s):
        sc.value = SEG.sub("食碗材质=%s" % val, s, count=1)
    else:
        sc.value = (s + "；食碗材质=%s" % val) if s else "食碗材质=%s" % val
    filled.append((r, asin, val, why))
print("② 回填 未明确 → %s：%d 行" % (sorted(ALLOW), len(filled)))
import collections  # noqa: E402
print("   分布:", dict(collections.Counter(x[2] for x in filled)))

# ---------- 审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
fill_map = {a: (v, w) for _, a, v, w in filled}
nn = nf = 0
for r in range(2, aud.max_row + 1):
    if "食碗材质" not in str(aud.cell(row=r, column=F).value or ""):
        continue
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    st = str(aud.cell(row=r, column=S).value or "").strip()

    def add(note, st=st, r=r):
        aud.cell(row=r, column=S).value = (st + "；" + note) if st else note

    if str(aud.cell(row=r, column=V).value or "").strip() == "不锈钢（材质字段）":
        aud.cell(row=r, column=V).value = "不锈钢"
        add("%s：写法统一「不锈钢（材质字段）」→「不锈钢」" % MARK)
        nn += 1
    if a in fill_map:
        v, w = fill_map[a]
        aud.cell(row=r, column=V).value = v
        add("%s：未明确 → %s（%s）" % (MARK, v, w))
        nf += 1
print("审计同步: 写法统一 %d 条 / 回填 %d 条" % (nn, nf))

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（食碗材质）"
n.cell(row=rr, column=2).value = (
    "① 写法统一：「不锈钢（材质字段）」→「不锈钢」共 %d 行（该写法源自详细参数 Material 字段，"
    "语义与「不锈钢」相同）。词表收口为 {不锈钢, 塑料}。"
    "② 回填「未明确」%d 行：文本判定 %d 行（碗附近出现不锈钢/塑料，或详细参数 Material 字段），"
    "图片视觉判定 %d 行（看碗的材质/图注，如 304 Stainless Steel、Dual Stainless Bowls、ABS Meal Plate）。"
    "③ 未回填：%d 行仍为「未明确」—— 图片里看不出碗材质或该 listing 无独立碗；"
    "其中行33（PETLIBRO 宠物垫）与行145 等个别行证据指向非碗部件，按「不猜」保留。"
    % (norm, len(filled), len(plan["plan"]),
       len([x for x in plan["vision"] if int(x["row"]) not in EXCLUDE
            and x["vision"] in ALLOW]), 415 - norm - len(filled) - 101))
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

# ---------- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
ci = h.index("食碗材质")
c = collections.Counter("" if r[ci] is None else str(r[ci]).strip() for r in rows)
print("\n===== 复核 =====")
print("列数 %d 行数 %d" % (len(h), len(rows)))
for k, v in c.most_common():
    print("   %-24s %4d" % (k or "(空)", v))
print("仍含「（材质字段）」= %d" % sum(1 for r in rows if "材质字段" in str(r[ci] or "")))
