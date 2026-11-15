# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

p = "data/Cutting-Boards/14_流量拆分/流量拆分数据-需人工补充.xlsx"
xl = pd.ExcelFile(p)
print("sheets:", xl.sheet_names)
ai = pd.read_excel(p, sheet_name="AI已补充", dtype=str)
raw = pd.read_excel(p, sheet_name="原始表", dtype=str)
manual = pd.read_excel(p, sheet_name="人工待补充表", dtype=str)
print(f"原始表 {len(raw)} / AI已补充 {len(ai)} / 人工待补充表 {len(manual)}")
print("\nAI已补充列数:", len(ai.columns))
print("列:", list(ai.columns))
# 多变体拆分样例: 找家族>1 的行展示拆分结果
ai2 = ai.copy()
ai2["父ASIN"] = ai2["父ASIN"].astype(str).str.strip()
fam = ai2.groupby("父ASIN")["变体ASIN"].nunique()
multi_parents = fam[fam > 1].index.tolist()
print(f"\nAI已补充中 多变体父体数: {len(multi_parents)}, 其变体行数: {int(fam[fam > 1].sum())}")
print(f"AI已补充中 单变体父体行数: {int(fam[fam == 1].sum())}")
if multi_parents:
    pa = multi_parents[0]
    sub = ai2[ai2["父ASIN"] == pa][["变体ASIN", "价格($)", "流量词数", "流量占比", "月销量", "变体月销量", "变体月销额", "配送方式", "新老品"]]
    print(f"\n多变体拆分样例 父体={pa}:")
    print(sub.to_string(max_colwidth=16))
# 配送/新老品分布
print("\n配送方式分布:", ai["配送方式"].value_counts(dropna=False).head(8).to_dict())
print("新老品分布:", ai["新老品"].value_counts(dropna=False).to_dict())
print("人工待补充表前5行(配送/上架/价格列):")
print(manual[["变体ASIN", "配送方式", "上架时间", "新老品"]].head(5).to_string(max_colwidth=14))
