# -*- coding: utf-8 -*-
import sys, json
sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("data/salt-pepper-shaker-sets-v2/salt-pepper-shaker-sets-v2-amazon-keyword-data.json", encoding="utf-8-sig"))
sc = {}
try:
    sc = {r.get("keyword"): r for r in json.load(open("data/salt-pepper-shaker-sets-v2/salt-pepper-shaker-sets-v2-screen-results.json", encoding="utf-8-sig"))}
except Exception:
    pass

hdr = f"{'词':<42}{'月搜索量':>10}{'站内同比':>9}{'Google同比':>10}{'CPC':>7}  {'判定':<6}原因"
print(hdr)
print("-" * 130)
for r in sorted(data, key=lambda x: -(x.get("monthly_search_volume") or 0)):
    kw = r.get("keyword", "")
    s = sc.get(kw, {})
    gy = r.get("google_yoy") or r.get("google_yoy_avg")
    gy_s = f"{gy:.1f}%" if isinstance(gy, (int, float)) else "-"
    print(
        f"{kw:<42}{r.get('monthly_search_volume') or 0:>10.0f}"
        f"{(r.get('mom_yoy') or 0):>8.1f}%{gy_s:>10}"
        f"{(r.get('cpc') or 0):>7.2f}  "
        f"{(s.get('verdict') or s.get('判定') or ''):<6}{s.get('reason','')}"
    )
