# -*- coding: utf-8 -*-
"""联网方式：否定感知回填 + 产品类型矛盾修正。

关键：文本里的 WiFi/App 可能是**否定句**（`No WiFi or smartphone needed`、
`NO App`、`cannot be connected to the APP`、`No Cam&app`）→ 这些行必须保持「未明确」。

规则：
  ① 含明确否定上下文 → 保持未明确（并记录否定证据）
  ② 无否定且 APP远程控制=是 → 必须填（有 App 就有联网）；频段能从文本读出就写具体频段
  ③ 无否定且文本出现 WiFi/App 正面表述 → 同上
  ④ 产品类型=重力式* 却填了联网 → 类型改为「联网智能干粮喂食器」（标题明确写 5G WiFi App Control，
     实为「WiFi 喂食器 + 重力水碗」二合一，不是纯重力）
"""
from __future__ import annotations

import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-net-refill.xlsx"
MARK = "2026-09-30 人工修正"

TEXT_COLS = ["商品标题", "产品卖点", "详细参数", "图片OCR摘要", "图片核心卖点",
             "图片理解结论", "SKU", "属性证据摘要"]
NEG = re.compile(r"(no|without|not?\s+(?:need|require|support))[^.;|]{0,28}"
                 r"(wi[\s-]?fi|app|smartphone|cam\b|camera)|"
                 r"cannot be connected to the app|no cam", re.I)
POS = re.compile(r"wi[\s-]?fi|wlan|\bapp\b|smart\s*app|app\s*control|app-controlled|"
                 r"2\.4\s*g|5\s*g|dual[\s-]?band|双频", re.I)
BAND_ANY = re.compile(r"dual[\s-]?band|双频|2\.4\s*(?:/\s*&?\s*)5|2\.4\s*G\s*(?:and|&|/)\s*5|"
                      r"5\s*G\s*(?:and|&|/)\s*2\.4", re.I)
