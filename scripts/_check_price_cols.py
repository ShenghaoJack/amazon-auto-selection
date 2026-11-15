# -*- coding: utf-8 -*-
"""核实: 图片链接/主图分隔符现状 + 两个价格列的来源与使用口径。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ROOT = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets"
detail = pd.read_excel(os.path.join(ROOT, "salt-pepper-shaker-sets-父项变体明细.xlsx"), sheet_name="变体明细", dtype=str)
ai = pd.read_excel(os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx"), sheet_name="AI已补充", dtype=str)
raw = pd.read_excel(os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx"), sheet_name="原始表", dtype=str)

print("== 1) 图片链接分隔符 ==")
for name, df in (("step10明细", detail), ("AI已补充", ai)):
    col = df["图片链接"].fillna("") if "图片链接" in df.columns else None
    if col is None:
        print(f"  {name}: 无图片链接列"); continue
    s = col.astype(str)
    n_semi = s.str.contains(";").sum()
    n_comma = s.str.contains(",").sum()
    both = s.str.contains(";") & s.str.contains(",")
    print(f"  {name}: 含';' {n_semi} 行 | 含',' {n_comma} 行 | 两者都有 {both.sum()} 行")

print("\n== 2) 主图列是否整串错误(分号数据被 split(',') 吞掉) ==")
for name, df in (("原始表", raw), ("AI已补充", ai)):
    if "主图" not in df.columns:
        print(f"  {name}: 无主图列"); continue
    img = df["图片链接"].fillna("").astype(str)
    main = df["主图"].fillna("").astype(str)
    semi_rows = img.str.contains(";")
    err = semi_rows & (main != img.str.split(";").str[0].str.strip())
    print(f"  {name}: 图片链接含';'行 {semi_rows.sum()}, 其中主图≠首图(整串错误) {err.sum()}")
    if err.sum():
        i = img[semi_rows].index[0]
        print(f"    样例 图片链接: {img[i][:150]}")
        print(f"        主图列值: {main[i][:150]}")

print("\n== 3) 价格列清单与口径 ==")
for name, df in (("原始表", raw), ("AI已补充", ai)):
    price_cols = [c for c in df.columns if "价格" in str(c)]
    print(f"  {name} 含'价格'的列: {price_cols}")
# 竞品表是否有 价格($)
comp = pd.read_excel(os.path.join(ROOT, "13_查竞品下载", "合并-13_查竞品下载.xlsx"), dtype=str, nrows=3)
print("  竞品表含'价格'的列:", [c for c in comp.columns if "价格" in str(c)])
print("  明细表含'价格'的列:", [c for c in detail.columns if "价格" in str(c)])
# AI表: 月销额 与 两列价格的关系(若都有)
if "价格($)" in raw.columns and "价格" in raw.columns:
    sub = raw[["变体ASIN", "价格", "价格($)", "变体月销量", "变体月销额"]].head(5)
    print("\n  原始表样例(价格 vs 价格($) vs 月销额):")
    print(sub.to_string(max_colwidth=18))
    # 若月销额=变体月销量*价格 成立, 说明用的是"价格"列
    num = raw[["价格", "价格($)", "变体月销量", "变体月销额"]].apply(pd.to_numeric, errors="coerce")
    m1 = (num["变体月销额"] - num["变体月销量"] * num["价格"]).abs().dropna()
    m2 = (num["变体月销额"] - num["变体月销量"] * num["价格($)"]).abs().dropna()
    print(f"  月销额与[价格]列误差均值: {m1.mean():.3f} (行数 {len(m1)})")
    print(f"  月销额与[价格($)]列误差均值: {m2.mean():.3f} (行数 {len(m2)})")
