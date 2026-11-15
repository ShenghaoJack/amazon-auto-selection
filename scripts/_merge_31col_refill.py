# -*- coding: utf-8 -*-
"""把全量重跑的结果落地：仅写「原为未明确/空 且 新值合法」的格（≈1542 格）。
与现值冲突的 275 格一律不写，另出清单交人工。
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-31col-refill.xlsx"
R = os.path.join("scripts", "_reannotate_result.json")
CONF_OUT = os.path.join("scripts", "_31col_conflicts.csv")
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
VOCAB = {}
for k in KEYS:
    if k in hdr:
        VOCAB[k] = {str(ws.cell(row=r, column=hdr[k]).value or "").strip()
                    for r in range(2, ws.max_row + 1)}


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
filled = collections.Counter()
samples = collections.defaultdict(list)
for r, x in by_row.items():
    for k in KEYS:
        if k not in hdr:
            continue
        o, nw = x["old"].get(k, ""), x["new"].get(k, "")
        if o not in ABSENT or nw in ABSENT:
            continue
        if not valid(k, nw):
            continue
        v = num(nw) if k in NUMCOL else nw
        c = ws.cell(row=r, column=hdr[k])
        c.value = v
        if k in NUMCOL and not c.number_format:
            c.number_format = "0.00"
        filled[k] += 1
        if len(samples[k]) < 3:
            samples[k].append((r, v))

print("\n已写入新填 = %d 格" % sum(filled.values()))
for k, v in filled.most_common():
    print("   %-22s %4d   例 %s" % (k, v, samples[k][:2]))

# ---- 审计同步
aud = wb["属性证据审计"]
h2 = {str(c.value): c.column for c in aud[1]}
F, V, S, A2 = h2["属性字段"], h2["提取值"], h2["审核状态"], h2["变体ASIN"]
alias = {k: [k, k.replace("（L）", "").replace("（餐/天）", "").replace("（份/餐）", "")
             .replace("（g/份）", "").replace("（秒）", "").replace("（mm）", "")
             .replace("（原标称）", "").replace("（GHz）", "")] for k in KEYS}
n_aud = 0
for r in range(2, aud.max_row + 1):
    f = str(aud.cell(row=r, column=F).value or "").strip()
    a = str(aud.cell(row=r, column=A2).value or "").strip()
    hitk = next((k for k in KEYS if f in alias.get(k, [k])), None)
    if not hitk:
        continue
    # 找到该 ASIN 对应的 Sheet1 行
    r2 = next((rr for rr, x in by_row.items() if x["asin"] == a), None)
    if r2 is None:
        continue
    nw = by_row[r2]["new"].get(hitk, "")
    o = by_row[r2]["old"].get(hitk, "")
    if o in ABSENT and nw not in ABSENT and valid(hitk, nw):
        aud.cell(row=r, column=V).value = num(nw) if hitk in NUMCOL else nw
        st = str(aud.cell(row=r, column=S).value or "").strip()
        note = ("%s：未明确 → %s（依据：%s）" % (MARK, nw, str(by_row[r2].get("why") or "")[:60]))
        aud.cell(row=r, column=S).value = (st + "；" + note) if st else note
        n_aud += 1
print("审计同步 %d 条" % n_aud)

# ---- 冲突清单
confs = []
for r, x in by_row.items():
    for k in KEYS:
        o, nw = x["old"].get(k, ""), x["new"].get(k, "")
        if o in ABSENT or nw in ABSENT or o == nw:
            continue
        confs.append((r, x["asin"], k, o, nw, x.get("why", "")))
import csv  # noqa: E402
with open(CONF_OUT, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Excel行", "变体ASIN", "列", "现值", "模型判定", "模型依据"])
    w.writerows(confs)
print("冲突清单 -> %s（%d 条）" % (CONF_OUT, len(confs)))

n = wb["提取说明"]
rr = n.max_row + 2
n.cell(row=rr, column=1).value = MARK + "（31列全量重跑）"
n.cell(row=rr, column=2).value = (
    "对「产品类型 → 适用颗粒尺寸（mm）」共 31 列做了**全量重跑**（415 行，每行一次调用："
    "喂入该行标题+详细参数+卖点+最多 6 张图，要求只填有明确证据的、无证据一律未明确、"
    "二值属性只有「是/未明确」不推「否」）。"
    "本次**回填未明确 %d 格**（值域校验后写入；3 格「适用颗粒尺寸」模型给了单值而非范围，已拒绝）。"
    "另产生 %d 格与现值冲突（如 每日最大餐数 10→12、容量误取水容量、喂食机制 定量出粮→旋转餐盘 等），"
    "**一律未自动改写**，清单见 脚本 _31col_conflicts.csv，待人工裁定。"
    % (sum(filled.values()), len(confs)))
wb.save(P)
print("提取说明 追加第 %d 行；已保存" % rr)

# ---- 复核
wb2 = openpyxl.load_workbook(P, read_only=True, data_only=True)
w2 = wb2["Sheet1"]
it = w2.iter_rows(values_only=True)
h = [str(x) for x in next(it)]
rows = list(it)
wb2.close()
i0, i1 = h.index("产品类型"), h.index("适用颗粒尺寸（mm）")
print("\n===== 复核：31 列的未明确缺口变化 =====")
print("%-22s %8s %8s" % ("列", "改动后", "改动前"))
tot_after = tot_before = 0
for j in range(i0, i1 + 1):
    a = sum(1 for r in rows if str(r[j] or "").strip() in ABSENT)
    b = a + filled.get(h[j], 0)
    tot_after += a
    tot_before += b
    print("%-22s %8d %8d" % (h[j], a, b))
print("-" * 40)
print("合计未明确 %d → %d（补了 %d 格）" % (tot_before, tot_after, tot_before - tot_after))
