# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

p = "data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx"
print("文件 mtime:", os.path.getmtime(p))
df = pd.read_excel(p, sheet_name="变体明细", dtype=str)
print("列:", list(df.columns))
for c in ["月销量", "评分", "流量词数"]:
    if c in df.columns:
        s = df[c].fillna("").astype(str)
        print(f"  [{c}] 非空 {(s.strip()!='').sum()}/{len(df)} 样例: {s[s.strip()!=''].iloc[0][:30]!r}" if (s.strip()!='').any() else f"  [{c}] 全空")
# 统计 sheet 列表
print("sheets:", pd.ExcelFile(p).sheet_names)
