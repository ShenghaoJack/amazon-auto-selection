# -*- coding: utf-8 -*-
"""验证2: AI表月销额用哪列价; AI价格 vs 竞品价格($) 一致性与覆盖。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ROOT = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets"
ai = pd.read_excel(os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx"), sheet_name="AI已补充", dtype=str)
comp = pd.read_excel(os.path.join(ROOT, "13_查竞品下载", "合并-13_查竞品下载.xlsx"), dtype=str)
print("AI表列:", list(ai.columns))
print("竞品价格列:", [c for c in comp.columns if "价格" in str(c)])

# 1) AI表内: 月销额 = 变体月销量 × 价格 ?
n = ai[["价格", "变体月销量", "变体月销额"]].apply(pd.to_numeric, errors="coerce")
m1 = (n["变体月销额"] - n["变体月销量"] * n["价格"]).abs().dropna()
print(f"\n[AI表] 月销额 = 变体月销量 × 价格 误差均值: {m1.mean():.4f} (n={len(m1)})  -> {'成立' if m1.mean() < 0.01 else '不成立'}")

# 2) AI表价格 vs 竞品价格($) 按 ASIN
ai2 = ai[["变体ASIN", "价格"]].copy()
ai2["变体ASIN"] = ai2["变体ASIN"].astype(str).str.strip()
ai2["价格"] = pd.to_numeric(ai2["价格"], errors="coerce")
cp = comp[["ASIN", "价格($)"]].copy()
cp.columns = ["ASIN", "comp_price"]
cp["ASIN"] = cp["ASIN"].astype(str).str.strip()
cp["comp_price"] = pd.to_numeric(cp["comp_price"], errors="coerce")
cp = cp.dropna(subset=["comp_price"])
cp = cp.sort_values("comp_price").drop_duplicates("ASIN", keep="last")  # 有重复取一个
merged = ai2.merge(cp, left_on="变体ASIN", right_on="ASIN", how="left")
print(f"AI行 {len(merged)}, 竞品有价覆盖: {merged['comp_price'].notna().sum()} ({merged['comp_price'].notna().mean()*100:.0f}%)")
both = merged.dropna(subset=["价格", "comp_price"])
if len(both):
    diff = (both["价格"] - both["comp_price"]).abs()
    print(f"两值都有 {len(both)} 行: 差<=0.01 占比 {(diff<=0.01).mean()*100:.0f}% | 差中位 {diff.median():.2f}")
    print(f"样例: AI价格 vs 竞品价:")
    print(both[["变体ASIN", "价格", "comp_price"]].head(6).to_string(max_colwidth=14))
ai_only = merged[merged["价格"].notna() & merged["comp_price"].isna()]
comp_only = merged[merged["价格"].isna() & merged["comp_price"].notna()]
print(f"仅AI有价 {len(ai_only)} | 仅竞品有价(本可免补抓) {len(comp_only)}")
