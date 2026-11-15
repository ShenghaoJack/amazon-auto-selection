# -*- coding: utf-8 -*-
"""精确定位: 流量拆分数据 xlsx 三个 sheet 中所有价格类列及其来源。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

p = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets\14_流量拆分\流量拆分数据-需人工补充.xlsx"
xl = pd.ExcelFile(p)
print("sheets:", xl.sheet_names)
for sh in xl.sheet_names:
    df = pd.read_excel(p, sheet_name=sh, dtype=str)
    price_cols = [c for c in df.columns if "价格" in str(c)]
    print(f"\n== sheet[{sh}] 行数 {len(df)} 列数 {len(df.columns)}")
    print("  价格类列:", price_cols)
    if "价格" in price_cols and "价格($)" in price_cols:
        sub = df[["变体ASIN", "价格", "价格($)"]].head(8)
        print("  样例:")
        print(sub.to_string(max_colwidth=16))
        n1 = pd.to_numeric(df["价格"], errors="coerce")
        n2 = pd.to_numeric(df["价格($)"], errors="coerce")
        both = n1.notna() & n2.notna()
        print(f"  两列都有值行: {both.sum()}/{len(df)} | 价格空但价格($)有值: {(n1.isna() & n2.notna()).sum()} | 反之: {(n1.notna() & n2.isna()).sum()}")
        if both.sum():
            diff = (n1[both] - n2[both]).abs()
            print(f"  差值<=0.01 占比 {(diff <= 0.01).mean()*100:.0f}% | 差中位 {diff.median():.2f}")
