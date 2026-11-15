# -*- coding: utf-8 -*-
"""只读诊断：重放 Salt and Pepper 第 17 步的结构闸门，定位 ERR 1308 的来源。

不调用 LLM、不写任何产物。输入是已经跑完的 runs/a1..a4.json。
"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.attr_stability import (  # noqa: E402
    DEFAULT_TOP_KEYS, RuleSource, gate_run,
)

CAT = "Salt and Pepper Mill Sets-US-202608"
BASE = os.path.join("data", CAT)
RUNS = os.path.join(BASE, "17_属性标注", "runs")


def load_rows(path):
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if isinstance(obj, dict):
        for k in ("rows", "data", "result"):
            if isinstance(obj.get(k), (dict, list)):
                obj = obj[k]
                break
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return [obj[k] for k in sorted(obj, key=lambda x: str(x))]
    raise TypeError(type(obj))


def main() -> None:
    rules = RuleSource.load(BASE, "salt_pepper_mill")
    print("=" * 78)
    print("规则载入")
    print("  cat_id            =", getattr(rules, "cat", "?"))
    tk = set(rules.top_keys)
    print("  top_keys (闸门期望, %d 个) =" % len(tk), sorted(tk))
    print("  top_keys == DEFAULT_TOP_KEYS ?", tk == set(DEFAULT_TOP_KEYS))
    print("  attrs (%d 个) =" % len(rules.attrs), list(rules.attrs)[:6], "...")

    for tag in ("a1", "a2", "a3", "a4"):
        p = os.path.join(RUNS, "%s.json" % tag)
        if not os.path.exists(p):
            continue
        rows = load_rows(p)
        print("=" * 78)
        print("重放 %s.json：%d 行" % (tag, len(rows)))
        r0 = rows[0]
        print("  第 1 行顶层键 (%d) = %s" % (len(r0), sorted(r0)))
        miss = tk - set(r0)
        extra = set(r0) - tk
        print("  缺闸门期望键 (%d) = %s" % (len(miss), sorted(miss)))
        print("  多出未登记键 (%d) = %s" % (len(extra), sorted(extra)))
        # 统计全量行的键并集/交集
        union, inter = set(), None
        for r in rows:
            ks = set(r)
            union |= ks
            inter = ks if inter is None else (inter & ks)
        print("  全量键并集 (%d) = %s" % (len(union), sorted(union)))
        print("  全量键交集 (%d) = %s" % (len(inter or ()), sorted(inter or ())))
        nv = []
        g = gate_run(rows, rules, None, tag=tag, new_vocab=nv)
        print("  闸门: rows=%d errors=%d warnings=%d verdict=%s"
              % (g.rows, g.errors, g.warnings, g.verdict))
        c = collections.Counter(
            (i.level, i.field, i.why.split(":")[0][:70]) for i in g.issues)
        for (lv, f, why), n in c.most_common(10):
            print("    [%s] %-22s x%-5d %s" % (lv, f, n, why))
        rows_with = len({i.xl_row for i in g.err_issues if i.xl_row is not None})
        print("  出错行数(去重 xl_row) = %d / %d" % (rows_with, len(rows)))


if __name__ == "__main__":
    main()
