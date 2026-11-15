# -*- coding: utf-8 -*-
"""食碗材质：碗感知的文本判定 + 无证据行看图。结果落 JSON，由后续脚本写回 xlsx。

词表：不锈钢 / 塑料（与文件既有取值一致；`不锈钢（材质字段）` 只是写法差异）。
判定优先级：
  ① 碗附近出现不锈钢/304        → 不锈钢
  ② 碗附近出现塑料/PP/ABS       → 塑料
  ③ 无碗信息但详细参数 Material  → 按 Material（原文件 `（材质字段）` 就是这个口径）
  ④ 全无证据                    → 看图（视觉通道判断碗的材质）
"""
from __future__ import annotations

import json
import os
import re
import sys

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
OUT = os.path.join("scripts", "_bowl_material_plan_all.json")

STEEL = r"stainless\s*steel|不锈钢|304|18/8"
PLAST = r"plastic|塑料|abs\b|pp\b|polypropylene|acrylonitrile|树脂"
BOWL = r"bowl|tray|dish|basin|碗|盆|碟"
NEAR = 34

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
a = wb["属性证据审计"]
it2 = a.iter_rows(values_only=True)
h2 = [str(x) for x in next(it2)]
C2 = {h: i for i, h in enumerate(h2)}
aud = {}
for r in it2:
    if "食碗材质" in str(r[C2["属性字段"]]):
        aud.setdefault(str(r[C2["变体ASIN"]]).strip(), []).append(
            (str(r[C2["提取值"]]).strip(), str(r[C2["证据原文"]]),
             str(r[C2["证据来源"]])))
wb.close()
C = {h: i for i, h in enumerate(hdr)}
COL = [h for h in hdr if "食碗材质" in h][0]
TXT = ["商品标题", "详细参数", "产品卖点", "图片OCR摘要", "图片核心卖点",
       "图片理解结论", "属性证据摘要"]


def near(pat_a, pat_b, s):
    for m in re.finditer(pat_a, s, re.I):
        lo, hi = max(0, m.start() - NEAR), min(len(s), m.end() + NEAR)
        if re.search(pat_b, s[lo:hi], re.I):
            return s[max(0, m.start() - 20):m.end() + 20].replace("\n", " ")
    return None


def urls(r):
    out = []
    for col in ("图片链接", "图片证据URL"):
        if col in C:
            for u in ("" if r[C[col]] is None else str(r[C[col]])).replace(" | ", ",").split(","):
                u = u.strip()
                if u.startswith("http") and u not in out:
                    out.append(u)
    return out


plan, need_vision = [], []
for n, r in rows:
    v = "" if r[C[COL]] is None else str(r[C[COL]]).strip()
    if v != "未明确":
        continue
    asin = str(r[C["变体 ASIN"]]).strip()
    evs = aud.get(asin, [])
    blob = " || ".join("" if r[C[c]] is None else str(r[C[c]]) for c in TXT if c in C)
    evtxt = " || ".join(t for _, t, _ in evs)
    both = blob + " || " + evtxt
    val = why = None
    h1 = near(STEEL, BOWL, both) or near(BOWL, STEEL, both)
    h2_ = near(PLAST, BOWL, both) or near(BOWL, PLAST, both)
    if h1:
        val, why = "不锈钢", "碗附近出现不锈钢：%s" % h1[:70]
    elif h2_:
        val, why = "塑料", "碗附近出现塑料：%s" % h2_[:70]
    else:
        m = re.search(r"Material\s*[:：]\s*([^|;]{2,40})", both, re.I)
        if m:
            seg = m.group(1)
            if re.search(STEEL, seg, re.I):
                val, why = "不锈钢", "材质字段=%s" % seg[:40]
            elif re.search(PLAST, seg, re.I):
                val, why = "塑料", "材质字段=%s" % seg[:40]
    if val:
        plan.append({"row": n, "asin": asin, "value": val, "why": why})
    else:
        need_vision.append({"row": n, "asin": asin, "title": str(r[C["商品标题"]])[:70],
                            "urls": urls(r), "ev": evtxt[:120]})

print("文本可判 = %d 行；需看图 = %d 行" % (len(plan), len(need_vision)))
import collections  # noqa: E402
print("文本判定分布:", dict(collections.Counter(p["value"] for p in plan)))
for p in plan[:10]:
    print("   行%-4d %-13s → %-5s %s" % (p["row"], p["asin"], p["value"], p["why"][:74]))

# ---- 看图
if need_vision:
    PROMPT = """这是同一款宠物喂食器/饮水器的商品图片（多张，按顺序编号）。
请判断**食碗／水碗／托盘**（宠物接触食物的那个碗或托盘）的材质。

常见写法：Stainless Steel Bowl / 不锈钢碗 / Plastic Bowl / PP / BPA-Free Plastic / Ceramic / Silicone。

严格输出 JSON：
{"碗材质": "不锈钢" | "塑料" | "陶瓷" | "硅胶" | "未明确",
 "证据": "<第几张图看到什么, 25字内>", "图片序号": <整数或null>}
若图片里看不出碗的材质，或根本没有碗，填 "未明确"。不要解释。
"""
    cfg = Config.load(CFG)
    llm = LLMClient(cfg)
    for i, item in enumerate(need_vision, 1):
        try:
            out = llm.vision(PROMPT, item["urls"], temperature=0.1, max_tokens=400,
                             detail="low", max_images=None)
            if isinstance(out, dict) and isinstance(out.get("response"), dict):
                out = out["response"]
            item["vision"] = str((out or {}).get("碗材质") or "未明确")
            item["vision_ev"] = "%s（图%s）" % (str((out or {}).get("证据") or "")[:50],
                                              (out or {}).get("图片序号"))
        except Exception as exc:  # noqa: BLE001
            item["vision"] = "未明确"
            item["vision_ev"] = "调用失败: %s" % str(exc)[:60]
        print("  [%2d/%d] 行%-4d %-13s → %s | %s" % (i, len(need_vision), item["row"],
              item["asin"], item["vision"], item["vision_ev"][:44]), flush=True)
    try:
        print("看图用量:", json.dumps(llm.meter().totals(), ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass

json.dump({"plan": plan, "vision": need_vision}, open(OUT, "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\n结果已写 ->", OUT)
if need_vision:
    print("看图判定分布:", dict(collections.Counter(
        v["vision"] for v in need_vision)))
