# -*- coding: utf-8 -*-
"""只读：把 5 个问题的证据一次查清。"""
from __future__ import annotations

import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
FN2 = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一.xlsx"
P, P2 = os.path.join(DESK, FN), os.path.join(DESK, FN2)


def load(p):
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x) for x in next(it)]
    rows = list(it)
    # 审计表
    aud = {}
    if "属性证据审计" in wb.sheetnames:
        a = wb["属性证据审计"]
        it2 = a.iter_rows(values_only=True)
        h2 = [str(x) for x in next(it2)]
        C2 = {h: i for i, h in enumerate(h2)}
        for r in it2:
            aud.setdefault((str(r[C2["变体ASIN"]]).strip(),
                            str(r[C2["属性字段"]]).strip()), []).append(
                (str(r[C2["提取值"]]), str(r[C2["证据原文"]]), str(r[C2["证据来源"]]),
                 str(r[C2["证据定位"]])))
    wb.close()
    return hdr, rows, aud


hdr, rows, aud = load(P)
C = {h: i for i, h in enumerate(hdr)}

print("=" * 100)
print("【问题1】每日最大餐数（餐/天） = 0 的行")
for i, r in enumerate(rows, start=2):
    if str(r[C["每日最大餐数（餐/天）"]]).strip() == "0":
        a = str(r[C["变体 ASIN"]]).strip()
        print("-" * 100)
        print("行%-4d %-13s 价$%-8s 标题: %s" % (i, a, r[C["价格（USD）"]], str(r[C["商品标题"]])[:120]))
        print("        摘要: %s" % str(r[C["属性证据摘要"]])[:200])
        for e in aud.get((a, "每日最大餐数"), []) + aud.get((a, "每日最大餐数（餐/天）"), []):
            print("        证据: 值=%s | 原文=%s | 来源=%s" % (e[0], e[1][:110], e[2]))

print("\n" + "=" * 100)
print("【问题2】单份克重（g/份）—— 本文件全部非「未明确」的行 + 证据")
for i, r in enumerate(rows, start=2):
    v = str(r[C["单份克重（g/份）"]]).strip()
    if v and v != "未明确":
        a = str(r[C["变体 ASIN"]]).strip()
        print("  行%-4d %-13s 值=%-6s 标题: %s" % (i, a, v, str(r[C["商品标题"]])[:100]))
        for e in aud.get((a, "单份克重"), []) + aud.get((a, "单份克重（g/份）"), []):
            print("        证据: 值=%s | 原文=%s" % (e[0], e[1][:130]))
print("  —— 在本文件里全表搜索含「>」或范围写法的单元格：")
pat = re.compile(r"[>≥]|\d+\s*[-~–—]\s*\d+")
for i, r in enumerate(rows, start=2):
    for j, v in enumerate(r):
        s = "" if v is None else str(v)
        if "g/份" in s or "克重" in s or ("克" in s and pat.search(s)):
            print("     行%-4d 列「%s」= %s" % (i, hdr[j], s[:120]))

print("\n" + "=" * 100)
print("【问题2b】对照文件（无 _已填充About）的单份克重列")
if os.path.exists(P2):
    h2, r2, _ = load(P2)
    c2 = {h: i for i, h in enumerate(h2)}
    key = [h for h in h2 if "单份克重" in h]
    print("  该文件里的列名:", key)
    for k in key:
        vals = sorted({("" if r[c2[k]] is None else str(r[c2[k]]).strip()) for r in r2})
        print("   %s → %d 种取值: %s" % (k, len(vals), vals[:30]))

print("\n" + "=" * 100)
print("【问题3】联网方式 含 Bluetooth 的行")
for i, r in enumerate(rows, start=2):
    v = str(r[C["联网方式（GHz）"]]).strip()
    if "lue" in v:
        a = str(r[C["变体 ASIN"]]).strip()
        print("  行%-4d %-13s 值=%s" % (i, a, v))
        print("        标题: %s" % str(r[C["商品标题"]])[:130])
        print("        摘要: %s" % str(r[C["属性证据摘要"]])[:200])

print("\n" + "=" * 100)
print("【问题4】摄像头分辨率 = 3MP 的行")
for i, r in enumerate(rows, start=2):
    if "3MP" in str(r[C["摄像头分辨率（原标称）"]]):
        a = str(r[C["变体 ASIN"]]).strip()
        print("  行%-4d %-13s 标题: %s" % (i, a, str(r[C["商品标题"]])[:130]))
        for e in aud.get((a, "摄像头分辨率"), []) + aud.get((a, "摄像头分辨率（原标称）"), []):
            print("        证据: 值=%s | 原文=%s" % (e[0], e[1][:150]))

print("\n" + "=" * 100)
print("【问题5】备用电池规格（数量×型号）列")
col = C["备用电池规格（数量×型号）"]
nonblank = [(i, r[col]) for i, r in enumerate(rows, start=2)
            if str(r[col]).strip() not in ("", "未明确")]
print("  该列 = 第 %d 列；非「未明确」的行 %d 行：" % (col + 1, len(nonblank)))
for i, v in nonblank:
    print("     行%-4d %s" % (i, v))
