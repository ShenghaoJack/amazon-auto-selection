# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

p = "data/Cutting-Boards/14_流量拆分/流量拆分数据-需人工补充.xlsx"
for sh in pd.ExcelFile(p).sheet_names:
    df = pd.read_excel(p, sheet_name=sh, dtype=str, nrows=1)
    print(f"sheet[{sh}] 列数 {len(df.columns)}")

ai = pd.read_excel(p, sheet_name="AI已补充", dtype=str, nrows=1)
cols = list(ai.columns)
drop_targets = ["流量来源", "自然搜索词", "AC推荐词", "ER推荐词", "4星推荐词", "HR推荐词", "SP广告词", "视频广告词", "品牌广告词"]
print("应删流量列残留(应0):", [c for c in drop_targets if c in cols])
print("流量词数保留:", "流量词数" in cols)
# 分组概览
print("\nAI列(分段):")
core = ["父ASIN","变体ASIN","标题","五点描述","图片链接","主图","来源ASIN"]
idx = {c: i for i, c in enumerate(cols)}
print("  核心:", [c for c in core if c in idx])
# 竞品到尾/流量词数/计算
t = idx.get("流量词数")
print("  竞品块(SKU→标签):", cols[idx["SKU"]:t+1] if "SKU" in idx else "?")
print("  计算/尾列:", cols[t+1:])
