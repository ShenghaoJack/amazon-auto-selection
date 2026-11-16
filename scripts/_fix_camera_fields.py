# -*- coding: utf-8 -*-
"""摄像头字段收口：
  ① 行187 B020BFBAFF  分辨率 4K → 未明确（重力式喂食器无摄像头，"4K"系 OCR 把 `2．4K` 误读）
  ② 行134 B037AA1140  摄像头 是 → 未明确（其 OCR 摘要原文明写 `this product does not have a camera`）
  ③ 行362 B0A5E34961  摄像头 是 → 未明确（它是 Slow Feeder Bowl 慢食碗，卖点说的是"适配带摄像头的喂食器"）
  ④ 行98/247/341：摄像头=是但分辨率未明确 → high + 全部图片看图补
同步 属性证据摘要 / 图片核心卖点 / 图片理解结论（追加修正标记）/ 属性证据审计 / 提取说明。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-camera-fix.xlsx"
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
MARK = "2026-09-30 人工修正"
SEG_CAM = re.compile(r"摄像头=[^；]*")
SEG_RES = re.compile(r"摄像头分辨率=[^；]*")
ALLOW = {"1080P", "2K", "4K", "3MP"}

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
CAM, RES = hdr["摄像头"], hdr["摄像头分辨率（原标称）"]
SUMCOL, ASINC = hdr["属性证据摘要"], hdr["变体 ASIN"]
SUBJ, CONC = hdr["图片核心卖点"], hdr["图片理解结论"]
by_asin = {str(ws.cell(row=r, column=ASINC).value or "").strip(): r
           for r in range(2, ws.max_row + 1)}


def note(col, r, mark):
    c = ws.cell(row=r, column=col)
    s = "" if c.value is None else str(c.value)
    if mark in s:
        return
    c.value = (s + "；[%s：%s]" % (MARK, mark)) if s else "[%s：%s]" % (MARK, mark)


def set_sum(col, r, pat, repl):
    c = ws.cell(row=r, column=col)
    s = "" if c.value is None else str(c.value)
    if pat.search(s):
        c.value = pat.sub(repl, s, count=1)
    else:
        c.value = (s + "；" + repl) if s else repl


# ---------- ①②③ 三处明确错误
errs = [("B020BFBAFF", RES, "未明确", SEG_RES, "摄像头分辨率=未明确",
         "原判 4K 系图片 OCR 把 `2．4K` 误读；该机为重力式喂食器，无摄像头"),
        ("B037AA1140", CAM, "未明确", SEG_CAM, "摄像头=未明确",
         "该行 OCR 摘要原文即写明 `this product does not have a camera`"),
        ("B0A5E34961", CAM, "未明确", SEG_CAM, "摄像头=未明确",
         "该 listing 为 PETLIBRO 慢食碗；卖点说的是「适配带摄像头的喂食器」，非本机带摄像头")]
fixed = []
for asin, col, val, pat, repl, why in errs:
    r = by_asin[asin]
    old = str(ws.cell(row=r, column=col).value or "").strip()
    ws.cell(row=r, column=col).value = val
    set_sum(SUMCOL, r, pat, repl)
    note(SUBJ, r, why)
    note(CONC, r, why)
    fixed.append((r, asin, ("摄像头" if col == CAM else "分辨率"), old, val, why))
print("\n① 三处明确错误：")
for r, a, f, o, nv, why in fixed:
    print("   行%-4d %-13s %s：%s → %s   （%s）" % (r, a, f, o, nv, why[:44]))

# ---------- ④ 看图补 3 行分辨率
PROMPT = """这是同一款宠物喂食器的商品图片（多张，按顺序编号）。
请找出**摄像头分辨率**的标称（相机像素/清晰度），英文常见：1080P / Full HD / 2K / 4K / 3MP / 2MP，
也可能写成 `1920x1080`、`2304x1296`、`2560x1440`、`3840x2160`。

