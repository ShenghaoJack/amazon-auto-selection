# -*- coding: utf-8 -*-
"""对照实验：同一行、同一 prompt、同一组图，detail=low vs detail=high（各跑 2 次）。

比较三件事：① 提取出的图片文字（OCR 类）② 视觉理解（结构化字段）③ token/耗时/费用。
"""
from __future__ import annotations

import collections
import difflib
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
OUT = os.path.join("scripts", "_detail_experiment.json")

PROMPT = """这是同一款宠物喂食器的商品图片（多张，按顺序编号）。请做两件事：

A)【图片文字】逐张列出图上能看清的文字（英文/中文都要：规格、卖点、单位、数字、认证标记）。
   看不清的字符用 `?` 代替。没有文字的图写 ""。

B)【视觉理解】描述产品外观与结构，按字段填。

严格输出 JSON，不要解释：
{
 "图片文字": [{"序号": 1, "文字": "..."}, ...],
 "视觉理解": {"碗材质": "", "碗数量": "", "机身颜色": "", "显示屏或按键": "",
             "手机或App界面": "", "出现动物": "", "包装内容物": "", "其他可见结构": ""},
 "置信度": "高|中|低"
}
"""


def grab():
    wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x) for x in next(it)]
    data = [(i + 2, r) for i, r in enumerate(it)]
    wb.close()
    return hdr, data


hdr, data = grab()
C = {h: i for i, h in enumerate(hdr)}


def g(r, k):
    return "" if r[C[k]] is None else str(r[C[k]]).strip()


def urls(r):
    out = []
    for col in ("图片链接", "图片证据URL"):
        if col in C:
            for u in g(r, col).replace(" | ", ",").split(","):
                u = u.strip()
                if u.startswith("http") and u not in out:
                    out.append(u)
    return out


best = max(data, key=lambda x: len(urls(x[1])))
n, r = best
imgs = urls(r)
print("选用 行%d  %s  图片 %d 张" % (n, g(r, "变体 ASIN"), len(imgs)))
print("标题:", g(r, "商品标题")[:110])
print("现表值: 产品类型=%s 摄像头=%s 分辨率=%s 食碗材质=%s 联网=%s"
      % (g(r, "产品类型"), g(r, "摄像头"), g(r, "摄像头分辨率（原标称）"),
         g(r, "食碗材质"), g(r, "联网方式（GHz）")))
for u in imgs:
    print("   ", u)

cfg = Config.load(CFG)
llm = LLMClient(cfg)
runs = []
for detail in ("low", "high"):
    for rep in (1, 2):
        t0 = time.time()
        try:
            out = llm.vision(PROMPT, imgs, temperature=0.1, max_tokens=4000,
                             detail=detail, max_images=None)
            if isinstance(out, dict) and isinstance(out.get("response"), dict):
                out = out["response"]
            err = None
        except Exception as exc:  # noqa: BLE001
            out, err = None, str(exc)[:200]
        dt = time.time() - t0
        runs.append({"detail": detail, "rep": rep, "sec": round(dt, 1),
                     "out": out, "error": err})
        print("  detail=%-4s rep%d  %.1fs  %s" % (detail, rep, dt,
              "失败: %s" % err if err else "OK"))

json.dump({"row": n, "asin": g(r, "变体 ASIN"), "n_images": len(imgs),
           "images": imgs, "runs": runs}, open(OUT, "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\n结果已写 ->", OUT)

# ---------------- 比对 ----------------
def text_of(x):
    o = x["out"] or {}
    parts = o.get("图片文字") or []
    if isinstance(parts, dict):
        parts = [{"序号": k, "文字": v} for k, v in parts.items()]
    return "\n".join(str(p.get("文字") or "") for p in parts if isinstance(p, dict))


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


print("\n" + "=" * 100)
print("A. 图片文字（OCR 类）")
print("%-6s %-4s %6s %8s %8s %8s" % ("detail", "rep", "字符数", "数字个数", "含单位", "段数"))
for x in runs:
    t = text_of(x)
    o = x["out"] or {}
    segs = len(o.get("图片文字") or [])
    units = len(re.findall(r"\b(g|kg|ml|l|oz|cup|cups|mm|cm|inch|meals?|portions?)\b", t, re.I))
    print("%-6s %-4d %6d %8d %8d %8d"
          % (x["detail"], x["rep"], len(t), len(re.findall(r"\d+", t)), units, segs))

lo = [text_of(x) for x in runs if x["detail"] == "low"]
hi = [text_of(x) for x in runs if x["detail"] == "high"]
print("\n  low  两次之间相似度 = %.2f" % difflib.SequenceMatcher(None, lo[0], lo[1]).ratio())
print("  high 两次之间相似度 = %.2f" % difflib.SequenceMatcher(None, hi[0], hi[1]).ratio())
print("  low(第1次) vs high(第1次) 相似度 = %.2f"
      % difflib.SequenceMatcher(None, lo[0], hi[0]).ratio())

print("\n  —— high 独有、low 两次都没有的片段（差异最大的地方）——")
sm = difflib.SequenceMatcher(None, lo[0] + lo[1], hi[0] + hi[1])
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag in ("insert", "replace") and (j2 - j1) > 8:
        frag = (hi[0] + hi[1])[j1:j2].replace("\n", " ")
        print("    [+%3d字] %s" % (j2 - j1, frag[:150]))

print("\n" + "=" * 100)
print("B. 视觉理解（结构化字段）")
FIELDS = ["碗材质", "碗数量", "机身颜色", "显示屏或按键", "手机或App界面",
          "出现动物", "包装内容物", "其他可见结构"]
print("%-16s %-34s %-34s %s" % ("字段", "low(2次)", "high(2次)", "异同"))
for f in FIELDS:
    lv = [str(((x["out"] or {}).get("视觉理解") or {}).get(f) or "") for x in runs if x["detail"] == "low"]
    hv = [str(((x["out"] or {}).get("视觉理解") or {}).get(f) or "") for x in runs if x["detail"] == "high"]
    same_low = len(set(lv)) == 1
    same_high = len(set(hv)) == 1
    cross = set(lv) == set(hv)
    flag = "一致" if cross else "**不同**"
    print("%-16s %-34s %-34s %s" % (f, " | ".join(v[:32] for v in lv),
                                    " | ".join(v[:32] for v in hv), flag))
print("\nC. 置信度自评:", [(x["detail"], x["rep"], ((x["out"] or {}).get("置信度")))
                         for x in runs])
try:
    print("D. 用量:", json.dumps(llm.meter().totals(), ensure_ascii=False))
except Exception:  # noqa: BLE001
    pass
