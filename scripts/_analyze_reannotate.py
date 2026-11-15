# -*- coding: utf-8 -*-
"""只读：校验全量重跑结果的值域合法性，并把 275 个冲突分类。"""
from __future__ import annotations

import collections
import json
import os
import re

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
R = os.path.join("scripts", "_reannotate_result.json")

wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
ws = wb["Sheet1"]
it = ws.iter_rows(values_only=True)
hdr = [str(x) for x in next(it)]
rows = list(it)
wb.close()
C = {h: i for i, h in enumerate(hdr)}
KEYS = list(json.load(open(R, encoding="utf-8"))["keys"])
d = json.load(open(R, encoding="utf-8"))

NUMCOL = {"容量（L）", "每日最大餐数（餐/天）", "每餐份数范围（份/餐）",
          "单份克重（g/份）", "语音录制时长（秒）"}
BINS = {"APP远程控制", "摄像头", "夜视", "双向语音", "移动/声音提醒", "语音录制/餐食呼唤",
        "定时定量", "慢食模式", "保鲜密封", "干燥剂盒/防潮", "防卡粮/防堵", "低粮提醒",
        "堵粮检测", "备用供电", "可拆洗/易清洁", "湿粮适用", "冷藏/冰袋保鲜",
        "宠物识别", "多宠管理"}
# 各列的合法值域：以文件现有取值为准
VOCAB = {}
for k in KEYS:
    if k in C:
        VOCAB[k] = {("" if r[C[k]] is None else str(r[C[k]]).strip()) for r in rows}
VOCAB["适用颗粒尺寸（mm）"] = None      # 用正则
GRAIN = re.compile(r"^\d+(?:\.\d+)?\s*[–\-~]\s*\d+(?:\.\d+)?$")
ABSENT = {"", "未明确"}


def valid(k, v):
    if v in ABSENT:
        return True
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


bad = collections.Counter()
badsamp = collections.defaultdict(list)
fills = collections.Counter()
conf = collections.Counter()
confsamp = collections.defaultdict(list)
for x in d["results"]:
    if x.get("error"):
        continue
    for k in KEYS:
        o, nw = x["old"].get(k, ""), x["new"].get(k, "")
        if nw in ABSENT:
            continue
        if not valid(k, nw):
            bad[k] += 1
            if len(badsamp[k]) < 4:
                badsamp[k].append((x["row"], o, nw))
        if o in ABSENT:
            fills[k] += 1
        elif o != nw:
            conf[k] += 1
            if len(confsamp[k]) < 5:
                confsamp[k].append((x["row"], o, nw, x.get("why", "")[:60]))

print("=== 值域非法（会被拒绝，不写入） ===")
for k, v in bad.most_common():
    print("  %-22s %4d  例: %s" % (k, v, badsamp[k][:2]))
print("  合计非法 = %d" % sum(bad.values()))
print("\n=== 可写入的新填（原为未明确，且值合法） ===")
tot = 0
for k, v in fills.most_common():
    ok = v - bad.get(k, 0)
    tot += ok
    print("  %-22s %4d" % (k, ok))
print("  合计新填 ≈ %d" % tot)
print("\n=== 与现值冲突（不予自动写入，交人工） ===")
for k, v in conf.most_common():
    print("  %-22s %4d" % (k, v))
    for row, o, nw, why in confsamp[k][:3]:
        print("        行%-4d %-16s → %-16s | %s" % (row, o, nw, why))
print("  合计冲突 = %d" % sum(conf.values()))
