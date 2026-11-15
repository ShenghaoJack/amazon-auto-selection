# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

t = pd.read_excel("data/Cutting-Boards/11_查流量来源/合并-11_查流量来源.xlsx", dtype=str)
print("列:", list(t.columns))
for col in t.columns:
    s = t[col].fillna("").astype(str)
    lens = s.str.len()
    big = s[s.str.len() > 30]
    print(f"[{col}] 非空 {(s.str.strip()!='').sum()}/{len(t)} 最大len {int(lens.max())} 样例: {s.iloc[0][:80]!r}")
    if len(big):
        print(f"   长文本行数 {len(big)}, 样例: {big.iloc[0][:120]!r}")
