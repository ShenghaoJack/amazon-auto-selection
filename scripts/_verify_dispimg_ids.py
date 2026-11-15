# -*- coding: utf-8 -*-
"""证明 openpyxl 往返没有丢失 DISPIMG 图片 ID（原始文件里的引号是 XML 转义的）。"""
from __future__ import annotations

import os
import re
import zipfile

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
BASE = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"


def ids_of(path):
    z = zipfile.ZipFile(path)
    s = z.read("xl/worksheets/sheet1.xml").decode("utf-8", errors="replace")
    s = s.replace("&quot;", '"').replace("&apos;", "'")      # 反转义后再抽 ID
    ids = re.findall(r'DISPIMG\("([^"]+)"', s)
    return ids, s


a_ids, a = ids_of(os.path.join(DESK, BASE + ".bak-before-typefix.xlsx"))
b_ids, b = ids_of(os.path.join(DESK, BASE))
print("原始 DISPIMG ID 数 =", len(a_ids), " 唯一 =", len(set(a_ids)))
print("当前 DISPIMG ID 数 =", len(b_ids), " 唯一 =", len(set(b_ids)))
sa, sb = set(a_ids), set(b_ids)
print("原始有、当前没有的 ID =", len(sa - sb), list(sa - sb)[:3])
print("当前新增的 ID =", len(sb - sa), list(sb - sa)[:3])
print("→ ID 集合一致 =", sa == sb)
# 原始里的转义形式长什么样
m = re.search(r".{20}DISPIMG.{60}", a)
print("\n原始 sheet1.xml 中该公式的样式:", m.group(0) if m else "未找到")
m2 = re.search(r".{20}DISPIMG.{60}", b)
print("当前 sheet1.xml 中该公式的样式:", m2.group(0) if m2 else "未找到")
