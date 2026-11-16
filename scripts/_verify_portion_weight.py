# -*- coding: utf-8 -*-
"""核对 5 行大克重（120/330）：看图区分「单份克重」与「总容量」。"""
from __future__ import annotations

import json
import os
import sys

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
COL = "单份克重（g/份）"
WANT = ["B047043728", "B0039CCED8", "B063EB0692", "B0911E1472", "B0E1C94242"]

PROMPT = """这是同一款宠物喂食器（多为**湿粮/冷藏**喂食器）的商品图片（多张，按顺序编号）。
请找出**单份克重**——即「一格/一餐装多少克食物」（per portion / per meal weight，英文常见
`100-120g per meal`、`330g*5 meals`、`240mL(100-120g)`）。

⚠ 必须区分这两个概念，不要把总容量当成单份：
  · 单份克重 = 每一餐/每一格装多少克
  · 总容量   = 整机总装量（如 960ml、1650g、4 格合计 240mL）

严格输出 JSON：
{"单份克重": <数字 或 null>, "该数字的含义": "单份" | "总容量" | "不确定",
 "证据原文": "<图上原文片段>", "图片序号": <整数或null>, "总容量若可见": "<如 960ml / 1650g，否则空>"}
只有能明确判断是**单份**时才给数字；拿不准就 "单份克重": null。不要解释。"""


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


cfg = Config.load(CFG)
llm = LLMClient(cfg)
res = []
for n, r in data:
    a = g(r, "变体 ASIN")
    if a not in WANT:
        continue
    cur = g(r, COL)
    out = llm.vision(PROMPT, urls(r), temperature=0.1, max_tokens=600,
                     detail="high", max_images=None)
    if isinstance(out, dict) and isinstance(out.get("response"), dict):
        out = out["response"]
    out = out or {}
    print("=" * 100)
    print("行%-4d %-13s 现值=%-5s 容量列=%s | %s" % (n, a, cur, g(r, "容量（L）"), g(r, "商品标题")[:58]))
    print("   看图 → 单份克重=%r 含义=%s 图%s | 总容量=%s"
          % (out.get("单份克重"), out.get("该数字的含义"), out.get("图片序号"),
             out.get("总容量若可见") or "-"))
    print("   证据: %s" % str(out.get("证据原文") or "")[:130])
    res.append({"row": n, "asin": a, "cur": cur, "vision": out})
json.dump(res, open(os.path.join("scripts", "_portion_weight_check.json"), "w",
                    encoding="utf-8"), ensure_ascii=False, indent=2)
try:
    print("\n用量:", json.dumps(llm.meter().totals(), ensure_ascii=False))
except Exception:  # noqa: BLE001
    pass
