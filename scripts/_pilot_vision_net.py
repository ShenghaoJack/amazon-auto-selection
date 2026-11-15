# -*- coding: utf-8 -*-
"""试点：抽 10 行「联网方式=未明确」的非重力式产品，用视觉判断图中有无手机/App。
目的 = 量出单行成本与命中率，再决定是否全跑 136 行。
"""
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
GRAV = ("重力式喂食器", "重力式食水套装")
PILOT = ["B021B5D856", "B039C9B218", "B08D475884", "B09F88EB01", "B0E6099D5D",
         "B032D8A218", "B0A5E34961", "B042EFA0E8", "B0E582D628", "B0E7E972E3"]

PROMPT = """这是同一款宠物喂食器的商品图片（多张，按顺序编号）。请只根据图片作答。

问题：图片里是否出现下面任意一种「联网」线索？
  · 手机 / 平板 / App 界面截图（能看到手机外形、App 按钮、手机与喂食器同框）
  · 路由器 / WiFi 信号图标
  · 图片上写有 WiFi / Wi-Fi / APP / Smart / 2.4G / 5G / Dual Band 等字样

严格输出 JSON：
{"出现手机或App": true/false, "WiFi字样": true/false, "频段字样": "<2.4G | 5G | dual | 空>",
 "证据": "<你在第几张图看到了什么，20字内>", "图片序号": <整数或null>}
若都没有，全部填 false / 空。不要解释，不要输出多余文字。
"""


def grab():
    wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x) for x in next(it)]
    rows = [(i + 2, r) for i, r in enumerate(it)]
    wb.close()
    return hdr, rows


hdr, rows = grab()
C = {h: i for i, h in enumerate(hdr)}


def g(r, k):
    i = C.get(k)
    v = r[i] if i is not None else None
    return "" if v is None else str(v).strip()


def urls(r):
    out = []
    for col in ("图片链接", "图片证据URL"):
        if col not in C:
            continue
        for u in g(r, col).replace(" | ", ",").split(","):
            u = u.strip()
            if u.startswith("http") and u not in out:
                out.append(u)
    return out


cfg = Config.load(CFG)
llm = LLMClient(cfg)
n_done = 0
hits = 0
for n, r in rows:
    a = g(r, "变体 ASIN")
    if a not in PILOT:
        continue
    imgs = urls(r)
    try:
        out = llm.vision(PROMPT, imgs, temperature=0.1, max_tokens=400,
                         detail="low", max_images=8)
    except Exception as exc:  # noqa: BLE001
        print("行%-4d %-13s 调用失败: %s" % (n, a, str(exc)[:90]))
        continue
    if isinstance(out, dict) and isinstance(out.get("response"), dict):
        out = out["response"]
    n_done += 1
    phone = bool((out or {}).get("出现手机或App"))
    wifi = bool((out or {}).get("WiFi字样"))
    if phone or wifi:
        hits += 1
    print("行%-4d %-13s %-14s 手机/App=%-5s WiFi字样=%-5s 频段=%-4s 图%s | %s"
          % (n, a, g(r, "产品类型"), phone, wifi,
             str((out or {}).get("频段字样") or "-"), (out or {}).get("图片序号"),
             str((out or {}).get("证据") or "")[:44]))

print("\n试点完成 %d 行，命中 %d 行（%.0f%%）" % (n_done, hits, 100.0 * hits / max(1, n_done)))
try:
    t = llm.meter.totals()
    print("用量 totals:", json.dumps(t, ensure_ascii=False)[:400])
except Exception as exc:  # noqa: BLE001
    print("取用量失败:", exc)
    print("snapshot:", json.dumps(llm.meter.snapshot(), ensure_ascii=False)[:400])
