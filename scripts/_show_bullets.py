# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

df = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str)
s = df["五点描述"].fillna("")
print(f"行 {len(df)} | 五点非空 {(s!='').sum()} | 长度: 中位 {int(s.str.len().median())} 最大 {int(s.str.len().max())} | 超长>2000: {int((s.str.len()>2000).sum())}")
print()
# 抽几个不同父体(含多变体家族代表 + 单变体)展示
shown = set()
for _, r in df.iterrows():
    pa = r["父ASIN"]
    if pa in shown:
        continue
    shown.add(pa)
    parts = [x.strip() for x in str(r["五点描述"]).split("|") if x.strip()]
    print(f"== 变体{r['变体ASIN']} 父{pa} | 五点 {len(parts)} 条:")
    for i, b in enumerate(parts, 1):
        print(f"   [{i}] {b[:150]}")
    if len(shown) >= 4:
        break
