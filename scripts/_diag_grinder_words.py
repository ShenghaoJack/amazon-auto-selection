# -*- coding: utf-8 -*-
import sys, json, re
sys.stdout.reconfigure(encoding="utf-8")
base = "data/salt-pepper-shaker-sets-v2"

all_recs = [json.loads(l) for l in open(f"{base}/salt-pepper-shaker-sets-v2-dual-details.jsonl", encoding="utf-8-sig") if l.strip()]
kept_asin = set()
try:
    with open(f"{base}/salt-pepper-shaker-sets-v2-dual-details-filtered.jsonl", encoding="utf-8-sig") as f:
        kept_asin = {json.loads(l).get("request_asin") for l in f if l.strip()}
except Exception:
    pass

pat = re.compile(r"pepper\s*(mill|grinder)|salt\s*(mill|grinder)|grinder", re.I)
in_all = [r for r in all_recs if pat.search(str(r.get("title") or ""))]
print(f"榜单详情 152 条中标题含 grinder/mill 的商品: {len(in_all)}")
in_kept = [r for r in in_all if r.get("request_asin") in kept_asin]
print(f"其中保留(03 过滤后): {len(in_kept)} | 被剔除: {len(in_all) - len(in_kept)}")
for r in in_all[:8]:
    print(f"  {'保留' if r.get('request_asin') in kept_asin else '剔除'} {r.get('request_asin')}: {str(r.get('title'))[:90]}")
print(f"\n保留总数 58, 含 grinder/mill 词条数: {sum(1 for r in all_recs if r.get('request_asin') in kept_asin and pat.search(str(r.get('title') or '')))}")

print("\n== 过滤提示词 hard_exclude ==")
d = json.load(open(f"{base}/filter-salt-pepper-shaker-sets-v2-prompt.json", encoding="utf-8-sig"))
print("hard_exclude:", d.get("hard_exclude"))
