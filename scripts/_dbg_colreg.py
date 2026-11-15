# -*- coding: utf-8 -*-
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit

cfg = Config.load("config.cb.yaml")
lg = logging.getLogger("dbg")
lg.handlers = [logging.NullHandler()]
ctx = StepContext(cfg, Paths(cfg), logger=lg, state=None)
st = Step14TrafficSplit()
st._col_order = []
d = st._load_detail(ctx)
print("detail 列:", list(d.columns))
d = st._merge_images(ctx, d)
d = st._join_competitor(ctx, d)
print("\njoin竞品后 detail 列(前30):", list(d.columns)[:30])
d = st._join_traffic(ctx, d)
print("\njoin流量后 detail 列(前40):", list(d.columns)[:40])
print("\n_col_order 前30:", st._col_order[:30])
print("_col_order 长度:", len(st._col_order), "detail列数:", len(d.columns))
