# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
from datetime import datetime

ai = pd.read_excel("data/Cutting-Boards/14_流量拆分/流量拆分数据-需人工补充.xlsx", sheet_name="AI已补充", dtype=str)
sub = ai[["变体ASIN", "上架时间", "上架天数", "新老品"]].copy()
sub = sub[sub["上架时间"].fillna("") != ""].head(6)
today = datetime.now().date()
print("今天:", today)
for _, r in sub.iterrows():
    t = pd.to_datetime(r["上架时间"], errors="coerce")
    days = (today - t.date()).days if t is not None and pd.notna(t) else None
    print(f"  变体{r['变体ASIN']} 上架时间={r['上架时间']}  上架天数={r['上架天数']} (按今天算应={days})  新老品={r['新老品']}")
