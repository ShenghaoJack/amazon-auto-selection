# -*- coding: utf-8 -*-
import sys, os, json
sys.stdout.reconfigure(encoding="utf-8")
base = "data/salt-pepper-shaker-sets-v2"

def count_lines(p):
    if not os.path.exists(p):
        return None
    n = 0
    with open(p, encoding="utf-8-sig") as f:
        for _ in f:
            n += 1
    return n

print("00 榜单 all.txt:", count_lines(os.path.join(base, "salt-pepper-shaker-sets-v2-bestseller-newrelease-all.txt")))
for f in ("salt-pepper-shaker-sets-v2-dual-details.jsonl",
          "salt-pepper-shaker-sets-v2-dual-details-filtered.jsonl",
          "salt-pepper-shaker-sets-v2-filtered-dual.txt"):
    p = os.path.join(base, f)
    print(os.path.basename(p), "存在:", os.path.exists(p), "| 行数:", count_lines(p))
# jsonl ok 统计
p1 = os.path.join(base, "salt-pepper-shaker-sets-v2-dual-details.jsonl")
if os.path.exists(p1):
    ok = bad = 0
    for line in open(p1, encoding="utf-8-sig"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("ok"):
            ok += 1
        else:
            bad += 1
    print("dual-details: ok", ok, "失败", bad)
