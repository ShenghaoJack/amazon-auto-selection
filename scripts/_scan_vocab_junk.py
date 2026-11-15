# -*- coding: utf-8 -*-
"""只读：扫描所有类目 rules.json 的受控值词表里有没有「非值」垃圾
（缺键哨兵 / None / nan / 空串 / 重复）。2026-09-29 起因: `<缺键>` 泄漏事件。
"""
from __future__ import annotations

import glob
import json
import os

JUNK = {"<缺键>", "None", "none", "NONE", "nan", "NaN", "null", "NULL"}

files = sorted(glob.glob(os.path.join("data", "*", "*.rules.json")))
print("扫描 %d 个 rules.json\n" % len(files))
hits = 0
for f in files:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print("  解析失败 %s: %s" % (f, e))
        continue
    cat = os.path.basename(f)
    for a in (d.get("attributes") or []):
        if not isinstance(a, dict):
            continue
        vals = a.get("values") or (a.get("params") or {}).get("values") or []
        bad = [v for v in vals if str(v).strip() in JUNK or str(v).strip() == ""]
        dup = [v for v, n in
               {str(x): [str(y) for y in vals].count(str(x)) for x in vals}.items()
               if n > 1]
        if bad or dup:
            hits += 1
            print("  [%s] 属性 %s:" % (cat, a.get("name")))
            if bad:
                print("      垃圾值 = %s" % bad)
            if dup:
                print("      重复值 = %s" % dup)
print("\n有问题的属性数 = %d" % hits)
