# -*- coding: utf-8 -*-
"""聚焦: 111 行中缺价的 87 个 ASIN, 在过滤前全家族(1413 行, 明细+jsonl+竞品价合并)的价格覆盖。"""
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statistics
import pandas as pd
from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit

cfg = Config.load("config.yaml")
cfg.data["CAT"] = "salt-pepper-shaker-sets"
lg = logging.getLogger("sim")
lg.handlers = [logging.NullHandler()]
ctx = StepContext(cfg, Paths(cfg), logger=lg, state=None)
st = Step14TrafficSplit()

# 全家族视角: merge 后 1413 行(与 run 中 raw 同刻), 再按 _fill_price 规则合并 jsonl 价
d = st._load_detail(ctx)
d = st._merge_images(ctx, d)
d = st._join_competitor(ctx, d)
d = st._join_traffic(ctx, d)
d["价格"] = st._to_float(d["价格"])
d["价格"] = d["价格"].fillna(st._to_float(d.get("__jsonl_price")))
print(f"全家族(过滤前): {len(d)} 行, 价非空 {d['价格'].notna().sum()} ({d['价格'].notna().mean()*100:.0f}%)")

# 111 行核心集(过滤+无销量后), 找缺价 ASIN
f = st._variant_count_and_filter(ctx, d.copy())
f = st._clean_rows(ctx, f)
f["价格"] = st._to_float(f["价格"]).fillna(st._to_float(f.get("__jsonl_price")))
miss = f[f["价格"].isna()]
print(f"核心集(111): {len(f)} 行, 缺价 {len(miss)}")

# 对每个缺价 ASIN: 家族(1413)内 其它成员 的价格
fam_prices = d.groupby("父ASIN")["价格"].apply(lambda x: list(x.dropna())).to_dict()
rows = []
for _, r in miss.iterrows():
    ps = [p for p in fam_prices.get(r["父ASIN"], [])]  # 家族内所有价(包含自身? 自身缺价故不含)
    other = [p for p in ps]  # 该行自身缺价, 家族价均为其它成员
    rows.append((r["变体ASIN"], r["父ASIN"], other))
covered = [r for r in rows if r[2]]
print(f"\n缺价 {len(rows)} 行中, 全家族其它成员有价(可推断): {len(covered)} ({len(covered)/len(rows)*100:.0f}%)")
if covered:
    spreads = []
    for _, _, ps in covered:
        lo, hi = min(ps), max(ps)
        spreads.append((hi - lo) / lo if lo else 1.0)
    t = sum(1 for s in spreads if s <= 0.15); m = sum(1 for s in spreads if 0.15 < s <= 0.50); w = sum(1 for s in spreads if s > 0.50)
    print(f"  家族价差: <=15%: {t} | 15-50%: {m} | >50%: {w} | 中位极差 {statistics.median(spreads)*100:.0f}%")
    print("  样例(可推断):")
    for va, pa, ps in covered[:6]:
        print(f"    变体{va} 父{pa} 家族价({len(ps)}): {[round(x,2) for x in ps[:6]]}")
notcov = [r for r in rows if not r[2]]
print(f"  无家族参考(仍须补抓/删): {len(notcov)}")
# 覆盖行当前在真实流程中都被网页补抓(87 次请求)——若推断覆盖, 省这些请求
print(f"\n=> 推断可跳过网页补抓的请求量: {len(covered)}/{len(miss)} 个")
