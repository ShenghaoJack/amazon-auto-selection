# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

sys.path.insert(0, ".")
from steps.step14_traffic_split import _TRAFFIC_ALIASES

for cat in ("Cutting-Boards", "salt-pepper-shaker-sets"):
    t = pd.read_excel(f"data/{cat}/11_查流量来源/合并-11_查流量来源.xlsx", dtype=str, nrows=2)
    cols = list(t.columns)
    canon_flow = _TRAFFIC_ALIASES.get("流量词", [])
    canon_cnt = _TRAFFIC_ALIASES.get("流量词数", [])
    has_all = "全部流量词" in cols
    print(f"[{cat}] 流量表列: {cols}")
    print(f"  含'全部流量词'列: {has_all}")
    print(f"  别名[流量词]含'全部流量词': {'全部流量词' in canon_flow} | 别名[流量词数]含'全部流量词': {'全部流量词' in canon_cnt}")
