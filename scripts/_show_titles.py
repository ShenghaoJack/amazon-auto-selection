# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

df = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str)
print("明细行数:", len(df), "| 列:", list(df.columns))
t = df["标题"].fillna("")
print(f"标题非空: {(t!='').sum()}/{len(df)} | 长度: 中位 {int(t.str.len().median())}, 最大 {int(t.str.len().max())}")
print("\n前 12 行(变体ASIN + 标题):")
for _, r in df.head(12).iterrows():
    print(f"  {r['变体ASIN']}  (父{r['父ASIN']})  {str(r['标题'])[:110]}")
