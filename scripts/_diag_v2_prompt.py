# -*- coding: utf-8 -*-
import sys, json, os
sys.stdout.reconfigure(encoding="utf-8")
base = "data/salt-pepper-shaker-sets-v2"

pp = os.path.join(base, "filter-salt-pepper-shaker-sets-v2-prompt.json")
print("== 过滤提示词 ==")
if os.path.exists(pp):
    d = json.load(open(pp, encoding="utf-8-sig"))
    for k in d:
        v = str(d[k])
        print(f"[{k}] {v[:800]}")
else:
    print("prompt 文件不存在")

print("\n== dual-details 详情样例(前5条, 标题/五点是否正常) ==")
n = 0
for line in open(os.path.join(base, "salt-pepper-shaker-sets-v2-dual-details.jsonl"), encoding="utf-8-sig"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    n += 1
    if n <= 5:
        print(f"ASIN {r.get('request_asin')} ok={r.get('ok')} status={r.get('status')}")
        print(f"  标题: {str(r.get('title'))[:100]}")
        print(f"  五点: {str((r.get('bullets') or [])[:2])[:150]}")
