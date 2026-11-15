# -*- coding: utf-8 -*-
"""只读：属性列的空值到底是「两遍意见不合→平票」还是「两遍都说没有证据」。

决定该不该"填"、怎么填。数据源: 17_属性标注/稳定性结果.json (33 MB)
"""
from __future__ import annotations

import collections
import json
import os

CAT = "Salt and Pepper Mill Sets-US-202608"
P = os.path.join("data", CAT, "17_属性标注", "稳定性结果.json")

d = json.load(open(P, encoding="utf-8"))
deliv = d.get("deliverable") or []
cells = d.get("cells") or {}
print("deliverable 行数=%d   cells 格数=%d" % (len(deliv), len(cells)))
print("gates:", {t: (g.get("rows"), g.get("data_errors"), g.get("verdict"))
                 for t, g in (d.get("gates") or {}).items()})
print("used_runs=%s  alone_rate=%s" % (d.get("used_runs"), d.get("alone_rate")))
print("needs_review=%d  vocab_candidates=%d"
      % (len(d.get("needs_review") or []), len(d.get("vocab_candidates") or [])))

# ---- cells 标志分布
flags = collections.Counter()
for k, c in cells.items():
    if not isinstance(c, dict):
        continue
    if str(k).endswith("_来源"):
        flags["来源格"] += 1
        continue
    if c.get("no_majority"):
        flags["值格·平票(no_majority)"] += 1
    elif c.get("needs_review"):
        flags["值格·待复核"] += 1
    else:
        flags["值格·有众数"] += 1
print("\ncell 标志分布:")
for k, v in flags.most_common():
    print("   %-24s %d" % (k, v))

# ---- 逐属性：deliverable 里空 / 未明确 / 有值
print("\n%-14s %6s %6s %6s | %-28s" % ("属性", "有值", "空", "未明确", "对应格的状态(空值的主要来源)"))
print("-" * 96)
attrs = sorted({k.partition("attr.")[2] for k in cells
                if "attr." in str(k) and not str(k).endswith("_来源")})
for a in attrs:
    has = blank = unk = 0
    why = collections.Counter()
    for r in deliv:
        v = str((r.get("attributes") or {}).get(a) or "").strip()
        key = "%s|attr.%s" % (r.get("xl_row"), a)
        c = cells.get(key)
        st = ("无此格" if not isinstance(c, dict)
              else "平票" if c.get("no_majority")
              else "待复核" if c.get("needs_review") else "有众数")
        if v == "":
            blank += 1
            why[st] += 1
        elif v in ("未明确", "不适用", "未知", "待确认"):
            unk += 1
        else:
            has += 1
    print("%-14s %6d %6d %6d | %s" % (a, has, blank, unk,
                                      dict(why.most_common())))

# ---- 空值格里的提名分布（看是不是"两遍各提一个不同的值"）
print("\n空值格里的提名情况抽样:")
shown = 0
for r in deliv[:400]:
    for a in attrs:
        v = str((r.get("attributes") or {}).get(a) or "").strip()
        if v:
            continue
        c = cells.get("%s|attr.%s" % (r.get("xl_row"), a))
        if isinstance(c, dict):
            vals = [g.get("value") for g in (c.get("values") or []) if isinstance(g, dict)]
            bys = [g.get("by") for g in (c.get("values") or []) if isinstance(g, dict)]
            print("   行%d %-10s n_votes=%s mode=%r no_majority=%s 提名=%s 来自=%s"
                  % (r.get("xl_row"), a, c.get("n_votes"), c.get("mode"),
                     c.get("no_majority"), vals, bys))
            shown += 1
            break
    if shown >= 12:
        break
