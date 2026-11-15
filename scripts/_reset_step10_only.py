# -*- coding: utf-8 -*-
"""只清步骤10产物+状态(保留 11-13 卖家精灵数据与上游), 供 step 10 全量重抓。"""
import os, io

CAT = "Cutting-Boards"
base = f"data/{CAT}"
targets = [
    f"{CAT}-results.jsonl",
    f"{CAT}-父项变体明细.xlsx",
    f"{CAT}-parents.txt",
    f"{CAT}-variants.txt",
    f"{CAT}-过滤审计.csv",
    f"{CAT}-复核-不一致项.csv",
    f"{CAT}-复核-过滤审计.csv",
]
removed = []
for t in targets:
    p = os.path.join(base, t)
    if os.path.exists(p):
        os.remove(p)
        removed.append(t)
print("删除产物:", removed)

status = os.path.join(base, "17_备份与监控", "step-status.txt")
if os.path.exists(status):
    kept = []
    for line in io.open(status, encoding="utf-8-sig"):
        line = line.strip()
        if not line:
            continue
        try:
            num = int(line.split("|")[0])
        except ValueError:
            continue
        if num == 10:
            continue
        kept.append(line)
    io.open(status, "w", encoding="utf-8").write("\n".join(kept) + "\n")
    print("step-status: 已移除步骤 10 状态行(11+ 保留)")
print("完成, 可执行 step 10 全量重抓")
