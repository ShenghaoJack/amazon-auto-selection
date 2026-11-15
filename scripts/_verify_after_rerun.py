# -*- coding: utf-8 -*-
"""重跑后的核验：新提示词到底有没有修好 schema？（只读）

用法: python scripts/_verify_after_rerun.py
对比对象: 17_属性标注/runs/*.json（新）  vs  17_属性标注.bak-before-promptfix/runs/*.json（旧）
"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.attr_stability.attr_stability import RuleSource, gate_run  # noqa: E402

CAT = "Salt and Pepper Mill Sets-US-202608"
CID = "salt_pepper_mill"
BASE = os.path.join("data", CAT)
NEW = os.path.join(BASE, "17_属性标注", "runs")
OLD = os.path.join(BASE, "17_属性标注.bak-before-promptfix", "runs")


def load(path):
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if isinstance(obj, dict):
        for k in ("rows", "data", "result"):
            if isinstance(obj.get(k), (dict, list)):
                obj = obj[k]
                break
    if isinstance(obj, list):
        return obj
    return [obj[k] for k in sorted(obj, key=lambda x: str(x))]


def profile(rows, label):
    types = collections.Counter()
    src_rows = 0
    src_vals = collections.Counter()
    keys = set()
    for r in rows:
        keys |= set(r)
        A = r.get("attributes") or {}
        if any(str(k).endswith("_来源") for k in A):
            src_rows += 1
        for k, v in A.items():
            types[type(v).__name__] += 1
            if str(k).endswith("_来源"):
                src_vals[str(v).strip()] += 1
    print("  %-10s %5d 行  取值类型=%-28s 含_来源的行=%d/%d"
          % (label, len(rows), dict(types), src_rows, len(rows)))
    print("             行级键=%s" % sorted(keys))
    print("             _来源值分布=%s" % dict(src_vals.most_common(6)))
    return src_rows, len(rows)


def main() -> None:
    rules = RuleSource.load(BASE, CID)
    print("=" * 88)
    print("新闸门口径: top_keys=%d 必需, allow_keys=%s 允许"
          % (len(rules.top_keys), sorted(rules.allow_keys)))

    news = {}
    for tag in ("a1", "a2"):
        p = os.path.join(NEW, "%s.json" % tag)
        if os.path.exists(p):
            news[tag] = load(p)
    if not news:
        print("\n!!! 新产物还没出现，重跑可能仍在进行或失败 !!!")
        return

    print("\n--- 新产物（项目根扁平 schema 提示词）---")
    for tag, rows in news.items():
        profile(rows, tag)
        g = gate_run(rows, rules, None, tag=tag)
        print("      闸门: rows=%d %-44s WARN %-5d → %s"
              % (g.rows, g.err_summary or "ERR 0", g.warnings, g.verdict))
        if g.issues:
            c = collections.Counter((i.level, i.kind, i.field) for i in g.issues)
            for (lv, kd, f), n in c.most_common(6):
                print("        [%s/%s] %-14s x%d" % (lv, kd, f, n))

    if os.path.isdir(OLD):
        print("\n--- 旧产物（已归档的 09-20 覆盖版提示词，作对照）---")
        for tag in ("a1",):
            p = os.path.join(OLD, "%s.json" % tag)
            if os.path.exists(p):
                profile(load(p), tag + "(旧)")

    print("\n--- 结论判据 ---")
    ok = all(not any(isinstance(v, dict)
                     for v in (r.get("attributes") or {}).values())
             for rows in news.values() for r in rows)
    src_ok = all(profile(rows, "")[0] == len(rows) for rows in news.values())
    print("  ① 取值是否不再出现多字段字典: %s" % ("是 ✅" if ok else "否 ❌ 仍是旧 schema"))
    print("  ② 是否每行都有 _来源 键    : %s"
          % ("是 ✅" if src_ok else "否 — 见上面的「含_来源的行」比例"))
    sch = sum(gate_run(r, rules, None, tag=t).schema_errors
              for t, r in news.items())
    print("  ③ schema 类 ERR 合计        : %d %s" % (sch, "✅" if sch == 0 else "❌"))


if __name__ == "__main__":
    main()