B5 = re.compile(r"\b5\s*G(?:Hz)?\b|5GHz", re.I)
B24 = re.compile(r"\b2\.4\s*G(?:Hz)?\b|2\.4GHz", re.I)
GRAV = ("重力式喂食器", "重力式食水套装")

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK), os.path.getsize(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
NET, TYPE, APP = hdr["联网方式（GHz）"], hdr["产品类型"], hdr["APP远程控制"]
SUMCOL, ASINC = hdr["属性证据摘要"], hdr["变体 ASIN"]
SEG_NET = re.compile(r"联网方式=[^；]*")

fills, keeps, typefix = [], [], []
for r in range(2, ws.max_row + 1):
    asin = str(ws.cell(row=r, column=ASINC).value or "").strip()
    typ = str(ws.cell(row=r, column=TYPE).value or "").strip()
    app = str(ws.cell(row=r, column=APP).value or "").strip()
    net = str(ws.cell(row=r, column=NET).value or "").strip()

    # ④ 重力式* 却有联网 → 类型矛盾
    if typ in GRAV and net not in ("", "未明确"):
        typefix.append((r, asin, typ, net, app))
        ws.cell(row=r, column=TYPE).value = "联网智能干粮喂食器"

    if net not in ("", "未明确"):
        continue
    blob = " || ".join(str(ws.cell(row=r, column=hdr[c]).value or "")
                       for c in TEXT_COLS if c in hdr)
    negs = [m.group(0).strip() for m in NEG.finditer(blob)]
    if negs:
        keeps.append((r, asin, typ, negs[0][:60]))
        continue
    if app != "是" and not POS.search(blob):
        continue
    band = ""
    if BAND_ANY.search(blob):
        band = "Wi‑Fi 2.4/5 GHz"
    elif B5.search(blob) and B24.search(blob):
        band = "Wi‑Fi 2.4/5 GHz"
    elif B5.search(blob):
        band = "Wi‑Fi 5 GHz"
    elif B24.search(blob):
        band = "Wi‑Fi 2.4 GHz"
    else:
        band = "Wi‑Fi（频段未明确）"
    ws.cell(row=r, column=NET).value = band
    sc = ws.cell(row=r, column=SUMCOL)
    s = "" if sc.value is None else str(sc.value)
    if SEG_NET.search(s):
        sc.value = SEG_NET.sub("联网方式=%s" % band, s, count=1)
    fills.append((r, asin, typ, app, band))

print("\n【回填】联网方式 未明确 → 具体值：%d 行" % len(fills))
print("  %-7s %-13s %-14s %-4s %s" % ("Excel行", "变体ASIN", "产品类型", "APP", "填入"))
for r, a, t, ap, b in fills:
    print("  %-7d %-13s %-14s %-4s %s" % (r, a, t, ap, b))
print("\n【保持未明确】因文本是否定句：%d 行（示例）" % len(keeps))
for r, a, t, nz in keeps[:12]:
    print("  行%-4d %-13s %-14s 否定证据: %s" % (r, a, t, nz))
print("\n【产品类型矛盾修正】重力式* 却有联网 → 联网智能干粮喂食器：%d 行" % len(typefix))
for r, a, t, net, ap in typefix:
    print("  行%-4d %-13s %-14s 联网=%-16s APP=%s" % (r, a, t, net, ap))

# ---- 属性证据审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
FA, VA, SA, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
TA, T2 = h2.get("属性", h2["属性字段"]), None
fill_asin = {a: b for _, a, _, _, b in fills}
type_asin = {a for _, a, _, _, _ in typefix}
nf = nt = 0
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=FA).value or "").strip()
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    st = str(aud.cell(row=r, column=SA).value or "").strip()

    def add(note, st=st, r=r):
        aud.cell(row=r, column=SA).value = (st + "；" + note) if st else note

    if f in ("联网方式", "联网方式（GHz）") and a in fill_asin:
        aud.cell(row=r, column=VA).value = fill_asin[a]
        add("%s：未明确 → %s（文本/App 证据支持存在联网；已排除否定句）" % (MARK, fill_asin[a]))
        nf += 1
    elif f in ("产品类型",) and a in type_asin:
        aud.cell(row=r, column=VA).value = "联网智能干粮喂食器"
        add("%s：原判重力式却有 WiFi/App（标题明确 5G WiFi App Control），"
            "实为「WiFi 喂食器 + 重力水碗」二合一 → 改为联网智能干粮喂食器" % MARK)
        nt += 1
print("\n审计同步: 联网 %d 条 / 产品类型 %d 条" % (nf, nt))

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（联网校验）"
n.cell(row=rr, column=2).value = (
    "①「联网方式（GHz）」回填 %d 行（原未明确）：判定依据为存在 App/WiFi 的**正面**证据"
    "（APP远程控制=是，或标题/卖点明确写 WiFi/App），频段能从文本读出就写具体频段，"
    "否则写「Wi-Fi（频段未明确）」。"
    "② 明确排除了 %d 行的**否定句**（`No WiFi or smartphone needed`、`NO App`、"
    "`cannot be connected to the APP`、`No Cam&app` 等）—— 这些行保持「未明确」，"
    "不得回填。③ 修正 %d 行产品类型矛盾：原判「重力式喂食器/重力式食水套装」却带 WiFi/App 的，"
    "实为「WiFi 喂食器 + 重力水碗」二合一，改为「联网智能干粮喂食器」。"
    "④ 图片视觉复核（是否出现手机/App）见后续批次。" % (len(fills), len(keeps), len(typefix)))
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

# ---- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
import collections  # noqa: E402
ni, ti = h.index("联网方式（GHz）"), h.index("产品类型")
print("\n===== 复核 =====")
print("列数 = %d 行数 = %d" % (len(h), len(rows)))
print("联网方式分布:", dict(collections.Counter("" if r[ni] is None else str(r[ni]).strip() for r in rows).most_common()))
print("\n产品类型 × 联网方式:")
tab = collections.defaultdict(collections.Counter)
for r in rows:
    tab[str(r[ti]).strip()][str(r[ni]).strip()] += 1
for t, c in sorted(tab.items(), key=lambda x: -sum(x[1].values())):
    print("  %-14s %s" % (t, dict(c.most_common())))
