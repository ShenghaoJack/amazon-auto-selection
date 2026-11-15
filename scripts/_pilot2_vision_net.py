# -*- coding: utf-8 -*-
"""试点 2：从 136 行「未明确 且 非重力式」里随机抽 20 行，量增量命中率与成本。"""
from __future__ import annotations

import json
import os
import random
import sys

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
GRAV = ("重力式喂食器", "重力式食水套装")

PROMPT = """这是同一款宠物喂食器的商品图片（多张，按顺序编号）。请只根据图片作答。

问题：图片里是否出现下面任意一种「联网」线索？
  · 手机 / 平板 / App 界面截图（手机外形、App 按钮、手机与喂食器同框）
  · 路由器 / WiFi 信号图标
  · 图上写有 WiFi / Wi-Fi / APP / Smart / 2.4G / 5G / Dual Band 字样

严格输出 JSON：
{"出现手机或App": true/false, "WiFi字样": true/false, "频段字样": "<2.4G | 5G | dual | 空>",
 "证据": "<第几张图看到了什么, 20字内>", "图片序号": <整数或null>}
都没有就全部 false / 空。不要解释。
"""

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}


def g(r, k):
    i = C.get(k)
    v = r[i] if i is not None else None
    return "" if v is None else str(v).strip()


def urls(r):
    out = []
    for col in ("图片链接", "图片证据URL"):
        if col in C:
            for u in g(r, col).replace(" | ", ",").split(","):
                u = u.strip()
                if u.startswith("http") and u not in out:
                    out.append(u)
    return out


pool = [(n, r) for n, r in rows
        if g(r, "联网方式（GHz）") in ("", "未明确") and g(r, "产品类型") not in GRAV]
random.seed(20260930)
sample = random.sample(pool, 20)
print("未明确且非重力式共 %d 行；随机抽 %d 行\n" % (len(pool), len(sample)))

cfg = Config.load(CFG)
llm = LLMClient(cfg)
hits = []
for n, r in sample:
    a = g(r, "变体 ASIN")
    try:
        out = llm.vision(PROMPT, urls(r), temperature=0.1, max_tokens=400,
                         detail="low", max_images=8)
    except Exception as exc:  # noqa: BLE001
        print("行%-4d %-13s 失败: %s" % (n, a, str(exc)[:70]))
        continue
    if isinstance(out, dict) and isinstance(out.get("response"), dict):
        out = out["response"]
    ph = bool((out or {}).get("出现手机或App"))
    wf = bool((out or {}).get("WiFi字样"))
    if ph or wf:
        hits.append((n, a))
    print("行%-4d %-13s %-14s 手机/App=%-5s WiFi字样=%-5s 频段=%-4s | %s"
          % (n, a, g(r, "产品类型"), ph, wf, str((out or {}).get("频段字样") or "-"),
             str((out or {}).get("证据") or "")[:40]))

print("\n样本 20 行，命中 %d 行（%.0f%%）" % (len(hits), 100.0 * len(hits) / 20))
try:
    t = llm.meter().totals()
    print("本轮用量:", json.dumps(t, ensure_ascii=False)[:500])
except Exception as exc:  # noqa: BLE001
    print("取用量失败:", exc)
