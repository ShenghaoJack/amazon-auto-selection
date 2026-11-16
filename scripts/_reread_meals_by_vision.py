# -*- coding: utf-8 -*-
"""重新取图识别：5 行「每日最大餐数 = 0」的真实标称餐数。

背景：原值来自图片 OCR，OCR 把 `10 Meals Per Day` 读成了 `一 ] 0 Meals Per Day`
（同一行里还有 `50 Portions Per Meal`、`10g Per Portion`、`Max 10-Min Interval`）。
本脚本用视觉通道重读这 5 行的商品图，只认**图上明确写出**的餐数。
"""
from __future__ import annotations

import json
import os
import sys

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config            # noqa: E402
from core.llm import LLMClient            # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")

WANT = ["B015E6715D", "B0A91EC273", "B07DCBC264", "B0B82EEE1B", "B0A8320E61"]

PROMPT = """这是同一款自动猫/狗喂食器的商品图片（多张，按顺序编号）。
请只在图片**明确写出**文字时作答，找出规格表 / 卖点面板里关于「每日最大餐数」的标称。

英文常见写法：`X Meals Per Day`、`Up to X meals per day`、`X Meals Daily`。

⚠ 注意区分，不要把下面这些误当成餐数：
  · `Portions Per Meal` / `X Portions per meal` 是**每餐份数**，不是每天餐数
  · `Max 10-Min Interval` 是**出粮间隔**，不是餐数
  · `10g Per Portion` 是**单份克重**
  · 容量（L / Cups / Gallon）不是餐数

严格输出 JSON（不要解释）：
{"每日最大餐数": <整数 或 null>, "证据原文": "<图上读到的原文片段>", "图片序号": <第几张, 从1开始, 或 null>}
若多张图给出不同数字，取**明确写出的最大值**，并在证据原文里注明。
若没有任何一张图明确写出餐数，返回 {"每日最大餐数": null, "证据原文": "", "图片序号": null}
"""


def urls_of(row, C):
    out = []
    for col in ("图片链接", "图片证据URL"):
        if col not in C:
            continue
        v = "" if row[C[col]] is None else str(row[C[col]])
        for u in v.replace(" | ", ",").split(","):
            u = u.strip()
            if u.startswith("http") and u not in out:
                out.append(u)
    return out


wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}

cfg = Config.load(CFG)
llm = LLMClient(cfg)
results = []
for n, r in rows:
    a = str(r[C["变体 ASIN"]]).strip()
    if a not in WANT:
        continue
    imgs = urls_of(r, C)
    print("=" * 96)
    print("行%-4d %-13s 图片 %d 张 | %s" % (n, a, len(imgs), str(r[C["商品标题"]])[:80]))
    try:
        out = llm.vision(PROMPT, imgs, temperature=0.1, max_tokens=800,
                         detail="high", max_images=10)
    except Exception as exc:  # noqa: BLE001
        print("   视觉调用失败:", str(exc)[:160])
        results.append((n, a, None, "", "调用失败: %s" % str(exc)[:60]))
        continue
    if isinstance(out, dict) and isinstance(out.get("response"), dict):
        out = out["response"]
    meals = out.get("每日最大餐数") if isinstance(out, dict) else None
    ev = str((out or {}).get("证据原文") or "")[:160]
    idx = (out or {}).get("图片序号")
    print("   → 每日最大餐数 = %r" % meals)
    print("     图片序号 = %r" % idx)
    print("     证据原文 = %s" % ev)
    results.append((n, a, meals, ev, idx))

print("\n" + "=" * 96)
print("汇总:")
for n, a, m, ev, idx in results:
    print("   行%-4d %-13s → %-6s (图 %s) %s" % (n, a, m, idx, ev[:70]))
print("\nLLM 用量:", json.dumps(llm.meter.summary() if hasattr(llm.meter, "summary")
                               else {}, ensure_ascii=False)[:300])
