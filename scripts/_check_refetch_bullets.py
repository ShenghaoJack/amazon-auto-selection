# -*- coding: utf-8 -*-
import sys, json, re
sys.stdout.reconfigure(encoding="utf-8")

p = "data/Cutting-Boards/Cutting-Boards-results.jsonl"
n = ok = 0
bsr_pat = re.compile(r"#\s?[\d,]+ in |See Top 100|Best Sellers Rank|in [A-Za-z &]+ \(See Top")
samples = []
for line in open(p, encoding="utf-8-sig"):
    line = line.strip()
    if not line:
        continue
    n += 1
    try:
        r = json.loads(line)
    except Exception:
        continue
    if not r.get("ok"):
        continue
    ok += 1
    bullets = r.get("bullets") or []
    if len(samples) < 5:
        samples.append((r.get("request_asin"), bullets))
    if ok <= 200 and bullets:
        bsr_hit = sum(1 for b in bullets if bsr_pat.search(str(b)))
        if bsr_hit:
            print(f"!! BSR 混入: {r.get('request_asin')} bullets={len(bullets)} BSR条目={bsr_hit}")

print(f"\n已抓记录 {n}, 成功 {ok}")
print("=" * 60)
for asin, bullets in samples:
    print(f"== {asin} | {len(bullets)} 条:")
    for i, b in enumerate(bullets, 1):
        print(f"   [{i}] {str(b)[:140]}")
