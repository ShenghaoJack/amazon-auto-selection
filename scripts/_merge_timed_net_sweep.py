# -*- coding: utf-8 -*-
"""合并落地：
  ① 用户决定：`程序定时干粮喂食器` 里带 WiFi/App 的 7 行 → 改为 `联网智能干粮喂食器`
  ② 图片全量复核（scripts/_net_sweep_result.json）命中行 → 回填联网方式；
     若命中行类型是 `程序定时干粮喂食器`，按同一口径改为 `联网智能干粮喂食器`
同步 属性证据摘要 / 属性证据审计 / 字段口径 / 提取说明。
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
BAK = P + ".bak-before-timed-net-merge.xlsx"
SWEEP = os.path.join("scripts", "_net_sweep_result.json")
MARK = "2026-09-30 人工修正"
SEG = re.compile(r"联网方式=[^；]*")
TIMED, NETTED = "程序定时干粮喂食器", "联网智能干粮喂食器"

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
NET, TYPE, SUMCOL, ASINC = (hdr["联网方式（GHz）"], hdr["产品类型"],
                            hdr["属性证据摘要"], hdr["变体 ASIN"])


def band_of(txt: str) -> str:
    t = (txt or "").lower()
    if "dual" in t or "2.4/5" in t or "2.4&5" in t:
        return "Wi‑Fi 2.4/5 GHz"
    if "5g" in t:
        return "Wi‑Fi 5 GHz"
    if "2.4" in t:
        return "Wi‑Fi 2.4 GHz"
    return "Wi‑Fi（频段未明确）"


# ---------- ① 已知 7 行
type_fix = []          # (row, asin, old_type, why)
for r in range(2, ws.max_row + 1):
    t = str(ws.cell(row=r, column=TYPE).value or "").strip()
    net = str(ws.cell(row=r, column=NET).value or "").strip()
    if t == TIMED and net not in ("", "未明确"):
        a = str(ws.cell(row=r, column=ASINC).value or "").strip()
        ws.cell(row=r, column=TYPE).value = NETTED
        type_fix.append((r, a, t, "已有联网值 %s" % net))

# ---------- ② 图片复核命中
sweep = json.load(open(SWEEP, encoding="utf-8"))
net_fill, type_fix2, checked, nohit = [], [], 0, 0
# 人工复核后剔除的假阳性 / 不适用的命中：
#   行145 证据是「No App 图标」—— 否定图标被模型读成"有 App"
#   行234 证据是「No WIFI Needed」—— 同上
#   行223 证据是手机 App 界面，但该产品是 **Bluetooth** 连接；
#        按既定口径「蓝牙不算联网方式」保持未明确
REJECT = {145: "证据为「No App」图标（否定），模型误判为有 App",
          234: "证据为「No WIFI Needed」（否定），模型误判为有 WiFi",
          223: "证据是手机 App，但该机型为 Bluetooth 连接；按口径蓝牙不算联网方式"}
NEGEV = re.compile(r"no\s*(?:app|wi[\s-]?fi)|no\s*app|without\s*(?:app|wi[\s-]?fi)", re.I)
for rec in sweep.get("results") or []:
    r0 = int(rec["row"])
    checked += 1
    ev0 = str(rec.get("ev") or "")
    if r0 in REJECT or NEGEV.search(ev0):
        print("   ⊘ 剔除 行%-4d %-13s %s" % (r0, rec.get("asin"),
                                            REJECT.get(r0) or "证据含否定词：%s" % ev0[:40]))
        nohit += 1
        continue
    if not (rec.get("phone") or rec.get("wifi")):
        nohit += 1
        continue
    r = int(rec["row"])
    a = str(rec["asin"])
    cur = str(ws.cell(row=r, column=NET).value or "").strip()
    band = band_of(str(rec.get("band") or ""))
    ev = "图片序号=%s；%s" % (rec.get("img"), rec.get("ev") or "")
    # 未明确 → 填；已是「频段未明确」而本次读到具体频段 → 升级
    if cur in ("", "未明确") or (cur == "Wi‑Fi（频段未明确）" and band != "Wi‑Fi（频段未明确）"):
        ws.cell(row=r, column=NET).value = band
        sc = ws.cell(row=r, column=SUMCOL)
        s = "" if sc.value is None else str(sc.value)
        if SEG.search(s):
            sc.value = SEG.sub("联网方式=%s" % band, s, count=1)
        net_fill.append((r, a, band, ev))
    if str(ws.cell(row=r, column=TYPE).value or "").strip() == TIMED:
        ws.cell(row=r, column=TYPE).value = NETTED
        type_fix2.append((r, a, TIMED, "图片复核发现手机/App"))

print("① 程序定时+联网 → 联网智能：%d 行" % len(type_fix))
for r, a, t, w in type_fix:
    print("   行%-4d %-13s %s（%s）" % (r, a, t, w))
print("\n② 图片复核：已检查 %d 行，无发现 %d 行，命中并回填 %d 行"
      % (checked, nohit, len(net_fill)))
for r, a, b, ev in net_fill:
    print("   行%-4d %-13s → %-18s %s" % (r, a, b, ev[:50]))
print("   命中行同时改类型 %d 行" % len(type_fix2))
for r, a, t, w in type_fix2:
    print("      行%-4d %-13s（%s）" % (r, a, w))

# ---------- 审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
fix_type = {a: w for _, a, _, w in (type_fix + type_fix2)}
fix_net = {a: (b, ev) for _, a, b, ev in net_fill}
nt = nf = 0
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=F).value or "").strip()
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    st = str(aud.cell(row=r, column=S).value or "").strip()

    def add(note, st=st, r=r):
        aud.cell(row=r, column=S).value = (st + "；" + note) if st else note

    if f == "产品类型" and a in fix_type:
        aud.cell(row=r, column=V).value = NETTED
        add("%s：%s，按「有 WiFi/App 即归联网智能干粮喂食器」口径改为 %s"
            % (MARK, fix_type[a], NETTED))
        nt += 1
    elif f in ("联网方式", "联网方式（GHz）") and a in fix_net:
        b, ev = fix_net[a]
        aud.cell(row=r, column=V).value = b
        add("%s：图片复核发现手机/App → 回填 %s（%s）" % (MARK, b, ev[:40]))
        nf += 1
print("\n审计同步: 产品类型 %d 条 / 联网方式 %d 条" % (nt, nf))

# ---------- 字段口径
fj = wb["字段口径"]
for r in range(2, fj.max_row + 1):
    if str(fj.cell(row=r, column=1).value or "").strip() == "产品类型":
        fj.cell(row=r, column=2).value = (
            "按自动化/联网/摄像/湿粮冷藏/宠物识别/重力式等产品形态归类。"
            "**口径（2026-09-30）**：有 WiFi/App 证据的一律归「联网智能干粮喂食器」，"
            "与「程序定时干粮喂食器」（无网定时器）互斥；"
            "「WiFi 喂食器 + 重力水碗」二合一的也归联网智能，不归重力式。")
        break

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（类型/联网收口）"
n.cell(row=rr, column=2).value = (
    "① 按用户口径：`程序定时干粮喂食器` 里带 WiFi/App 的 %d 行改为 `联网智能干粮喂食器`。"
    "② 对「联网方式=未明确 且 非重力式」的 %d 行做了**全量图片复核**"
    "（判据：图中是否出现手机/平板/App 界面、路由器/WiFi 图标或 WiFi/APP/Smart/2.4G/5G 字样）："
    "命中 %d 行并回填联网方式，其余 %d 行保持未明确（图片复核亦未见联网线索）。"
    "③ 口径说明：单独出现的 `2.4G` 一律按 WiFi 2.4 GHz 计（用户决定）。"
    % (len(type_fix) + len(type_fix2), checked, len(net_fill), nohit))
wb.save(P)
print("字段口径/提取说明 已更新；保存完成")

# ---------- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
import collections  # noqa: E402
ti, ni = h.index("产品类型"), h.index("联网方式（GHz）")
print("\n===== 复核 =====")
tab = collections.defaultdict(collections.Counter)
for r in rows:
    tab[str(r[ti]).strip()][str(r[ni]).strip()] += 1
for t, c in sorted(tab.items(), key=lambda x: -sum(x[1].values())):
    net = sum(v for k, v in c.items() if k != "未明确")
    print("  %-14s 共%3d | 有联网%3d | %s" % (t, sum(c.values()), net, dict(c.most_common())))
