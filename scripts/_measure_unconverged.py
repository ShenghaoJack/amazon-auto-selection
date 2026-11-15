# -*- coding: utf-8 -*-
"""只读：标准化交付表里，到底有多少值其实**没被收敛**（=待归类的落点）。"""
from __future__ import annotations

import collections
import json
import os

import openpyxl

CAT = "Salt and Pepper Mill Sets-US-202608"
BASE = os.path.join("data", CAT)
XLSX = os.path.join(BASE, "18_标准化", "%s-属性标注-标准化交付.xlsx" % CAT)
RULES = os.path.join(BASE, "salt_pepper_mill.rules.json")

d = json.load(open(RULES, encoding="utf-8"))
vocab = {}
for a in (d.get("attributes") or []):
    p = a.get("params") or {}
    vocab[a["name"]] = [str(v) for v in (a.get("values") or p.get("values") or [])]

wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
ws = wb["属性标注"]
it = ws.iter_rows(values_only=True)
hdr = [str(h) for h in next(it)]
rows = list(it)
wb.close()
C = {h: i for i, h in enumerate(hdr)}
RESERVED = {"未明确", "不适用", "未知", "待确认", ""}

print("%-14s %6s %8s %8s %9s %s" % ("属性", "词表", "表内取值", "表内越界", "越界占比", "越界样例"))
print("-" * 118)
tot_out = tot_cells = 0
detail = {}
for name in [h for h in hdr if h in vocab]:
    vs = set(vocab[name])
    vals = [str(r[C[name]]).strip() if r[C[name]] is not None else "" for r in rows]
    uniq = {v for v in vals if v not in RESERVED}
    out = {v for v in uniq if v not in vs}
    outside_cells = sum(1 for v in vals if v and v not in RESERVED and v not in vs)
    tot_out += outside_cells
    tot_cells += sum(1 for v in vals if v and v not in RESERVED)
    detail[name] = (len(vs), len(uniq), sorted(out))
    print("%-14s %6d %8d %8d %8.1f%% %s"
          % (name, len(vs), len(uniq), len(out),
             100.0 * outside_cells / max(1, sum(1 for v in vals if v and v not in RESERVED)),
             "、".join(sorted(out)[:3])[:44]))
print("-" * 118)
print("合计：有值格 %d，其中不在受控词表内的 %d 格（%.1f%%）"
      % (tot_cells, tot_out, 100.0 * tot_out / max(1, tot_cells)))
