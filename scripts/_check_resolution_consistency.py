# -*- coding: utf-8 -*-
"""只读：摄像头 ↔ 摄像头分辨率 的一致性核查（找出"有分辨率但没摄像头"等矛盾行）。"""
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
CAM, RES = C["摄像头"], C["摄像头分辨率（原标称）"]


def g(r, i):
    return "" if r[i] is None else str(r[i]).strip()


print("摄像头 × 摄像头分辨率 交叉表：")
tab = collections.defaultdict(collections.Counter)
for n, r in rows:
    tab[g(r, CAM) or "(空)"][g(r, RES) or "(空)"] += 1
for k, c in tab.items():
    print("   摄像头=%-6s %s" % (k, dict(c.most_common())))

print("\n=== 矛盾行：有分辨率 但 摄像头 ≠ 是 ===")
bad = [(n, r) for n, r in rows if g(r, RES) not in ("", "未明确") and g(r, CAM) != "是"]
for n, r in bad:
    print("   行%-4d %-13s 摄像头=%-6s 分辨率=%-8s | %s"
          % (n, g(r, C["变体 ASIN"]), g(r, CAM) or "(空)", g(r, RES),
             g(r, C["商品标题"])[:76]))
print("   合计 %d 行" % len(bad))

print("\n=== 反向：摄像头=是 但 分辨率 未明确 ===")
rev = [(n, r) for n, r in rows if g(r, CAM) == "是" and g(r, RES) in ("", "未明确")]
print("   合计 %d 行（这些是能补的候选）" % len(rev))
for n, r in rev[:8]:
    print("   行%-4d %-13s | %s" % (n, g(r, C["变体 ASIN"]), g(r, C["商品标题"])[:70]))

print("\n=== 产品类型 含「重力式」却有分辨率/摄像头的行 ===")
for n, r in rows:
    t = g(r, C["产品类型"])
    if "重力" in t and (g(r, RES) not in ("", "未明确") or g(r, CAM) == "是"):
        print("   行%-4d %-13s 类型=%-12s 摄像头=%-6s 分辨率=%-8s | %s"
              % (n, g(r, C["变体 ASIN"]), t, g(r, CAM) or "-", g(r, RES) or "-",
                 g(r, C["商品标题"])[:60]))
