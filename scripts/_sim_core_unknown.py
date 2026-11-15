# -*- coding: utf-8 -*-
"""111 核心集内: 配送方式/上架时间 未知行的家族覆盖(1413 全家族映射)。"""
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

d = st._load_detail(ctx)
d = st._merge_images(ctx, d)
d = st._join_competitor(ctx, d)
d = st._join_traffic(ctx, d)

def clean(s):
    return s.fillna("").astype(str).str.strip()

# 全家族映射: 父ASIN -> 其它成员已知配送/上架
fam_del = {}
fam_list = {}
for pa, g in d.groupby("父ASIN"):
    dels = clean(g["配送方式"]); lists = clean(g["上架时间"])
    fam_del[pa] = [x for x in dels if x not in ("", "nan", "未知")]
    fam_list[pa] = [x for x in lists if x not in ("", "nan")]

# 111 核心集
f = st._variant_count_and_filter(ctx, d.copy())
f = st._clean_rows(ctx, f)
print(f"核心集行数: {len(f)}")

for col, fam, label in (("配送方式", fam_del, "配送方式"), ("上架时间", fam_list, "上架时间")):
    s = clean(f[col])
    if col == "配送方式":
        miss = s.isin(["", "nan", "未知"])
    else:
        miss = s.isin(["", "nan"])
    n = int(miss.sum())
    print(f"\n[{label}] 核心集未知/空: {n}/{len(f)}")
    if n:
        sub = f[miss]
        covered = 0
        uniq_ok = 0
        for _, r in sub.iterrows():
            vals = fam.get(r["父ASIN"], [])
            if vals:
                covered += 1
                if len(set(vals)) == 1:
                    uniq_ok += 1
        print(f"  家族其它成员有已知值: {covered}/{n} ({covered/n*100:.0f}%) | 其中组内唯一(高置信): {uniq_ok}")
        # 样例
        for _, r in sub.head(5).iterrows():
            vals = fam.get(r["父ASIN"], [])
            print(f"    变体{r['变体ASIN']} 父{r['父ASIN']} 家族已知{label}: {list(set(vals))[:4]}")
    else:
        print(f"  [{label}] 无缺失 —— 该字段在核心集已全覆盖")
