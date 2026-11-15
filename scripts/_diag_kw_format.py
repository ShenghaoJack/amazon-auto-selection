# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

t = pd.read_excel("data/Cutting-Boards/11_查流量来源/合并-11_查流量来源.xlsx", dtype=str, nrows=4)
for _, r in t.iterrows():
    v = str(r.get("全部流量词") or "")
    print(f"ASIN={r['ASIN']} 长度={len(v)} 样例前200: {v[:200]!r}")
    print(f"  含空格数={v.count(' ')} 含逗号={v.count(',')} 含换行={v.count(chr(10))}")
    break
