# -*- coding: utf-8 -*-
"""验证: 月销额用哪列价格 + 竞品'价格($)'对缺价行的覆盖潜力。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ROOT = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets"
raw = pd.read_excel(os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx"), sheet_name="原始表", dtype=str)
ai = pd.read_excel(os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx"), sheet_name="AI已补充", dtype=str)
comp = pd.read_excel(os.path.join(ROOT, "13_查竞品下载", "合并-13_查竞品下载.xlsx"), dtype=str)

print("原始表列数:", len(raw.columns), "| AI表列数:", len(ai.columns))

# 1) 月销额用哪列: 对照误差
num = raw[["价格", "价格($)", "变体月销量", "变体月销额"]].apply(pd.to_numeric, errors="coerce")
m1 = (num["变体月销额"] - num["变体月销量"] * num["价格"]).abs().dropna()
m2 = (num["变体月销额"] - num["变体月销量"] * num["价格($)"]).abs().dropna()
print(f"\n月销额 vs [价格]列   误差均值: {m1.mean():.4f} (n={len(m1)})")
print(f"月销额 vs [价格($)]列 误差均值: {m2.mean():.4f} (n={len(m2)})")

# 2) 两列各自覆盖率
p1 = num["价格"].notna().sum(); p2 = num["价格($)"].notna().sum()
print(f"\n原始表 '价格' 非空 {p1}/{len(raw)} | '价格($)' 非空 {p2}/{len(raw)}")
print(f"'价格' 空但 '价格($)' 有值(本可本地填充): {((num['价格'].isna()) & num['价格($)'].notna()).sum()}")

# 3) 两列值一致性(都有值时)
both = num.dropna(subset=["价格", "价格($)"])
if len(both):
    diff = (both["价格"] - both["价格($)"]).abs()
    print(f"两列都有值 {len(both)} 行, 价格差 <=0.01 的占比: {(diff <= 0.01).mean()*100:.0f}% | 差均值 {diff.mean():.2f}")

# 4) AI已补充(交付111)里的价格来源: 竞品覆盖潜力
aic = ai[["变体ASIN", "价格"]].copy()
comp_p = comp[["ASIN", "价格($)"]].dropna(subset=["价格($)"]) if "价格($)" in comp.columns and "ASIN" in comp.columns else pd.DataFrame()
comp_p["ASIN"] = comp_p["ASIN"].astype(str).str.strip()
ai_asin = set(aic["变体ASIN"].astype(str).str.strip())
comp_asin = set(comp_p["ASIN"])
# AI 表价格已由补抓填满, 这里评估: 若用竞品价, 能覆盖多少(对照补抓必要性)
miss_now = aic["价格"].isna().sum()
print(f"\nAI已补充 {len(ai)} 行, 当前'价格'空: {miss_now}")
print(f"AI 变体 ∩ 竞品表(有价格($)): {len(ai_asin & comp_asin)} 个")
