# -*- coding: utf-8 -*-
import sys, json, re
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

print(f"校验记录: {len(recs)} 条\n")

# ---- 标题 ----
titles = [str(r.get("title") or "") for r in recs]
empty_t = sum(1 for t in titles if not t.strip())
html_t = sum(1 for t in titles if "<" in t or "&" in t.lower().replace("&amp;", ""))
script_t = sum(1 for t in titles if re.search(r"if\(window|mix_csa|vjs-|logShoppable", t))
print(f"[标题] 空: {empty_t} | 含HTML/实体: {html_t} | 含脚本特征: {script_t}")
print(f"       长度: min {min(len(t) for t in titles)} / max {max(len(t) for t in titles)}")

# ---- 五点 ----
cnts = [len(r.get("bullets") or []) for r in recs]
zero = sum(1 for c in cnts if c == 0)
bsr_pat = re.compile(r"#\s?[\d,]+ in |See Top 100|Best Sellers Rank")
script_pat = re.compile(r"if\(window|mix_csa|vjs-|logShoppable|_dnNlL")
long_b = 0
bsr_hits = scr_hits = 0
empty_item = 0
for r in recs:
    for b in r.get("bullets") or []:
        b = str(b)
        if len(b) > 2000:
            long_b += 1
        if bsr_pat.search(b):
            bsr_hits += 1
        if script_pat.search(b):
            scr_hits += 1
        if not b.strip():
            empty_item += 1
from collections import Counter
print(f"[五点] 记录条数分布: {dict(sorted(Counter(cnts).items()))} | 0条记录: {zero}")
print(f"      长>2000条目: {long_b} | BSR/排名条目: {bsr_hits} | 脚本/组件条目: {scr_hits} | 空条目: {empty_item}")

# ---- 图片 ----
imgs = [r.get("images") or [] for r in recs]
n0 = sum(1 for i in imgs if not i)
n1 = sum(1 for i in imgs if len(i) == 1)
nN = sum(1 for i in imgs if len(i) > 1)
video = sum(1 for i in imgs for u in i if "video" in str(u).lower())
thumb = sum(1 for i in imgs for u in i if "_AC_US" in str(u) or "_AC_SL40" in str(u) or "_AC_SX" in str(u))
dup = sum(1 for i in imgs if len(i) != len(set(i)))
print(f"[图片] 0图:{n0} 单图:{n1} 多图:{nN} | 含video帧:{video} | 未还原缩略图URL:{thumb} | 同记录重复URL:{dup}")

# ---- 自身记录 / 变体 ----
reqs = [str(r.get("request_asin")) for r in recs]
print(f"\n[去重] request_asin 唯一: {len(set(reqs))}/{len(reqs)}")
multiv = sum(1 for r in recs if len(r.get("variant_asins") or []) > 1)
print(f"多变体页记录: {multiv}/{len(recs)}")

# ---- 同父标题/五点共享 ----
fam = {}
for r in recs:
    p = str(r.get("parent_asin") or r.get("request_asin"))
    fam.setdefault(p, []).append(r)
m = {p: g for p, g in fam.items() if len(g) > 1}
same_t = same_b = 0
for g in m.values():
    if len({str(x.get('title')) for x in g}) == 1:
        same_t += 1
    if len({tuple(str(b) for b in (x.get('bullets') or [])) for x in g}) == 1:
        same_b += 1
print(f"[同父多记录父族] {len(m)} 个 | 标题全同: {same_t} | 五点完全相同: {same_b}")
