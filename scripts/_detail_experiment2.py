# -*- coding: utf-8 -*-
"""① 逐图对照 low/high 的文字提取；② 量出单次调用的精确 token 差。"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
EXP = os.path.join("scripts", "_detail_experiment.json")

d = json.load(open(EXP, encoding="utf-8"))
runs = d["runs"]


def txt(run, idx):
    parts = (run["out"] or {}).get("图片文字") or []
    if isinstance(parts, dict):
        parts = [{"序号": k, "文字": v} for k, v in parts.items()]
    for p in parts:
        if isinstance(p, dict) and int(p.get("序号") or 0) == idx:
            return re.sub(r"\s+", " ", str(p.get("文字") or "")).strip()
    return ""


lo = next(x for x in runs if x["detail"] == "low" and x["rep"] == 1)
hi = next(x for x in runs if x["detail"] == "high" and x["rep"] == 1)
print("逐图文字长度对照（low vs high）：")
print("%-5s %7s %7s %7s  %s" % ("图序", "low字数", "high字数", "差", "high 多出来的内容（节选）"))
print("-" * 108)
n = d["n_images"]
for i in range(1, n + 1):
    a, b = txt(lo, i), txt(hi, i)
    extra = ""
    if len(b) > len(a):
        # 找出 b 里 a 没有的词
        wa = set(re.findall(r"[\w/.\-]{3,}", a.lower()))
        wb_ = [w for w in re.findall(r"[\w/.\-]{3,}", b) if w.lower() not in wa]
        extra = "、".join(dict.fromkeys(wb_))[:60]
    print("%-5d %7d %7d %+7d  %s" % (i, len(a), len(b), len(b) - len(a), extra))

print("\n" + "=" * 108)
print("差异最大的一张图（图 12）原文对照：")
for tag, t in (("low ", txt(lo, 12)), ("high", txt(hi, 12))):
    print("  [%s] %s" % (tag, t[:400] if t else "(空)"))

# ---------- 单次 token 差 ----------
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}
row = next(r for n, r in rows if str(r[C["变体 ASIN"]]).strip() == d["asin"])
imgs = d["images"]
PROMPT = ("这是同一款宠物喂食器的商品图片（多张，按顺序编号）。逐张列出图上能看清的文字，"
          "看不清用 ? 代替。严格输出 JSON：{\"图片文字\": [{\"序号\": 1, \"文字\": \"...\"}]}")
cfg = Config.load(CFG)
llm = LLMClient(cfg)
print("\n" + "=" * 108)
print("温度核对：temperature=0.1（两次调用同一值）")
try:
    from core.llm import TEMP_CYCLE_VISION
    print("  注意 LLMClient.vision 失败会按 TEMP_CYCLE_VISION=%s 换温度重试，"
          "下面同时报每次调用的**失败重试次数**（>0 说明该次不是 0.1）" % (TEMP_CYCLE_VISION,))
except Exception:  # noqa: BLE001
    print("  （未取到 TEMP_CYCLE_VISION）")
print("\n单次调用 token 对比（同一 prompt、同一 %d 张图、temperature=0.1）：" % len(imgs))
for detail in ("low", "high"):
    before = llm.meter().totals()
    t0 = time.time()
    llm.vision(PROMPT, imgs, temperature=0.1, max_tokens=4000, detail=detail, max_images=None)
    dt = time.time() - t0
    after = llm.meter().totals()
    dtok = after["total_tokens"] - before["total_tokens"]
    dcost = after["cost_cny"] - before["cost_cny"]
    dfail = after.get("failed_attempts", 0) - before.get("failed_attempts", 0)
    print("  detail=%-4s  %6.1fs  %7d tokens  %.5f 元  失败重试=%d %s"
          % (detail, dt, dtok, dcost, dfail, "（全程 temp=0.1 ✅）" if dfail == 0 else "⚠ 换过温度"))
