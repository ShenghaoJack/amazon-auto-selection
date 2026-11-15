# -*- coding: utf-8 -*-
"""核对 DISPIMG 图片引用是否为「本来就没有对应图片部件」的孤立引用。"""
from __future__ import annotations

import os
import re
import zipfile

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
BASE = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"

for tag, f in (("原始(未改动)", BASE + ".bak-before-typefix.xlsx"), ("当前", BASE)):
    z = zipfile.ZipFile(os.path.join(DESK, f))
    ct = z.read("[Content_Types].xml").decode("utf-8")
    names = z.namelist()
    print("== %s" % tag)
    print("   含 image/cellimage 的内容类型声明:",
          bool(re.search(r"image|cellimage|png|jpe?g|emf", ct, re.I)))
    print("   包内媒体部件:", [n for n in names if "media" in n or "cellimage" in n] or "无")
    wr = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    print("   workbook rels:", re.findall(r'Target="([^"]+)"', wr))
    s1 = z.read("xl/worksheets/sheet1.xml").decode("utf-8", errors="replace")
    ids = set(re.findall(r'DISPIMG\("([^"]+)"', s1))
    print("   sheet1 DISPIMG 出现次数 =", s1.count("DISPIMG"), " 唯一图片ID =", len(ids))
    # 关键：这些 ID 在包内有没有对应部件
    print("   → 包内是否存在承载这些 ID 的部件:",
          "是" if any("cellimage" in n.lower() for n in names) else "否（孤立引用/原本就无图）")
    print()
