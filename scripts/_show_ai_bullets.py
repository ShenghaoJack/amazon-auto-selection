# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ai = pd.read_excel("data/Cutting-Boards/14_流量拆分/流量拆分数据-需人工补充.xlsx", sheet_name="AI已补充", dtype=str)
print(f"AI已补充 {len(ai)} 行 | 五点非空 {(ai['五点描述'].fillna('')!='').sum()}")
shown = set()
for _, r in ai.iterrows():
    if r["父ASIN"] in shown:
        continue
    shown.add(r["父ASIN"])
    parts = [x.strip() for x in str(r["五点描述"]).split("|") if x.strip()]
    print(f"\n== 变体{r['变体ASIN']} 父{r['父ASIN']} | 五点 {len(parts)} 条:")
    for i, b in enumerate(parts, 1):
        print(f"   [{i}] {b[:150]}")
    if len(shown) >= 5:
        break
