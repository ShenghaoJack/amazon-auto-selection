# -*- coding: utf-8 -*-
import sys, re
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

df = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str)
s = df["五点描述"].fillna("").astype(str)

# BSR/排名特征条目: "#数字 in X" 或 "See Top 100"
bsr_pat = re.compile(r"#\s?[\d,]+ in |See Top 100|Best Sellers Rank|#\d+ in ")
has_bsr = s.str.contains(bsr_pat)
print(f"含 BSR/排名特征的五点行: {int(has_bsr.sum())}/{len(df)}")

# 逐条统计: 拆开后含 BSR 的条目数
tot = tot_bsr = 0
for v in s[has_bsr].head(2000):
    for b in v.split("|"):
        b = b.strip()
        if not b:
            continue
        tot += 1
        if bsr_pat.search(b):
            tot_bsr += 1
print(f"(抽样) 条目总数≈{tot}, 其中 BSR/排名条目≈{tot_bsr}")
# 说明: 正常五点条数分布
cnt = s.apply(lambda x: len([b for b in x.split("|") if b.strip()]))
print("五点条数分布:", cnt.value_counts().sort_index().to_dict())
