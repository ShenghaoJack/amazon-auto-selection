# -*- coding: utf-8 -*-
"""紧急核对：上一次保存之后文件是否被 WPS 重存过（缓存/图片部件回来了没有），
以及我这次的 openpyxl 保存有没有把回来的东西再弄丢。"""
from __future__ import annotations

import datetime as dt
import os
import zipfile

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
BASE = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"

files = [
    (BASE + ".bak-before-typefix.xlsx", "原始(用户给的)"),
    (BASE + ".bak-before-typefix2.xlsx", "我改31行标签后"),
    (BASE + ".bak-before-delete-others.xlsx", "删行前"),
    (BASE + ".bak-before-capacity-fix.xlsx", "改容量前"),
    (BASE, "当前"),
]
for f, tag in files:
    p = os.path.join(DESK, f)
    if not os.path.exists(p):
        print("缺:", f)
        continue
    z = zipfile.ZipFile(p)
    names = z.namelist()
    media = [n for n in names if "media" in n.lower()]
    ci = [n for n in names if "cellimage" in n.lower()]
    drawing = [n for n in names if "drawing" in n.lower()]
    s1 = z.read("xl/worksheets/sheet1.xml").decode("utf-8", errors="replace")
    ct = z.read("[Content_Types].xml").decode("utf-8")
    print("%-14s %-44s %8d  %s" % (tag, f[-40:], os.path.getsize(p),
                                   dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%m-%d %H:%M:%S")))
    print("     条目=%-3d media=%s cellimages=%s drawing=%s" % (len(names), media or "无", ci or "无", drawing or "无"))
    print("     Content_Types 有图片声明=%s  DISPIMG=%d  缓存文本=%d"
          % (any(k in ct.lower() for k in ("png", "jpeg", "image", "cellimage")),
             s1.count("DISPIMG"), s1.count("<v>=DISPIMG")))
