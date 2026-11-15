# -*- coding: utf-8 -*-
"""① 把「多值被当成单值」的三个属性改成 multi。

实测（本轮 17_属性标注/唯一值统计.json）：
  适用场景      112 个唯一值里 107 个是多场景（`厨房、户外`），分隔符 `、`
  外壳材质       31 个里 20 个是多材质（`不锈钢+玻璃`），分隔符 `+`
  磨芯材质       12 个里  6 个（`陶瓷+不锈钢`），分隔符 `+` / `、`

改法（新格式里 shape 由 `rule` 决定，见 attr_stability._MODERN_MULTI）：
  "rule": "enum"  →  "rule": "multi"
  并补 params.value_separators（拆词用）与 params.join_sep（重组用）。
这样代码里早已写好的 `_conv_multi`（按词拆→逐个查表→按规范序重组）才会被启用。
"""
from __future__ import annotations

import json
import os
import shutil

BASE = os.path.join("data", "Salt and Pepper Mill Sets-US-202608")
RULES = os.path.join(BASE, "salt_pepper_mill.rules.json")

PLAN = {
    "适用场景": {"value_separators": ["、", ",", ";", "；"], "join_sep": "、"},
    "外壳材质": {"value_separators": ["+", "、", ",", ";", "；"], "join_sep": "+"},
    "磨芯材质": {"value_separators": ["+", "、", ",", ";", "；"], "join_sep": "+"},
}

shutil.copy2(RULES, RULES + ".bak-before-multishape")
print("已备份 ->", os.path.basename(RULES) + ".bak-before-multishape")

d = json.load(open(RULES, encoding="utf-8"))
done = []
for a in d.get("attributes") or []:
    name = a.get("name")
    if name not in PLAN:
        continue
    old_rule = a.get("rule")
    a["rule"] = "multi"
    p = a.setdefault("params", {})
    p.setdefault("values", [])
    p["value_separators"] = PLAN[name]["value_separators"]
    p["join_sep"] = PLAN[name]["join_sep"]
    done.append((name, old_rule, a["rule"], p["value_separators"], p["join_sep"]))
json.dump(d, open(RULES, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

for name, o, n, seps, js in done:
    print("  %-8s rule %s → %s   拆词=%s  重组=%r" % (name, o, n, seps, js))

# ---- 复核：RuleSource / _converge_standardize 实际读到的 shape
from tools.attr_stability import RuleSource, attr_spec_from_modern   # noqa: E402
from tools.attr_stability.catrules import load_category              # noqa: E402

r = RuleSource.load(BASE, "salt_pepper_mill")
print("\nRuleSource 读到的 shape:")
for n in PLAN:
    s = r.attrs[n]
    print("   %-8s type=%-6s shape=%-6s vocab_policy=%s" % (n, s.type, s.shape, s.vocab_policy))

cf = load_category(BASE, "salt_pepper_mill")
print("_converge_standardize 会构造的 attrs_spec（模拟）:")
for at in cf.attributes:
    if at.get("name") not in PLAN:
        continue
    p = at.get("params") if isinstance(at.get("params"), dict) else {}
    spec = attr_spec_from_modern(at)
    built = {"type": spec.type, "value_shape": spec.shape,
             "value_separators": p.get("value_separators") or at.get("value_separators"),
             "join_sep": p.get("join_sep") or at.get("join_sep")}
    print("   %-8s %s" % (at["name"], built))
