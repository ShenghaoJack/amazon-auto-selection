# -*- coding: utf-8 -*-
"""只读：看清 Salt and Pepper 一份运行里 attributes 的真实结构。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.attr_stability.attr_stability import RuleSource  # noqa: E402

P = os.path.join("data", "Salt and Pepper Mill Sets-US-202608",
                 "17_属性标注", "runs", "a3.json")
rules = RuleSource.load("data/Salt and Pepper Mill Sets-US-202608",
                        "salt_pepper_mill")

with open(P, encoding="utf-8") as fh:
    rows = json.load(fh)
if isinstance(rows, dict):
    rows = rows.get("rows") or list(rows.values())

r = rows[0]
print("行级键:", sorted(r))
print("xl_row:", r.get("xl_row"))
A = r["attributes"]
print("\nattributes 键 (%d): %s" % (len(A), sorted(A)))
print("  含 _来源 后缀的键:", [k for k in A if str(k).endswith("_来源")])
print("  attrs 声明 (%d): %s" % (len(rules.attrs), list(rules.attrs)))

print("\n--- 逐属性取值（前 3 个）---")
for k in list(A)[:3]:
    v = A[k]
    print("  %-8s type=%-6s repr=%.400s" % (k, type(v).__name__, repr(v)))

print("\n--- 形态 / 尺寸_拆列 / category_check ---")
for k in ("形态", "尺寸_拆列", "category_check", "pack_count_数值",
          "suitable_scene_规范串"):
    print("  %-20s = %.200s" % (k, repr(r.get(k))))

# 全量统计：多少行的 attributes 缺 _来源 键
n = len(rows)
no_src = sum(1 for x in rows
             if not any(str(k).endswith("_来源") for k in (x.get("attributes") or {})))
kinds = {}
for x in rows:
    for k, v in (x.get("attributes") or {}).items():
        kinds[type(v).__name__] = kinds.get(type(v).__name__, 0) + 1
print("\n全量 %d 行：attributes 里一个 _来源 键都没有的行数 = %d" % (n, no_src))
print("attributes 取值的类型分布:", kinds)
