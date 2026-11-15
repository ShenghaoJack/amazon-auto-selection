# -*- coding: utf-8 -*-
"""拆解: 明细 3118 → AI已补充 104 的构成(哪些变体为何被过滤)。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

detail = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str)
traffic = pd.read_excel("data/Cutting-Boards/11_查流量来源/合并-11_查流量来源.xlsx", dtype=str)

d = detail[["父ASIN", "变体ASIN"]].copy()
d["父ASIN"] = d["父ASIN"].astype(str).str.strip()
d["变体ASIN"] = d["变体ASIN"].astype(str).str.strip()

fam = d.groupby("父ASIN")["变体ASIN"].nunique()
d["家族大小"] = d["父ASIN"].map(fam)
single = d["家族大小"] == 1
multi = d["家族大小"] > 1
print(f"明细变体总数: {len(d)}")
print(f"  单变体父体变体(家族=1): {int(single.sum())} ({single.mean()*100:.1f}%)")
print(f"  多变体父体变体(家族>1): {int(multi.sum())} ({multi.mean()*100:.1f}%)")
print(f"  父体总数: {d['父ASIN'].nunique()}, 多变体父体数: {d.loc[multi,'父ASIN'].nunique()}")

# 步骤11 查流量来源的 ASIN 覆盖
t = traffic.copy()
t["ASIN"] = t["ASIN"].astype(str).str.strip()
t = t[t["ASIN"] != ""]
kw = t["全部流量词"].fillna("").astype(str).str.strip()
has_kw = t[kw != ""]
print(f"\n步骤11 流量来源表: {len(t)} 行, 有流量词的 ASIN 数: {has_kw['ASIN'].nunique()}")
all_asin = set(d["变体ASIN"])
single_asin = set(d.loc[single, "变体ASIN"])
multi_asin = set(d.loc[multi, "变体ASIN"])
kw_asin = set(has_kw["ASIN"])
print(f"  有词 ASIN 中, 属单变体父体变体: {len(kw_asin & single_asin)}")
print(f"  有词 ASIN 中, 属多变体父体变体: {len(kw_asin & multi_asin)}")
print(f"  有词 ASIN 不在明细(如父体自身/已剔除): {len(kw_asin - all_asin)}")

# 流量表 ASIN 是父体还是子体?
par = set(d["父ASIN"])
print(f"\n  流量表 ASIN 是'父体'的数量: {len(kw_asin & par)} (父体是否在词表)")
print(f"  流量表 ASIN 是'子体变体'的数量: {len(kw_asin & all_asin)}")

# parents.txt 数量(步骤11 只查这些多变体父体)
try:
    with open("data/Cutting-Boards/Cutting-Boards-parents.txt", encoding="utf-8-sig") as f:
        pars = [x.strip() for x in f if x.strip()]
    print(f"\nparents.txt(步骤11 查询清单)多变体父体数: {len(pars)}")
except Exception as e:
    print("parents.txt:", e)
