# -*- coding: utf-8 -*-
"""模拟 step14 处理链(补抓打桩失败), 量化“无价格删行”的实际影响与推断潜力。"""
import sys, logging, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit

cfg = Config.load("config.yaml")
cfg.data["CAT"] = "salt-pepper-shaker-sets"   # 指向 salt 真实产物
paths = Paths(cfg)

lg = logging.getLogger("sim14")
lg.handlers = [logging.NullHandler()]
lg.propagate = False

ctx = StepContext(cfg, paths, logger=lg, state=None)
st = Step14TrafficSplit()

# 打桩: 网页补抓全部失败 -> 得到删行上限; jsonl 本地价仍正常合并
st._scrape_brand = lambda ctx_, asin: ""
st._scrape_price = lambda ctx_, asin: None

def P(tag, df):
    print(f"  [{tag}] 行数={len(df)}")
    return df

print("== step14 处理链阶段行数(补抓全失败=删行上限) ==")
detail = P("1 读取明细", st._load_detail(ctx))
detail = P("2 合并图片/jsonl", st._merge_images(ctx, detail))
detail = P("3 关联竞品", st._join_competitor(ctx, detail))
detail = P("4 关联流量", st._join_traffic(ctx, detail))
raw = detail.copy()
detail = P("5 过滤(单变体/多变体+流量词)", st._variant_count_and_filter(ctx, detail))
detail = P("6 流量占比", st._calc_ratio(ctx, detail))
detail = P("7 月销量统一", st._unify_sales(ctx, detail))
detail = P("8 删无销量行", st._clean_rows(ctx, detail))
detail = P("9 品牌补齐(补抓失败)", st._fill_brand(ctx, detail))

brand_unknown = detail["品牌"].fillna("").astype(str).str.strip().eq("未知").sum()
print(f"  -> 品牌仍'未知'行: {brand_unknown}")

detail = P("10 销量拆分", st._split_sales(ctx, detail))

# ---- 价格阶段 ----
before_price = len(detail)
price_missing_before = detail["价格"].isna().sum()
detail = st._fill_price(ctx, detail)   # 内部会先合并 jsonl 价, 再(打桩失败)删行
after_price = len(detail)
deleted = before_price - after_price
print(f"  [11 价格补齐] 进入={before_price} 缺价={price_missing_before} 补抓全失败后删除={deleted} 剩余={after_price}")

# ---- 对被删行的推断潜力分析 ----
print("\n== 被删价格行的同父体推断潜力(若补抓部分成功则删行更少, 此为上限) ==")
if deleted > 0:
    pre = st._variant_count_and_filter(ctx, raw.copy()) if False else None
    # 重新构造到价格前的完整帧以拿到被删行(注: 直接复用上面 detail 无法取被删行, 改在删前快照)
else:
    print("  无删行")

# 更精确: 重跑一遍到 fill_price 前, 快照删前帧与删后 ASIN 差集
detail2 = st._load_detail(ctx)
detail2 = st._merge_images(ctx, detail2)
detail2 = st._join_competitor(ctx, detail2)
detail2 = st._join_traffic(ctx, detail2)
detail2 = st._variant_count_and_filter(ctx, detail2)
detail2 = st._calc_ratio(ctx, detail2)
detail2 = st._unify_sales(ctx, detail2)
detail2 = st._clean_rows(ctx, detail2)
detail2 = st._fill_brand(ctx, detail2)
detail2 = st._split_sales(ctx, detail2)
before_asins = set(detail2["变体ASIN"])
detail3 = st._fill_price(ctx, detail2)
deleted_rows = detail2[~detail2["变体ASIN"].isin(set(detail3["变体ASIN"]))].copy()
print(f"  精算删除行: {len(deleted_rows)}")
if len(deleted_rows):
    fam_known = detail3.groupby("父ASIN")["价格"].apply(
        lambda x: x.notna().sum()
    )
    dr = deleted_rows.copy()
    dr["_fam_known"] = dr["父ASIN"].map(fam_known).fillna(0).astype(int)
    dr["_fam_prices"] = dr["父ASIN"].map(
        detail3.groupby("父ASIN")["价格"].apply(lambda x: list(x.dropna()))
    )
    with_ref = dr["_fam_known"] >= 1
    print(f"  其中同父体组内有价格参考(可推断价保留): {with_ref.sum()}/{len(dr)} ({with_ref.mean()*100:.0f}%)")
    nz = dr[with_ref]
    if len(nz):
        spreads = []
        for _, r in nz.iterrows():
            ps = sorted(r["_fam_prices"])
            spreads.append((ps[-1] - ps[0]) / ps[0] if ps and ps[0] else 1.0)
        import statistics
        med = statistics.median(spreads) if spreads else float("nan")
        tight = sum(1 for s in spreads if s <= 0.15)
        mid = sum(1 for s in spreads if 0.15 < s <= 0.50)
        wide = sum(1 for s in spreads if s > 0.50)
        print(f"  可推断行家族价差分布: 极差<=15%: {tight} | 15-50%: {mid} | >50%: {wide} (家族价差中位 {med*100:.0f}%)")
    no_ref = (~with_ref).sum()
    print(f"  其中同父体无任何价格参考(推断不了): {no_ref}")
    # 删除行里有多少本来就没流量词/单变体(是否低价值行)
    kw_empty = deleted_rows["流量词"].fillna("").astype(str).str.strip().eq("").sum()
    print(f"  被删行中流量词为空: {kw_empty}/{len(deleted_rows)} (单变体父体才有资格无流量词保留)")
