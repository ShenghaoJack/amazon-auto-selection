# -*- coding: utf-8 -*-
import sys, json
sys.stdout.reconfigure(encoding="utf-8")

recs = []
for line in open("data/Cutting-Boards/Cutting-Boards-results.jsonl", encoding="utf-8-sig"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("ok"):
        recs.append(r)
print("jsonl 成功记录:", len(recs))
if not recs:
    raise SystemExit

# 1) request_asin 自身页面字段(标题/五点/图)概览
imgs0 = sum(1 for r in recs if len(r.get("images") or []) == 0)
imgs1 = sum(1 for r in recs if len(r.get("images") or []) == 1)
imgsN = sum(1 for r in recs if len(r.get("images") or []) > 1)
print(f"images: 0图={imgs0} 单图={imgs1} 多图={imgsN}")

# 2) 同父族多记录: 标题/五点是否一致(变体共享 listing 与否)
#    用 parent_asin 归组(记录自身 = request_asin)
fam = {}
for r in recs:
    p = r.get("parent_asin") or r.get("request_asin")
    fam.setdefault(p, []).append(r)
multi = {p: g for p, g in fam.items() if len(g) > 1}
print(f"\n父族数: {len(fam)}, 其中多记录父族(同父被抓了多个ASIN): {len(multi)}")
for p, g in list(multi.items())[:4]:
    print(f"  父 {p} 记录 {len(g)} 条(request_asin): {[r['request_asin'] for r in g]}")
    titles = {str(r.get('title'))[:60] for r in g}
    bhead = {str((r.get('bullets') or [''])[0])[:60] for r in g}
    print(f"    标题唯一数: {len(titles)}")
    for t in list(titles)[:3]:
        print(f"      | {t}")
    print(f"    五点[0]唯一数: {len(bhead)}")
    for b in list(bhead)[:3]:
        print(f"      | {b}")

# 3) 样例 3 条
print("\n== 样例(request_asin 自身页面) ==")
for r in recs[:3]:
    print(f"ASIN={r['request_asin']} parent={r.get('parent_asin')} variants={len(r.get('variant_asins') or [])}")
    print(f"  标题: {str(r.get('title'))[:90]}")
    print(f"  五点({len(r.get('bullets') or [])}): {[str(b)[:40] for b in (r.get('bullets') or [])][:3]}")
    print(f"  图片({len(r.get('images') or [])}): {[str(i)[:60] for i in (r.get('images') or [])][:2]}")
