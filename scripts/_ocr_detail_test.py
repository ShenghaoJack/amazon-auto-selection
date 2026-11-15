# -*- coding: utf-8 -*-
"""纯 OCR 专项：同一张文字密集图，detail=low vs high 各跑 3 次。
以 6 次输出的并集为伪参考集，衡量"文本完整性"（召回）与稳定性。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
IMG = "https://m.media-amazon.com/images/I/61Npv8ylMWL._AC_SL1500_.jpg"   # 行20 第6张（App 喂食计划页）
N = 3
OUT = os.path.join("scripts", "_ocr_detail_test.json")

PROMPT = """请对这张图片做**纯 OCR**：逐字提取图上出现的全部可见文字。

要求：
- 不要描述图片、不要翻译、不要总结、不要补全、不要推测；
- 按阅读顺序输出，同一行的文字连在一起；
- 单个看不清的字符用 `?` 代替，整段看不清就不写；
- 数字、单位、标点、品牌名、界面文字、状态栏文字、水印都要。

严格输出 JSON：{"文字": "<全部文字，用 \\n 分行>"}"""

cfg = Config.load(CFG)
llm = LLMClient(cfg)
runs = []
for detail in ("low", "high"):
    for rep in range(1, N + 1):
        b = llm.meter().totals()
        t0 = time.time()
        try:
            out = llm.vision(PROMPT, [IMG], temperature=0.1, max_tokens=2000,
                             detail=detail, max_images=None)
            if isinstance(out, dict) and isinstance(out.get("response"), dict):
                out = out["response"]
            t = re.sub(r"[ \t]+", " ", str((out or {}).get("文字") or "")).strip()
            err = None
        except Exception as exc:  # noqa: BLE001
            t, err = "", str(exc)[:150]
        dt = time.time() - t0
        a = llm.meter().totals()
        runs.append({"detail": detail, "rep": rep, "sec": round(dt, 1), "text": t,
                     "error": err,
                     "tokens": a["total_tokens"] - b["total_tokens"],
                     "cost": round(a["cost_cny"] - b["cost_cny"], 5),
                     "retry": a.get("failed_attempts", 0) - b.get("failed_attempts", 0)})
        print("  %-4s rep%d  %5.1fs  %6d tok  %s"
              % (detail, rep, dt, runs[-1]["tokens"], "失败" if err else "OK"), flush=True)

json.dump({"img": IMG, "runs": runs}, open(OUT, "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

# ---------------- 分析 ----------------
TOK = re.compile(r"[A-Za-z]+|\d+(?:[.:/]\d+)*|[\u4e00-\u9fff]")


def toks(s):
    return set(t.lower() for t in TOK.findall(s))


allt = toks(" ".join(x["text"] for x in runs))
print("\n伪参考集（6 次并集）唯一词元 = %d" % len(allt))
print("\n%-6s %-4s %7s %7s %8s %8s %7s" %
      ("detail", "rep", "字符数", "词元数", "召回率", "独有词元", "重试"))
print("-" * 62)
summ = {}
for x in runs:
    tk = toks(x["text"])
    only = {t for t in tk if sum(1 for y in runs if t in toks(y["text"])) == 1}
    rec = 100.0 * len(tk & allt) / max(1, len(allt))
    print("%-6s %-4d %7d %7d %7.1f%% %8d %7d"
          % (x["detail"], x["rep"], len(x["text"]), len(tk), rec, len(only), x["retry"]))
    summ.setdefault(x["detail"], []).append((len(x["text"]), len(tk), rec))

print("\n按档位汇总：")
for d, v in summ.items():
    print("  %-5s 字符数 均值 %.0f  词元 均值 %.0f  召回 均值 %.1f%%"
          % (d, sum(a for a, _, _ in v) / len(v), sum(b for _, b, _ in v) / len(v),
             sum(c for _, _, c in v) / len(v)))
print("\n费用：low 合计 %.5f 元 / high 合计 %.5f 元"
      % (sum(x["cost"] for x in runs if x["detail"] == "low"),
         sum(x["cost"] for x in runs if x["detail"] == "high")))

# 只在 high 出现、low 三次都没有的词元
lo = set().union(*[toks(x["text"]) for x in runs if x["detail"] == "low"])
hi = set().union(*[toks(x["text"]) for x in runs if x["detail"] == "high"])
print("\n只在 high 出现（low 3 次全无）的词元 = %d 个:" % len(hi - lo))
print("   " + " ".join(sorted(hi - lo))[:600])
print("\n只在 low 出现（high 3 次全无）的词元 = %d 个:" % len(lo - hi))
print("   " + " ".join(sorted(lo - hi))[:400])
