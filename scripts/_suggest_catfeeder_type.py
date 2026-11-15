# -*- coding: utf-8 -*-
"""只读：对 31 行给出「产品类型」修正建议（按已提取属性推断形态）。"""
from __future__ import annotations

import csv
import os

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
P = os.path.join(DESK, "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx")
wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(h) for h in next(it)]
C = {h: i for i, h in enumerate(hdr)}
rows = [(i + 2, r) for i, r in enumerate(it)]
wb.close()


ABSENT = {"", "未明确", "None", "无", "不适用", "未知"}


def g(r, k):
    i = C.get(k)
    v = "" if i is None or i >= len(r) or r[i] is None else str(r[i]).strip()
    return "" if v in ABSENT else v


def suggest(r):
    cam = g(r, "摄像头") == "是"
    rfid = g(r, "宠物识别") == "是"
    net = bool(g(r, "联网方式（GHz）"))
    app = g(r, "APP远程控制") == "是"
    mech = g(r, "喂食机制")
    wet = g(r, "湿粮适用") == "是" or g(r, "冷藏/冰袋保鲜") == "是"
    if "重力" in mech:
        return "重力式喂食器", "喂食机制=重力式"
    if rfid:
        return "宠物识别喂食器", "宠物识别=是（RFID/芯片门控）"
    if cam:
        return "摄像智能喂食器", "摄像头=是"
    if net or app:
        return "联网智能干粮喂食器", "联网=%s APP=%s" % (g(r, "联网方式（GHz）") or "-",
                                                        "是" if app else "-")
    if wet:
        return "湿粮/冷藏喂食器", "湿粮/冷藏=是"
    if "定量" in mech:
        return "程序定时干粮喂食器", "喂食机制=定量出粮，且**无**联网/APP/摄像证据"
    return "其他非自动喂食用品", "无主品形态证据"


picks = [(n, r) for n, r in rows if g(r, "产品类型") == "配件/耗材"]
out = os.path.join("scripts", "_catfeeder_产品类型修正建议.csv")
with open(out, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Excel行", "父ASIN", "变体ASIN", "现产品类型", "建议产品类型",
                "判据", "月销量", "价格USD"])
    for n, r in picks:
        s, why = suggest(r)
        w.writerow([n, g(r, "父 ASIN"), g(r, "变体 ASIN"), g(r, "产品类型"),
                    s, why, g(r, "月销量（件/月）"), g(r, "价格（USD）")])

print("%-8s %-13s %-16s %-18s %-22s %s"
      % ("Excel行", "变体ASIN", "现类型", "建议类型", "判据", "月销/价"))
print("-" * 118)
from collections import Counter
cnt = Counter()
for n, r in picks:
    s, why = suggest(r)
    cnt[s] += 1
    print("%-8d %-13s %-16s %-18s %-22s %s / $%s"
          % (n, g(r, "变体 ASIN")[:13], g(r, "产品类型"), s, why,
             g(r, "月销量（件/月）"), g(r, "价格（USD）")))
print("-" * 118)
print("修正后分布：", dict(cnt))
print("已导出 ->", out)
