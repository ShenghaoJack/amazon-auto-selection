# -*- coding: utf-8 -*-
"""只读回归：闸门分级 + top_keys 入配置 之后，重放各类目已有产物。

不调用 LLM、不写任何产物。
  1) Salt and Pepper：17_属性标注/runs/a1..a4.json（1308 行/份）
  2) Cutting Boards ：必须仍然 PASS（它的产出不含 `形态`，用配置里的 10 键）
"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.attr_stability import RuleSource, gate_run  # noqa: E402

CASES = [
    ("Salt and Pepper Mill Sets-US-202608", "salt_pepper_mill",
     os.path.join("data", "Salt and Pepper Mill Sets-US-202608",
                  "17_属性标注", "runs"), ("a1", "a2", "a3", "a4")),
]


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
    for cat, cid, runs_dir, tags in CASES:
        rules = RuleSource.load(os.path.join("data", cat), cid)
        print("=" * 84)
        print("%s   top_keys(%d) = %s" % (cid, len(rules.top_keys),
                                          sorted(rules.top_keys)))
        for tag in tags:
            p = os.path.join(runs_dir, "%s.json" % tag)
            if not os.path.exists(p):
                print("  %s  (缺文件)" % tag)
                continue
            rows = load_rows(p)
            g = gate_run(rows, rules, None, tag=tag)
            print("  %-4s rows=%-5d %-44s WARN %-5d → %s"
                  % (tag, g.rows, g.err_summary or "ERR 0", g.warnings,
                     g.verdict))
            if g.issues:
                c = collections.Counter(
                    (i.level, i.kind, i.field, i.why.split("（")[0][:64])
                    for i in g.issues)
                for (lv, kd, f, why), n in c.most_common(6):
                    print("       [%s/%s] %-14s x%-6d %s" % (lv, kd, f, n, why))

    # Cutting Boards：只读校验口径本身，不重跑
    print("=" * 84)
    for cat, cid in (("Cutting Boards-US-202608", "cutting_boards"),):
        rules = RuleSource.load(os.path.join("data", cat), cid)
        print("%s  top_keys(%d) = %s"
              % (cid, len(rules.top_keys), sorted(rules.top_keys)))
        print("  attrs = %d 个" % len(rules.attrs))
        hist = os.path.join("data", cat, "17_属性标注", "稳定性结果.json")
        if os.path.exists(hist):
            with open(hist, encoding="utf-8") as fh:
                h = json.load(fh)
            for t, g in (h.get("gates") or {}).items():
                print("  历史(修复前) %-4s rows=%-5s errors=%-4s warnings=%-3s %s"
                      % (t, g.get("rows"), g.get("errors"),
                         g.get("warnings"), g.get("verdict")))


if __name__ == "__main__":
    main()
