# -*- coding: utf-8 -*-
"""只读：把 5188 个平票格分类，决定每种该怎么填。"""
from __future__ import annotations

import collections
import json
import os

CAT = "Salt and Pepper Mill Sets-US-202608"
P = os.path.join("data", CAT, "17_属性标注", "稳定性结果.json")
RESERVED = {"未明确", "不适用", "未知", "待确认", ""}

d = json.load(open(P, encoding="utf-8"))
cells = d.get("cells") or {}

kinds = collections.Counter()
per_attr = collections.defaultdict(collections.Counter)
examples = collections.defaultdict(list)

for k, c in cells.items():
    if not isinstance(c, dict) or not c.get("no_majority"):
        continue
    if str(k).endswith("_来源"):
        continue
    attr = str(k).partition("attr.")[2]
    vals = [str(g.get("value") or "").strip() for g in (c.get("values") or [])
            if isinstance(g, dict)]
    by = [tuple(g.get("by") or []) for g in (c.get("values") or [])
          if isinstance(g, dict)]
    concrete = [v for v in vals if v not in RESERVED]
    unk = [v for v in vals if v in RESERVED]
    if len(concrete) == 1 and unk:
        kind = "① 具体值 vs 未明确 → 具体值可确定胜出"
    elif len(concrete) == 2:
        a, b = concrete
        if a in b or b in a:
            kind = "② 一方包含另一方 → 可取更具体/更全的一方"
        else:
            kind = "③ 两个不同具体值 → 必须第3遍或定规则"
    elif not concrete:
        kind = "④ 双方都是未明确类"
    else:
        kind = "⑤ 其它(%d 具体)" % len(concrete)
    kinds[kind] += 1
    per_attr[attr][kind] += 1
    if len(examples[kind]) < 4:
        examples[kind].append((k, vals, by))

total = sum(kinds.values())
print("平票格合计 %d\n" % total)
for k, v in kinds.most_common():
    print("  %-44s %6d  (%.1f%%)" % (k, v, 100.0 * v / total))
    for kk, vals, by in examples[k][:2]:
        print("        e.g. %-28s 提名=%s 来自=%s" % (kk, vals, [list(b) for b in by]))

# ---- 区分「交付属性列」与「过程字段(尺寸_依据/尺寸_拆列/…)」
print("\n=== 只在 24 个交付属性列(attr.*) 里统计 ===")
ak = collections.Counter()
for k, c in cells.items():
    if not isinstance(c, dict) or not c.get("no_majority"):
        continue
    if str(k).endswith("_来源") or "attr." not in str(k):
        continue
    vals = [str(g.get("value") or "").strip() for g in (c.get("values") or [])
            if isinstance(g, dict)]
    concrete = [v for v in vals if v not in RESERVED]
    unk = [v for v in vals if v in RESERVED]
    if len(concrete) == 1 and unk:
        ak["① 具体 vs 未明确"] += 1
    elif len(concrete) == 2:
        a, b = concrete
        ak["② 一方包含另一方" if (a in b or b in a) else "③ 两个不同具体值"] += 1
    elif not concrete:
        ak["④ 双方都未明确类"] += 1
    else:
        ak["⑤ 其它"] += 1
at = sum(ak.values())
for k, v in ak.most_common():
    print("  %-24s %6d  (%.1f%%)" % (k, v, 100.0 * v / at))
print("  交付属性列平票合计 = %d" % at)
print("\n=== 非属性列(过程字段)平票 ===")
print("  合计 = %d" % (total - at))

print("\n%-14s %6s %6s %6s %6s" % ("属性", "①", "②", "③", "④⑤"))
print("-" * 44)
for a, c in sorted(per_attr.items(), key=lambda x: -sum(x[1].values())):
    n1 = sum(v for k, v in c.items() if k.startswith("①"))
    n2 = sum(v for k, v in c.items() if k.startswith("②"))
    n3 = sum(v for k, v in c.items() if k.startswith("③"))
    n45 = sum(v for k, v in c.items() if k.startswith(("④", "⑤")))
    print("%-14s %6d %6d %6d %6d" % (a, n1, n2, n3, n45))
