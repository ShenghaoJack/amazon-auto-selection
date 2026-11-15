# -*- coding: utf-8 -*-
"""审计步骤10(变体抓取)的失败构成: 按状态/原因/来源聚合, 并给出失败 ASIN 样本。

用法: python tools/_audit_step10.py "<CAT 数据根目录>" "<CAT>"
"""
import io
import json
import os
import sys
from collections import Counter

root, cat = sys.argv[1], sys.argv[2]


def load_jsonl(path):
    recs = []
    if not os.path.isfile(path):
        return recs
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    return recs


def summarize(name, recs, key_asin):
    print(f"\n===== {name}: {len(recs)} 条 =====")
    if not recs:
        return
    ok = Counter()
    status = Counter()
    src = Counter()
    err = Counter()
    fails = []
    for r in recs:
        o = bool(r.get("ok"))
        ok["ok" if o else "FAILED"] += 1
        status[str(r.get("status") or "?")] += 1
        src[str(r.get("source") or r.get("fetch_source") or "?")] += 1
        if not o:
            e = str(r.get("error") or r.get("err") or r.get("reason") or "?")
            err[e[:60]] += 1
            fails.append((str(r.get(key_asin) or "?"), str(r.get("status") or "?"), e[:110]))
    print("ok/失败:", dict(ok))
    print("status:", dict(status.most_common()))
    print("source:", dict(src.most_common()))
    print("失败原因 Top:")
    for k, v in err.most_common(10):
        print(f"   {v:5d}  {k}")
    print("失败样本(前 12):")
    for asin, st, e in fails[:12]:
        print(f"   {asin}  [{st}]  {e}")


summarize("首轮请求台账 results.jsonl",
          load_jsonl(os.path.join(root, f"{cat}-results.jsonl")), "request_asin")
summarize("子变体文本台账 variant-texts.jsonl",
          load_jsonl(os.path.join(root, f"{cat}-variant-texts.jsonl")), "asin")

qc = os.path.join(root, f"{cat}-变体抓取-质量检查.json")
if os.path.isfile(qc):
    with io.open(qc, encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    print("\n===== 质量检查 json =====")
    for k, v in data.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            print(f"   {k} = {v}")
        elif isinstance(v, dict):
            print(f"   {k}: dict({len(v)}) 键={list(v.keys())[:12]}")
        elif isinstance(v, list):
            print(f"   {k}: list({len(v)}) 前2={v[:2]}")
