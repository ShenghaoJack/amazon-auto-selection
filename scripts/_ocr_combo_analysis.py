# -*- coding: utf-8 -*-
"""离线穷举：6 次 OCR 运行的所有子集 → 覆盖率 vs 费用，找 Pareto 前沿。
回答「temperature=0.1 下 high1+low1 是不是最全的选择」。"""
from __future__ import annotations

import itertools
import json
import re

D = json.load(open("scripts/_ocr_detail_test.json", encoding="utf-8"))
runs = D["runs"]
TOK = re.compile(r"[A-Za-z]+|\d+(?:[.:/]\d+)*|[\u4e00-\u9fff]")


def tk(s):
    return set(t.lower() for t in TOK.findall(s))


UNION = set().union(*[tk(x["text"]) for x in runs])
print("参考集（6 次并集）= %d 词元；单次费用/覆盖率：" % len(UNION))
for i, x in enumerate(runs):
    print("   #%d %-4s rep%d  词元%2d  单次覆盖 %5.1f%%  费用 %.5f 元"
          % (i + 1, x["detail"], x["rep"], len(tk(x["text"])),
             100.0 * len(tk(x["text"]) & UNION) / len(UNION), x["cost"]))

combos = []
for k in range(1, len(runs) + 1):
    for sub in itertools.combinations(range(len(runs)), k):
        t = set().union(*[tk(runs[i]["text"]) for i in sub])
        combos.append((100.0 * len(t & UNION) / len(UNION),
                       sum(runs[i]["cost"] for i in sub), sub, len(t)))

print("\n=== 每次数下的最优组合 ===")
print("%-6s %-28s %8s %9s" % ("次数", "组合", "覆盖率", "费用(元)"))
for k in range(1, len(runs) + 1):
    best = max((c for c in combos if len(c[2]) == k), key=lambda c: (c[0], -c[1]))
    names = "+".join("%s%d" % (runs[i]["detail"], runs[i]["rep"]) for i in best[2])
    print("%-6d %-28s %7.1f%% %9.5f" % (k, names, best[0], best[1]))

print("\n=== Pareto 前沿（不花钱买不到更高覆盖的组合）===")
front = []
for c in sorted(combos, key=lambda c: (c[1], -c[0])):
    if not front or c[0] > max(f[0] for f in front):
        front.append(c)
print("%-30s %8s %9s" % ("组合", "覆盖率", "费用(元)"))
for cov, cost, sub, n in front:
    names = "+".join("%s%d" % (runs[i]["detail"], runs[i]["rep"]) for i in sub)
    print("%-30s %7.1f%% %9.5f" % (names, cov, cost))

print("\n=== 2 次调用的所有组合（按覆盖率排序）===")
pairs = sorted((c for c in combos if len(c[2]) == 2), key=lambda c: -c[0])
for cov, cost, sub, n in pairs[:6]:
    names = "+".join("%s%d" % (runs[i]["detail"], runs[i]["rep"]) for i in sub)
    print("   %-30s %7.1f%%  %.5f 元" % (names, cov, cost))

print("\n=== 3 次调用里最省的 ===")
for cov, cost, sub, n in sorted((c for c in combos if len(c[2]) == 3),
                                key=lambda c: -c[0])[:5]:
    names = "+".join("%s%d" % (runs[i]["detail"], runs[i]["rep"]) for i in sub)
    print("   %-30s %7.1f%%  %.5f 元" % (names, cov, cost))

# 只由 low 组成 vs 含 high 的对比
print("\n=== 同档 vs 跨档 ===")
lo = [i for i, x in enumerate(runs) if x["detail"] == "low"]
hi = [i for i, x in enumerate(runs) if x["detail"] == "high"]
for label, sub in (("low×1", (lo[0],)), ("low×2", tuple(lo[:2])), ("low×3", tuple(lo)),
                   ("high×1", (hi[0],)), ("high×2", tuple(hi[:2])), ("high×3", tuple(hi)),
                   ("high1+low1", (hi[0], lo[0])), ("high1+low2", (hi[0], lo[0], lo[1]))):
    t = set().union(*[tk(runs[i]["text"]) for i in sub])
    print("   %-12s 覆盖 %5.1f%%  费用 %.5f 元"
          % (label, 100.0 * len(t & UNION) / len(UNION), sum(runs[i]["cost"] for i in sub)))
