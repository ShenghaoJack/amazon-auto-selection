# -*- coding: utf-8 -*-
"""端到端模拟(补抓打桩失败): 验证 主价=竞品识别列 / 无价不删 / 月销额口径 / 输出列名。"""
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit, _price_cols_in

cfg = Config.load("config.yaml")
cfg.data["CAT"] = "salt-pepper-shaker-sets"
cfg.data["SITE"] = "美国"
lg = logging.getLogger("sim")
lg.handlers = [logging.NullHandler()]
ctx = StepContext(cfg, Paths(cfg), logger=lg, state=None)
st = Step14TrafficSplit()
st._scrape_page = lambda c, asin: ""   # 补抓全失败(极端), 验证"不删行+人工"
st._scrape_brand = lambda c, asin: ""

d = st._load_detail(ctx)
d = st._merge_images(ctx, d)
d = st._join_competitor(ctx, d)
d = st._join_traffic(ctx, d)
print("join 后价列:", [c for c in d.columns if "价格" in str(c)])
raw = d.copy()
d = st._variant_count_and_filter(ctx, d)
d = st._calc_ratio(ctx, d)
d = st._unify_sales(ctx, d)
d = st._clean_rows(ctx, d)
d = st._fill_brand(ctx, d)
d = st._split_sales(ctx, d)
d = st._fill_price(ctx, d)
d = st._calc_listed_days(ctx, d)

pc = st._price_main_col
print(f"\n主价列识别: {pc} 币种 {st._price_currency} prime={st._price_prime_col}")
print(f"fill_price 后行数: {len(d)} (应保留, 不删)")
n_pc = d[pc].notna().sum()
print(f"主价列有值: {n_pc}/{len(d)} | 空: {int(d[pc].isna().sum())}")
# 月销额口径
chk = d[d[pc].notna()]
diff = (chk['变体月销额'] - chk['变体月销量'] * chk[pc]).abs()
print(f"月销额=变体月销量×{pc} 误差: {diff.max():.4f} (有价行)")
print(f"无价行月销额是否空: {int(d[d[pc].isna()]['变体月销额'].isna().sum())} / {int(d[pc].isna().sum())}")

# 输出层
auto = st._finalize_processed(d)
print(f"\nAI表列({len(auto.columns)}): {list(auto.columns)}")
print(f"AI表 {len(auto)} 行, 主价空: {int(auto[pc].isna().sum())}")
manual_mask = (auto['配送方式']=='未知') | (auto['新老品']=='未知') | auto[pc].isna()
manual = auto[manual_mask]
print(f"人工待补充表: {len(manual)} 行 (价格空 {int(auto[pc].isna().sum())} 行)")
# 对比旧口径: 若用旧'价格'列(补抓价文本)有多少有值
print("\n对照: 若主价列仍有补抓兜底(打桩失败所以全空), 竞品价列原始值样例:")
print(d[[ '变体ASIN', pc, '变体月销量', '变体月销额']].head(4).to_string(max_colwidth=14))
