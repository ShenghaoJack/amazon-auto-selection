# -*- coding: utf-8 -*-
"""只读：新提示词下，受控值词表够不够用？（决定重跑前要不要先补词表）"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.attr_stability.attr_stability import RuleSource  # noqa: E402

CAT = "Salt and Pepper Mill Sets-US-202608"
rules = RuleSource.load(os.path.join("data", CAT), "salt_pepper_mill")
P = os.path.join("data", CAT, "17_属性标注", "runs", "a3.json")
rows = json.load(open(P, encoding="utf-8"))
if isinstance(rows, dict):
    rows = rows.get("rows") or list(rows.values())

print("%-12s %-6s %-7s %-5s %-6s %-6s %s"
      % ("属性", "shape", "policy", "词表", "取值数", "越界", "越界示例"))
print("-" * 104)
tot_bad = collections.Counter()
for name in sorted(rules.attrs):
    spec = rules.attrs[name]
    vals = []
    srcs = collections.Counter()
    for r in rows:
        v = (r.get("attributes") or {}).get(name)
        if isinstance(v, dict):
            vals.append(str(v.get("标准") or "").strip())
            srcs[str(v.get("来源") or "").strip()] += 1
        elif v is not None:
            vals.append(str(v).strip())
    uniq = {v for v in vals if v}
    bad = sorted(v for v in uniq if v not in set(spec.values)
                 and v not in {"未明确", "不适用", "未知", "待确认"})
    if bad:
        tot_bad[name] = len(bad)
    print("%-12s %-6s %-7s %-5d %-6d %-6d %s"
          % (name, spec.shape, spec.vocab_policy, len(spec.values or []),
             len(uniq), len(bad), "、".join(bad[:3])[:40]))
    if name in ("颜色", "供电方式", "产品类型"):
        print("      词表前 12: %s" % "、".join(list(spec.values or [])[:12]))
        print("      来源分布 : %s" % dict(srcs.most_common(5)))
        print("      空来源行数: %d / %d" % (srcs.get("", 0), len(rows)))

print("\n需要补词表的属性 (%d 个): %s"
      % (len(tot_bad), "、".join("%s(%d)" % (k, v) for k, v in tot_bad.most_common())))
