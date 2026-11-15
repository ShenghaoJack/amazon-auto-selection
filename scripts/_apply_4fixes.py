# -*- coding: utf-8 -*-
"""一次性处理 4 件事（上一版因文件被 WPS 占用未写成功）：
  ① 每日最大餐数 = 0 的 5 行 → 10（图片视觉重读证据：`1-10 Meals Per Day`）
  ② 每餐份数范围（份/餐）→ 取最大值（43 行：36 个范围 + 7 个 ≤N）
  ③ 联网方式 Bluetooth → 未明确（蓝牙不是网络连接）
  ④ 删除「备用电池规格（数量×型号）」列 + 字段口径对应说明
均同步 属性证据摘要 / 属性证据审计 / 字段口径 / 提取说明。
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl
from openpyxl.utils import get_column_letter

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-meals-range-net-battery.xlsx"
MARK = "2026-09-30 人工修正"
NUM = re.compile(r"\d+(?:\.\d+)?")
SEG_MEAL = re.compile(r"每日最大餐数=[^；]*")
SEG_RANGE = re.compile(r"每餐份数范围=[^；]*")
SEG_NET = re.compile(r"联网方式=[^；]*")
DELNAME = "备用电池规格（数量×型号）"
VISION_EV = "1-10 Meals Per Day"

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK), os.path.getsize(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
MEAL, RANGE, NET = hdr["每日最大餐数（餐/天）"], hdr["每餐份数范围（份/餐）"], hdr["联网方式（GHz）"]
SUMCOL, ASINC = hdr["属性证据摘要"], hdr["变体 ASIN"]
DELCOL = hdr[DELNAME]
rows_n = ws.max_row

# ---------- ① 每日最大餐数 0 → 10
meal_fix = []
for r in range(2, rows_n + 1):
    c = ws.cell(row=r, column=MEAL)
    v = "" if c.value is None else str(c.value).strip()
    if v in ("0", "0.0"):
        a = str(ws.cell(row=r, column=ASINC).value or "").strip()
        c.value = 10
        sc = ws.cell(row=r, column=SUMCOL)
        s = "" if sc.value is None else str(sc.value)
        if SEG_MEAL.search(s):
            sc.value = SEG_MEAL.sub("每日最大餐数=10餐/天", s, count=1)
        meal_fix.append((r, a, v))
print("\n① 每日最大餐数 0 → 10：%d 行" % len(meal_fix))
for r, a, v in meal_fix:
    print("    行%-4d %-13s %s → 10  （视觉证据: %s）" % (r, a, v, VISION_EV))

# ---------- ② 每餐份数范围 → 取最大
range_fix = []
for r in range(2, rows_n + 1):
    c = ws.cell(row=r, column=RANGE)
    v = "" if c.value is None else str(c.value).strip()
    if not v or v == "未明确":
        continue
    nums = NUM.findall(v)
    if not nums:
        continue
    mx = max(float(x) for x in nums)
    new = int(mx) if float(mx).is_integer() else mx
    if str(new) == v:
        continue
    c.value = new
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG_RANGE.search(s):
        sc.value = SEG_RANGE.sub("每餐份数范围=%s份/餐" % new, s, count=1)
    range_fix.append((r, str(ws.cell(row=r, column=ASINC).value or "").strip(), v, new))
print("\n② 每餐份数范围 → 取最大：%d 行" % len(range_fix))
print("    %-7s %-13s %-10s %s" % ("Excel行", "变体ASIN", "原值", "新值"))
for r, a, v, new in range_fix[:12]:
    print("    %-7d %-13s %-10s %s" % (r, a, v, new))
if len(range_fix) > 12:
    print("    …（其余 %d 行同理）" % (len(range_fix) - 12))

# ---------- ③ Bluetooth → 未明确
net_fix = []
for r in range(2, rows_n + 1):
    c = ws.cell(row=r, column=NET)
    v = "" if c.value is None else str(c.value).strip()
    if "Bluetooth" not in v:
        continue
    c.value = "未明确"
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG_NET.search(s):
        sc.value = SEG_NET.sub("联网方式=未明确", s, count=1)
    net_fix.append((r, str(ws.cell(row=r, column=ASINC).value or "").strip(), v))
print("\n③ 联网方式 Bluetooth → 未明确：%d 行" % len(net_fix))
for r, a, v in net_fix:
    print("    行%-4d %-13s %s → 未明确" % (r, a, v))

# ---------- 属性证据审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, ST = h2["属性字段"], h2["提取值"], h2["审核状态"]
A2, E2 = h2["变体ASIN"], h2["证据原文"]
meal_asin = {a for _, a, _ in meal_fix}
range_asin = {a for _, a, _, _ in range_fix}
net_asin = {a for _, a, _ in net_fix}
range_map = {a: new for _, a, _, new in range_fix}
cnt = {"meal": 0, "range": 0, "net": 0}
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=F).value or "").strip()
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    st = str(aud.cell(row=r, column=ST).value or "").strip()

    def add(note):
        aud.cell(row=r, column=ST).value = (st + "；" + note) if st else note

    if f in ("每日最大餐数", "每日最大餐数（餐/天）") and a in meal_asin:
        aud.cell(row=r, column=V).value = 10
        add("%s：原值为 0（OCR 把 `1-10 Meals Per Day` 误读为 `一 ] 0`）；"
            "图片视觉重读确认为 **10 餐/天**，证据原文：%s" % (MARK, VISION_EV))
        cnt["meal"] += 1
    elif f in ("每餐份数范围", "每餐份数范围（份/餐）") and a in range_asin:
        aud.cell(row=r, column=V).value = range_map[a]
        add("%s：按「取最大值」口径改为 %s" % (MARK, range_map[a]))
        cnt["range"] += 1
    elif f in ("联网方式", "联网方式（GHz）") and a in net_asin:
        aud.cell(row=r, column=V).value = "未明确"
        add("%s：Bluetooth 不算联网方式（蓝牙不是网络连接）→ 未明确" % MARK)
        cnt["net"] += 1
print("\n属性证据审计 同步: 餐数 %d / 份数范围 %d / 联网 %d" % (cnt["meal"], cnt["range"], cnt["net"]))

# ---------- ④ 删列（放最后，避免列号位移）
before = ws.max_column
nonblank = sum(1 for r in range(2, rows_n + 1)
               if str(ws.cell(row=r, column=DELCOL).value or "").strip() not in ("", "未明确"))
ws.delete_cols(DELCOL, 1)
ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(ws.max_column), ws.max_row)
print("\n④ 删除「%s」（原第 %d 列，%d 行有值）→ 列数 %d→%d, autoFilter=%s"
      % (DELNAME, DELCOL, nonblank, before, ws.max_column, ws.auto_filter.ref))

fj = wb["字段口径"]
rm = None
for r in range(fj.max_row, 1, -1):
    k = str(fj.cell(row=r, column=1).value or "")
    if DELNAME in k:
        rm = k
        fj.delete_rows(r, 1)
    elif k.strip() == "每餐份数范围（份/餐）":
        fj.cell(row=r, column=2).value = ("范围写法（1–15、≤50 等）统一取**最大值**填入单元格；"
                                          "无法判定时写「未明确」。（2026-09-30 口径变更）")
print("    字段口径：删除「%s」说明行；更新「每餐份数范围」口径" % (rm or "未找到"))

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（餐数/范围/联网/删列）"
n.cell(row=rr, column=2).value = (
    "①「每日最大餐数（餐/天）」5 行为 0（物理不可能）：图片视觉重读均为 `1-10 Meals Per Day` → 改为 10；"
    "原 OCR 把 `1-10` 读成 `一 ] 0`。"
    "②「每餐份数范围（份/餐）」%d 行按「取最大值」口径改为单一数字（1–15→15、≤50→50、0–24→24…）。"
    "③「联网方式（GHz）」1 行 Bluetooth → 未明确（蓝牙不算联网方式）。"
    "④ 删除「%s」列（%d 行有值、%d 行未明确），证据保留在属性证据审计。"
    % (len(range_fix), DELNAME, nonblank, rows_n - 1 - nonblank))
wb.save(P)
print("    提取说明 追加第 %d 行；已保存" % rr)

# ---------- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
import collections  # noqa: E402
print("\n===== 复核 =====")
print("列数 = %d（应 121） 行数 = %d" % (len(h), len(rows)))
print("「%s」已删除 = %s" % (DELNAME, DELNAME not in h))
ci = h.index("每日最大餐数（餐/天）")
c = collections.Counter("" if r[ci] is None else str(r[ci]).strip() for r in rows)
print("每日最大餐数分布:", dict(c.most_common()))
ri = h.index("每餐份数范围（份/餐）")
c2 = collections.Counter("" if r[ri] is None else str(r[ri]).strip() for r in rows)
print("每餐份数范围分布:", dict(c2.most_common(12)))
sym = sum(1 for r in rows if re.search(r"[≤≥<>]|[-–—~]", str(r[ri] or "")))
print("每餐份数范围里仍含符号/范围的行 =", sym)
ni = h.index("联网方式（GHz）")
print("联网方式仍含 Bluetooth =", sum(1 for r in rows if "lue" in str(r[ni] or "")))
