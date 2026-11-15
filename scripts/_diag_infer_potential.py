# -*- coding: utf-8 -*-
"""量化: step14 输出中 品牌/价格/配送方式/上架时间 的空值与同父体组内推断潜力。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ROOT = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets"
p = os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx")
df = pd.read_excel(p, sheet_name="原始表", dtype=str)
print("原始表行数:", len(df))

def rate(col):
    s = df[col].fillna("").astype(str).str.strip() if col in df.columns else None
    if s is None:
        print(f"  {col}: 列不存在")
        return s
    empty = (s == "") | (s.isin(["nan", "未知"]))
    print(f"  {col}: 空/未知 {(empty).sum()}/{len(df)} = {empty.mean()*100:.1f}%")
    return s

print("== 各列缺失(空 或 '未知') ==")
brand = rate("品牌"); price = rate("价格")
delivery = rate("配送方式"); listed = rate("上架时间")
newold = rate("新老品")

# 对缺失行按父ASIN分组, 统计组内其它变体该字段非空的比例(推断潜力)
def infer_potential(field, missing_mask, name):
    sub = df[missing_mask].copy()
    if sub.empty:
        print(f"== {name}: 无缺失行 ==")
        return
    sub["_g"] = sub.groupby("父ASIN")["父ASIN"].transform("count")
    # 组内其它成员非空计数
    fam_known = df[~missing_mask].groupby("父ASIN")[field].apply(
        lambda x: x.fillna("").astype(str).str.strip().isin(["", "nan", "未知"]).eq(False).sum()
    )
    sub["_fam_known"] = sub["父ASIN"].map(fam_known).fillna(0).astype(int)
    # 唯一已知值数量(同质度)
    fam_uniq = df[~missing_mask].groupby("父ASIN")[field].apply(
        lambda x: x.fillna("").astype(str).str.strip().nunique()
    )
    sub["_fam_uniq"] = sub["父ASIN"].map(fam_uniq).fillna(0).astype(int)
    n = len(sub)
    with_known = (sub["_fam_known"] >= 1).sum()
    uniform = (sub["_fam_uniq"] == 1).sum()  # 组内已知值唯一 -> 可直接推断
    print(f"== {name}: 缺失 {n} 行 | 同父体组内有已知值可推断: {with_known} ({with_known/n*100:.0f}%) | 组内已知值唯一(高置信): {uniform} ({uniform/n*100:.0f}%)")

infer_potential("品牌", (brand == "") | brand.isna() if False else df["品牌"].fillna("").astype(str).str.strip().isin(["", "nan", "未知"]), "品牌缺失")
infer_potential("配送方式", df["配送方式"].fillna("").astype(str).str.strip().isin(["", "nan", "未知"]), "配送方式缺失")
infer_potential("上架时间", df["上架时间"].fillna("").astype(str).str.strip().isin(["", "nan"]), "上架时间缺失")
infer_potential("价格", df["价格"].fillna("").astype(str).str.strip().isin(["", "nan"]), "价格缺失")

# 展示 配送/上架 均未知 且同父体有值的样例族
m = (df["配送方式"].fillna("").astype(str).str.strip().isin(["", "nan", "未知"])) & \
    (df["上架时间"].fillna("").astype(str).str.strip().isin(["", "nan"]))
print("\n配送+上架均缺失行数:", m.sum())
sub = df[m].head(8)
for _, r in sub.iterrows():
    fam = df[df["父ASIN"] == r["父ASIN"]]
    kd = fam[fam["配送方式"].fillna("").astype(str).str.strip().isin(["", "nan", "未知"]).eq(False)]
    print(f"  父{r['父ASIN']} 变体{r['变体ASIN']} | 家族{len(fam)}行 | 已知配送 {kd['配送方式'].nunique()} 种: {kd['配送方式'].dropna().unique()[:2]}")
