# -*- coding: utf-8 -*-
"""全量图片复核：137 行「联网方式=未明确 且 非重力式」逐行看图判断有无手机/App/WiFi 字样。
结果落 scripts/_net_sweep_result.json，由后续脚本回写 xlsx。≈0.45 元。
"""
from __future__ import annotations

import json
import os
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
OUT = os.path.join("scripts", "_net_sweep_result_all.json")
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
print("待复核 %d 行，开始…" % len(pool), flush=True)

cfg = Config.load(CFG)
llm = LLMClient(cfg)
res, t0 = [], time.time()
for k, (n, r) in enumerate(pool, 1):
    a = g(r, "变体 ASIN")
    rec = {"row": n, "asin": a, "type": g(r, "产品类型"),
           "title": g(r, "商品标题")[:80]}
    try:
        out = llm.vision(PROMPT, urls(r), temperature=0.1, max_tokens=400,
                         detail="low", max_images=None)
        if isinstance(out, dict) and isinstance(out.get("response"), dict):
            out = out["response"]
        rec.update({"phone": bool((out or {}).get("出现手机或App")),
                    "wifi": bool((out or {}).get("WiFi字样")),
                    "band": str((out or {}).get("频段字样") or ""),
                    "ev": str((out or {}).get("证据") or "")[:60],
                    "img": (out or {}).get("图片序号")})
    except Exception as exc:  # noqa: BLE001
        rec.update({"error": str(exc)[:120]})
    res.append(rec)
    if k % 10 == 0 or k == len(pool):
        hit = sum(1 for x in res if x.get("phone") or x.get("wifi"))
        print("  %3d/%d  命中 %d  用时 %.0fs" % (k, len(pool), hit, time.time() - t0),
              flush=True)

json.dump({"cat": os.path.basename(DESK), "n": len(res), "results": res},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
hit = [x for x in res if x.get("phone") or x.get("wifi")]
print("\n完成：%d 行，命中 %d 行" % (len(res), len(hit)))
for x in hit:
    print("   行%-4d %-13s %-14s 手机/App=%-5s WiFi=%-5s 频段=%-4s | %s"
          % (x["row"], x["asin"], x["type"], x["phone"], x["wifi"],
             x["band"] or "-", x["ev"]))
try:
    print("用量:", json.dumps(llm.meter().totals(), ensure_ascii=False))
except Exception as exc:  # noqa: BLE001
    print("取用量失败:", exc)
print("结果已写 ->", OUT)
