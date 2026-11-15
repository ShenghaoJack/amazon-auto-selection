# -*- coding: utf-8 -*-
"""只读：产品类型 × 联网方式 × APP远程控制 交叉校验。

找出：
  A. 重力式* 却填了联网方式（矛盾）
  B. 联网方式=未明确 但 APP远程控制=是（矛盾）
  C. 联网方式=未明确 且 产品类型本应有联网（可补的候选，需看图判定）
"""
from __future__ import annotations

import collections
import os

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()
C = {h: i for i, h in enumerate(hdr)}
for k in ("产品类型", "联网方式（GHz）", "APP远程控制", "喂食机制", "摄像头",
          "蓝牙" if "蓝牙" in C else "产品卖点", "变体 ASIN", "商品标题"):
    pass
print("相关列:", [h for h in hdr if h in
                 ("产品类型", "联网方式（GHz）", "APP远程控制", "喂食机制", "摄像头",
                  "定时定量", "宠物识别", "慢食模式", "备用供电")])


def g(r, k):
    i = C.get(k)
    v = r[i] if i is not None else None
    return "" if v is None else str(v).strip()


NETBLANK = ("", "未明确")
GRAV = ("重力式喂食器", "重力式食水套装")

print("\n===== 产品类型 × 联网方式 =====")
tab = collections.defaultdict(collections.Counter)
for n, r in rows:
    tab[g(r, "产品类型")][g(r, "联网方式（GHz）") or "(空)"] += 1
for t, c in sorted(tab.items(), key=lambda x: -sum(x[1].values())):
    print("  %-14s 共%3d | %s" % (t, sum(c.values()), dict(c.most_common())))

print("\n===== 产品类型 × APP远程控制 =====")
for t in sorted(tab):
    c = collections.Counter(g(r, "APP远程控制") or "(空)" for n, r in rows if g(r, "产品类型") == t)
    print("  %-14s %s" % (t, dict(c.most_common())))

print("\n===== A. 重力式* 却有联网方式（矛盾）=====")
A = []
for n, r in rows:
    if g(r, "产品类型") not in GRAV:
        continue
    net = g(r, "联网方式（GHz）")
    if net not in NETBLANK:
        A.append((n, r))
        print("  行%-4d %-13s %-16s 联网=%-14s APP=%-4s 机制=%-6s | %s"
              % (n, g(r, "变体 ASIN"), g(r, "产品类型"), net, g(r, "APP远程控制"),
                 g(r, "喂食机制"), g(r, "商品标题")[:56]))
print("  合计 %d 行" % len(A))

print("\n===== B. 联网方式=未明确 但 APP远程控制=是（矛盾）=====")
B = []
for n, r in rows:
    if g(r, "联网方式（GHz）") in NETBLANK and g(r, "APP远程控制") == "是":
        B.append((n, r))
        print("  行%-4d %-13s %-14s APP=是 机制=%-6s | %s"
              % (n, g(r, "变体 ASIN"), g(r, "产品类型"), g(r, "喂食机制"),
                 g(r, "商品标题")[:60]))
print("  合计 %d 行" % len(B))

print("\n===== C. 联网方式=未明确 且 非重力式（可补候选）=====")
Cc = [ (n, r) for n, r in rows
       if g(r, "联网方式（GHz）") in NETBLANK and g(r, "产品类型") not in GRAV ]
cc = collections.Counter(g(r, "产品类型") for n, r in Cc)
print("  合计 %d 行，按产品类型:" % len(Cc), dict(cc.most_common()))

print("\n===== 重力式* 的联网方式未明确（应保持未明确，不需补）=====")
Gc = [(n, r) for n, r in rows
      if g(r, "产品类型") in GRAV and g(r, "联网方式（GHz）") in NETBLANK]
print("  合计 %d 行" % len(Gc))
