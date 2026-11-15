# -*- coding: utf-8 -*-
"""删除 Sheet1 中「产品类型 = 其他非自动喂食用品」的行（当前仅 1 行：行180 B08B9FCE1F）。

安全措施：
  · 先备份 `.bak-before-delete-others.xlsx`
  · 删前三重校验（行号/变体ASIN/旧值），不符即中止
  · 删后逐项核验：行数、超链接（第180行之下有 12 条，需随行上移）、
    下方行内容整体上移 1、DISPIMG 公式仍在、autoFilter 范围同步
  · 属性证据审计 保留历史证据，但给该 ASIN 打「已从交付表删除」标记
"""
from __future__ import annotations

import os
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
BASE = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, BASE)
BAK = P + ".bak-before-delete-others.xlsx"
TARGET_ROW = 180
TARGET_ASIN = "B08B9FCE1F"
TARGET_VAL = "其他非自动喂食用品"
MARK = "2026-09-29 人工处理"

shutil.copy2(P, BAK)
print("① 备份 ->", os.path.basename(BAK), os.path.getsize(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): i + 1 for i, c in enumerate(ws[1])}
last = ws.max_row
maxcol = ws.max_column

# ---------- ② 删前快照 + 三重校验
hits = [r for r in range(2, last + 1)
        if str(ws.cell(row=r, column=hdr["产品类型"]).value or "").strip() == TARGET_VAL]
assert hits == [TARGET_ROW], "期望仅第 %d 行，实际 %s" % (TARGET_ROW, hits)
assert str(ws.cell(row=TARGET_ROW, column=hdr["变体 ASIN"]).value).strip() == TARGET_ASIN
print("② 校验通过：行 %d = %s（%s）" % (TARGET_ROW, TARGET_ASIN, TARGET_VAL))

snap = {}
for r in range(TARGET_ROW + 1, last + 1):
    snap[r] = (
        str(ws.cell(row=r, column=hdr["变体 ASIN"]).value or ""),
        str(ws.cell(row=r, column=hdr["商品标题"]).value or "")[:40],
        (ws.cell(row=r, column=hdr["商品详情页链接"]).hyperlink.target
         if ws.cell(row=r, column=hdr["商品详情页链接"]).hyperlink else None),
    )
old_links = sum(1 for row in ws.iter_rows() for c in row if c.hyperlink)
print("   删前：%d 行数据，%d 条超链接（其中第 %d 行之下 %d 条）"
      % (last - 1, old_links, TARGET_ROW, len(snap)))

# ---------- ③ 删行 + 修 autoFilter
ws.delete_rows(TARGET_ROW, 1)
ws.auto_filter.ref = "A1:%s%d" % (openpyxl.utils.get_column_letter(maxcol), last - 1)

# ---------- ④ 同步审计 + 提取说明
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
            "属耐用配件而非耗材，但确非自动喂食器，故按口径从 Automatic Cat Feeders "
            "交付表剔除；证据在此保留备查）" % (MARK, TARGET_VAL))
    aud.cell(row=r, column=h2["审核状态"]).value = (st + "；" + note) if st else note
    n_aud += 1
print("③ 属性证据审计 标记 %d 条（证据保留）" % n_aud)

note_ws = wb["提取说明"]
r = note_ws.max_row + 2
note_ws.cell(row=r, column=1).value = MARK + "（删行）"
note_ws.cell(row=r, column=2).value = (
    "删除 Sheet1 中「产品类型 = 其他非自动喂食用品」的行共 1 行：变体 %s"
    "（SAROSORA Cat Feeder Stainless Steel Bowl，$10.99，父 ASIN B0EC63910C 下的食碗变体）。"
    "该行既非自动喂食器、也非耗材，按「Automatic Cat Feeders 只保留自动喂食器」的口径剔除；"
    "删除后 Sheet1 为 %d 行数据。其证据仍保留在「属性证据审计」以备查。"
    % (TARGET_ASIN, last - 1))
wb.save(P)
print("④ 已保存（提取说明 追加第 %d 行）" % r)

# ---------- ⑤ 删后核验
wb2 = openpyxl.load_workbook(P)
w2 = wb2["Sheet1"]
print("\n===== 删后核验 =====")
print("Sheet1 行数 = %d（表头1 + 数据%d，期望 %d）"
      % (w2.max_row, w2.max_row - 1, last - 1))
from collections import Counter
c = Counter(str(w2.cell(row=r, column=hdr["产品类型"]).value or "").strip()
            for r in range(2, w2.max_row + 1))
print("产品类型分布:", dict(c))
print("其他非自动喂食用品 =", c.get(TARGET_VAL, 0))
print("autoFilter =", w2.auto_filter.ref)
new_links = sum(1 for row in w2.iter_rows() for c2 in row if c2.hyperlink)
print("超链接数 = %d（删前 %d）" % (new_links, old_links))
bad = 0
for old_r, (asin, title, tgt) in snap.items():
    nr = old_r - 1
    if str(w2.cell(row=nr, column=hdr["变体 ASIN"]).value or "") != asin:
        bad += 1
        continue
    h = w2.cell(row=nr, column=hdr["商品详情页链接"]).hyperlink
    if (h.target if h else None) != tgt:
        bad += 1
print("第 %d 行之下整体上移 1 后内容/超链接错位数 = %d（应为 0）" % (TARGET_ROW, bad))
print("DISPIMG 公式仍在 E 列 =",
      str(w2.cell(row=2, column=5).value)[:40] if w2.cell(row=2, column=5).value else "空")
wb2.close()
