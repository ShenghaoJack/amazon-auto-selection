# -*- coding: utf-8 -*-
import sys, glob, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

files = sorted(glob.glob("data/Cutting-Boards/11_查流量来源/KeywordCompetitors-*.xlsx"))
print("原始文件数:", len(files))
if files:
    df = pd.read_excel(files[0], dtype=str, nrows=3)
    print("文件:", os.path.basename(files[0]))
    print("列:", list(df.columns))
    for col in df.columns:
        s = df[col].fillna("").astype(str)
        print(f"  [{col}] 样例: {s.iloc[0][:60]!r} 最大len {int(s.str.len().max())}")