严格输出 JSON：
{"分辨率": "1080P | 2K | 4K | 3MP | 未明确", "证据原文": "<图上原文片段>", "图片序号": <整数或null>}
只有图上明确写出/能明确换算时才给值；看不出就填 "未明确"。不要解释。"""
cfg = Config.load(CFG)
llm = LLMClient(cfg)
fills = []
for asin in ("B04042A7EF", "B00AE1A633", "B09A31797F"):
    r = by_asin[asin]
    imgs, seen = [], set()
    for c in ("图片链接", "图片证据URL"):
        for u in str(ws.cell(row=r, column=hdr[c]).value or "").replace(" | ", ",").split(","):
            u = u.strip()
            if u.startswith("http") and u not in seen:
                seen.add(u)
                imgs.append(u)
    try:
        out = llm.vision(PROMPT, imgs, temperature=0.1, max_tokens=600,
                         detail="high", max_images=None)
        if isinstance(out, dict) and isinstance(out.get("response"), dict):
            out = out["response"]
    except Exception as exc:  # noqa: BLE001
        print("   行%-4d %-13s 调用失败: %s" % (r, asin, str(exc)[:70]))
        continue
    val = str((out or {}).get("分辨率") or "未明确").strip()
    ev = str((out or {}).get("证据原文") or "")[:80]
    print("   行%-4d %-13s 看图 → %-8s 图%s | %s" % (r, asin, val, (out or {}).get("图片序号"), ev))
    if val in ALLOW:
        ws.cell(row=r, column=RES).value = val
        set_sum(SUMCOL, r, SEG_RES, "摄像头分辨率=%s" % val)
        fills.append((r, asin, val, ev))
    else:
        note(CONC, r, "high 全图复核仍未给出可比分辨率，保持未明确（原文：%s）" % ev[:40])
print("\n④ 补齐分辨率 %d 行" % len(fills))

# ---------- 审计
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
want = {}
for r, a, f, o, nv, why in fixed:
    want.setdefault(a, []).append(("摄像头" if f == "摄像头" else "摄像头分辨率", nv, why))
for r, a, val, ev in fills:
    want.setdefault(a, []).append(("摄像头分辨率", val, "high 全图看图：%s" % ev[:50]))
na = 0
for r in range(2, aud.max_row + 1):
    fld = str(aud.cell(row=r, column=F).value or "").strip()
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    for f, nv, why in want.get(a, []):
        if fld in (f, f + "（原标称）", "摄像头分辨率"):
            aud.cell(row=r, column=V).value = nv
            st = str(aud.cell(row=r, column=S).value or "").strip()
            note2 = "%s：→ %s（%s）" % (MARK, nv, why)
            aud.cell(row=r, column=S).value = (st + "；" + note2) if st else note2
            na += 1
            break
print("审计同步 %d 条" % na)

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（摄像头字段收口）"
n.cell(row=rr, column=2).value = (
    "① 修正 3 处自相矛盾：行187（重力式喂食器）分辨率 4K→未明确（4K 系 OCR 把 `2．4K` 误读）；"
    "行134 摄像头 是→未明确（其 OCR 摘要原文明写 `this product does not have a camera`）；"
    "行362 摄像头 是→未明确（该 listing 是慢食碗，卖点说的是「适配」带摄像头的喂食器）。"
    "② 对行98/247/341（摄像头部=是但分辨率未明确）用 high + 全部图片复核，补齐 %d 行。"
    "③ 口径提醒：本列为**厂商原标称**，1080P/2K/3MP/4K 不是统一刻度 —— "
    "实测同款产品会同时挂多种标法（行254 的 3MP 自证 = 2048x1536；行258/259 的「2K」卖点又写 2MP），"
    "统计时不可把 2K 与 3MP 当两个档位排序。" % len(fills))
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
ci, ri = h.index("摄像头"), h.index("摄像头分辨率（原标称）")
print("\n===== 复核 =====")
print("分辨率分布:", dict(collections.Counter("" if r[ri] is None else str(r[ri]).strip()
                                              for r in rows).most_common()))
tab = collections.defaultdict(collections.Counter)
for r in rows:
    tab[str(r[ci]).strip()][str(r[ri]).strip()] += 1
print("摄像头 × 分辨率:", {k: dict(v) for k, v in tab.items()})
