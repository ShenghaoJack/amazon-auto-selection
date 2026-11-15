# -*- coding: utf-8 -*-
import sys, json, os, re
sys.stdout.reconfigure(encoding="utf-8")

def show(cat, base):
    print("=" * 60)
    print("类目:", cat)
    # 关键词文件是否含 grinder/mill
    for fn in (f"{cat}-keywords-candidates.txt", f"{cat}-keywords.txt"):
        p = os.path.join(base, fn)
        if not os.path.exists(p):
            print(f"  [{fn}] 不存在")
            continue
        lines = [l.strip() for l in open(p, encoding="utf-8-sig") if l.strip()]
        hits = [l for l in lines if re.search(r"grinder|mill", l, re.I)]
        print(f"  [{fn}] 行数 {len(lines)}, 含 grinder/mill: {hits}")
    # 过滤提示词
    import glob
    pp = glob.glob(os.path.join(base, f"filter-*-prompt.json"))
    if pp:
        d = json.load(open(pp[0], encoding="utf-8-sig"))
        print(f"  过滤提示词 hard_exclude: {d.get('hard_exclude')}")
    # 保留池是否含 grinder 商品
    fp = os.path.join(base, f"{cat}-dual-details-filtered.jsonl")
    if os.path.exists(fp):
        n = keep_g = 0
        for l in open(fp, encoding="utf-8-sig"):
            l = l.strip()
            if not l:
                continue
            r = json.loads(l)
            n += 1
            if re.search(r"grinder|mill", str(r.get("title") or ""), re.I):
                keep_g += 1
        print(f"  过滤后保留 {n} 条, 其中标题含 grinder/mill: {keep_g}")

show("salt-pepper-shaker-sets", "data/salt-pepper-shaker-sets")
show("salt-pepper-shaker-sets-v2", "data/salt-pepper-shaker-sets-v2")
