# -*- coding: utf-8 -*-
"""把「全图版」31 列重跑结果落地：只补当前仍为未明确的格；冲突另出清单。
并统计这 293 条冲突里，有多少是与我本轮早先（6 张上限版）的回填相冲突 —— 那批最值得复核。
"""
from __future__ import annotations

import collections
import csv
import json
import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
PRE = P + ".bak-before-31col-refill.xlsx"          # 本轮回填前的状态
BAK = P + ".bak-before-31col-refill-all.xlsx"
R = os.path.join("scripts", "_reannotate_result_all.json")
CONF = os.path.join("scripts", "_31col_conflicts_all.csv")
MARK = "2026-09-30 人工修正"
ABSENT = {"", "未明确"}
NUMCOL = {"容量（L）", "每日最大餐数（餐/天）", "每餐份数范围（份/餐）",
          "单份克重（g/份）", "语音录制时长（秒）"}
BINS = {"APP远程控制", "摄像头", "夜视", "双向语音", "移动/声音提醒", "语音录制/餐食呼唤",
        "定时定量", "慢食模式", "保鲜密封", "干燥剂盒/防潮", "防卡粮/防堵", "低粮提醒",
        "堵粮检测", "备用供电", "可拆洗/易清洁", "湿粮适用", "冷藏/冰袋保鲜",
        "宠物识别", "多宠管理"}
GRAIN = re.compile(r"^\d+(?:\.\d+)?\s*[–\-~]\s*\d+(?:\.\d+)?$")

shutil.copy2(P, BAK)
print("已备份 ->", os.path.basename(BAK))
d = json.load(open(R, encoding="utf-8"))
KEYS = d["keys"]

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = {str(c.value): c.column for c in ws[1]}
VOCAB = {k: {str(ws.cell(row=r, column=hdr[k]).value or "").strip()
             for r in range(2, ws.max_row + 1)} for k in KEYS if k in hdr}

# 本轮「回填前」的值，用于识别哪些现值是我上一轮填的
prev = {}
wb0 = openpyxl.load_workbook(PRE, read_only=True, data_only=True)
w0 = wb0["Sheet1"]
it0 = w0.iter_rows(values_only=True)
h0 = [str(x) for x in next(it0)]
C0 = {h: i for i, h in enumerate(h0)}
for i, r in enumerate(it0, start=2):
    prev[str(r[C0["变体 ASIN"]]).strip()] = {k: ("" if r[C0[k]] is None else str(r[C0[k]]).strip())
                                            for k in KEYS if k in C0}
wb0.close()


def valid(k, v):
    if k in NUMCOL:
        try:
            float(str(v).replace(",", ""))
            return True
        except ValueError:
            return False
    if k in BINS:
        return v == "是"
    if k == "适用颗粒尺寸（mm）":
        return bool(GRAIN.match(v))
    return v in VOCAB.get(k, set())


def num(v):
    f = float(str(v).replace(",", ""))
    return int(f) if f.is_integer() else f


by_row = {int(x["row"]): x for x in d["results"] if not x.get("error")}
filled, samples = collections.Counter(), collections.defaultdict(list)
for r, x in by_row.items():
    for k in KEYS:
        if k not in hdr:
            continue
        cur = str(ws.cell(row=r, column=hdr[k]).value or "").strip()
        nw = x["new"].get(k, "")
        if cur not in ABSENT or nw in ABSENT or not valid(k, nw):
            continue
        v = num(nw) if k in NUMCOL else nw
        c = ws.cell(row=r, column=hdr[k])
        c.value = v
        if k in NUMCOL and not c.number_format:
            c.number_format = "0.00"
        filled[k] += 1
        if len(samples[k]) < 3:
            samples[k].append((r, v))
print("\n全图版新填 = %d 格" % sum(filled.values()))
for k, v in filled.most_common():
    print("   %-22s %4d  例 %s" % (k, v, samples[k][:2]))

# ---- 冲突 + 是否为「我上轮填的」
rows_conf = []
mine = collections.Counter()
for r, x in by_row.items():
    asin = x["asin"]
    for k in KEYS:
        cur = x["old"].get(k, "")
        nw = x["new"].get(k, "")
        if cur in ABSENT or nw in ABSENT or cur == nw:
            continue
        was_mine = prev.get(asin, {}).get(k, "") in ABSENT
        rows_conf.append((r, asin, k, cur, nw, "本轮我填的" if was_mine else "原表值",
                          x.get("why", "")))
        if was_mine:
            mine[k] += 1
with open(CONF, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Excel行", "变体ASIN", "列", "现值", "全图版判定", "现值来源", "模型依据"])
    w.writerows(rows_conf)
print("\n冲突 = %d 条 -> %s" % (len(rows_conf), CONF))
print("其中「现值是我本轮填的」= %d 条，按列:" % sum(mine.values()))
for k, v in mine.most_common():
    print("   %-22s %4d" % (k, v))

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（31列全图重跑）"
n.cell(row=rr, column=2).value = (
    "修正：上一轮 31 列重跑误把每行图片限制为 6 张（本表 359/415 行超过 6 张，平均 7.8 张、最多 13 张），"
    "本轮改为**使用该行全部图片**重跑。结果：又回填未明确 %d 格；"
    "并对现有值提出 %d 条不同判定（其中 %d 条的现值是本轮早先按 6 张图回填的，"
    "最值得复核）。所有冲突**一律未自动改写**，清单见 脚本 _31col_conflicts_all.csv。"
    % (sum(filled.values()), len(rows_conf), sum(mine.values())))
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
i0, i1 = h.index("产品类型"), h.index("适用颗粒尺寸（mm）")
print("\n===== 复核：31 列未明确 =====")
tot = 0
for j in range(i0, i1 + 1):
    c = sum(1 for r in rows if str(r[j] or "").strip() in ABSENT)
    tot += c
    print("%-22s %6d" % (h[j], c))
print("合计未明确 = %d（原始 9387）" % tot)
