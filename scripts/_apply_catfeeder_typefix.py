# -*- coding: utf-8 -*-
"""把 Automatic Cat Feeders 表里「产品类型 = 配件/耗材」的 31 行改成实际形态。

依据：31 行标题全部含 feeder/dispenser 主品词、0 行含 replacement/refill 等耗材词，
属性证据全部是主品特征（喂食机制=定量出粮、容量 3–7L、定时定量=是…）。
误判源于标题带配件词（with Desiccant Bag / Elevated Stand / Large Food Tray / bundle 含干燥剂）。

做四件事（先备份原文件）：
  ① 备份 `<原文件>.bak-before-typefix.xlsx`
  ② Sheet1：31 行的「产品类型」改为建议值
  ③ 属性证据审计：对应行的「提取值」同步改为新值，并在「审核状态」追加人工修正标记
  ④ 提取说明：追加一条修正记录（不重算旧口径表格）
"""
from __future__ import annotations

import os
import shutil
from collections import Counter

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
BAK = P + ".bak-before-typefix.xlsx"
MARK = "2026-09-29 人工修正"

ABSENT = {"", "未明确", "None", "无", "不适用", "未知"}

# ---------- ① 备份
shutil.copy2(P, BAK)
print("① 已备份 ->", os.path.basename(BAK), os.path.getsize(BAK), "bytes")

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
hdr = [str(c.value) if c.value is not None else "" for c in ws[1]]
C = {h: i + 1 for i, h in enumerate(hdr)}      # 1-based 列号


def cell(r, name):
    v = ws.cell(row=r, column=C[name]).value
    s = "" if v is None else str(v).strip()
    return "" if s in ABSENT else s


def suggest(r):
    """与 scripts/_suggest_catfeeder_type.py 同一套判据。"""
    cam = cell(r, "摄像头") == "是"
    rfid = cell(r, "宠物识别") == "是"
    net = bool(cell(r, "联网方式（GHz）"))
    app = cell(r, "APP远程控制") == "是"
    mech = cell(r, "喂食机制")
    wet = cell(r, "湿粮适用") == "是" or cell(r, "冷藏/冰袋保鲜") == "是"
    if "重力" in mech:
        return "重力式喂食器"
    if rfid:
        return "宠物识别喂食器"
    if cam:
        return "摄像智能喂食器"
    if net or app:
        return "联网智能干粮喂食器"
    if wet:
        return "湿粮/冷藏喂食器"
    if "定量" in mech:
        return "程序定时干粮喂食器"
    return "其他非自动喂食用品"


# ---------- ② 改 Sheet1
targets = {}          # Excel行 -> (变体ASIN, 新类型)
for r in range(2, ws.max_row + 1):
    if str(ws.cell(row=r, column=C["产品类型"]).value or "").strip() == "配件/耗材":
        new = suggest(r)
        targets[r] = (str(ws.cell(row=r, column=C["变体 ASIN"]).value or "").strip(), new)
        ws.cell(row=r, column=C["产品类型"]).value = new
print("② Sheet1 已改 %d 行：%s" % (len(targets), dict(Counter(v[1] for v in targets.values()))))

# ---------- ③ 同步属性证据审计
asins = {v[0]: v[1] for v in targets.values() if v[0]}
aud = wb["属性证据审计"]
h2 = [str(c.value) if c.value is not None else "" for c in aud[1]]
C2 = {h: i + 1 for i, h in enumerate(h2)}
n_aud = 0
for r in range(2, aud.max_row + 1):
    if str(aud.cell(row=r, column=C2["属性字段"]).value or "").strip() != "产品类型":
        continue
    a = str(aud.cell(row=r, column=C2["变体ASIN"]).value or "").strip()
    if a not in asins:
        continue
    old = str(aud.cell(row=r, column=C2["提取值"]).value or "").strip()
    aud.cell(row=r, column=C2["提取值"]).value = asins[a]
    st = str(aud.cell(row=r, column=C2["审核状态"]).value or "").strip()
    note = ("%s：原判「%s」→「%s」（标题含配件词 Desiccant Bag/Elevated Stand/"
            "Food Tray 致误判，实为自动喂食器主品）" % (MARK, old, asins[a]))
    aud.cell(row=r, column=C2["审核状态"]).value = (st + "；" + note) if st else note
    n_aud += 1
print("③ 属性证据审计 同步 %d 条" % n_aud)

# ---------- ④ 提取说明 追加修正记录
note_ws = wb["提取说明"]
r = note_ws.max_row + 2
note_ws.cell(row=r, column=1).value = MARK
note_ws.cell(row=r, column=2).value = (
    "产品类型列：%d 行由「配件/耗材」改为实际形态（程序定时干粮喂食器 18 / "
    "联网智能干粮喂食器 11 / 摄像智能喂食器 1 / 宠物识别喂食器 1）；"
    "原因：标题含配件词（with Desiccant Bag、Elevated Stand、Large Food Tray、"
    "bundle 含 6 包干燥剂）被误判为耗材，实际均为自动喂食器主品，故未删除。"
    "本表上方按 494 行的旧口径统计未随之重算。" % len(targets))
print("④ 提取说明 追加第 %d 行修正记录" % r)

wb.save(P)
print("已保存 ->", P)
