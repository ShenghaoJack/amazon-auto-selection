# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

df = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str)
for target in ("B06E2214AC", "B04DD7C078", "B0A9BFFE2D"):
    fam = df[df["父ASIN"] == target]
    if fam.empty:
        fam = df[df["变体ASIN"] == target]
    if fam.empty:
        print(target, "未找到")
        continue
    row = fam.iloc[0]
    parts = [x.strip() for x in str(row["五点描述"]).split("|") if x.strip()]
    print(f"== {target} 变体{row['变体ASIN']} 五点共 {len(parts)} 条:")
    for i, b in enumerate(parts, 1):
        print(f"   [{i}] {b[:130]}")
