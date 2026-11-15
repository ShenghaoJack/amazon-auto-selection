# -*- coding: utf-8 -*-
"""删行前的安全检查：超链接所在单元格、合并单元格、是否在目标行之下。"""
from __future__ import annotations

import os

import openpyxl

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
BASE = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, BASE)

wb = openpyxl.load_workbook(P)
ws = wb["Sheet1"]
print("目标行 = 180；max_row =", ws.max_row)
links = [(c.coordinate, c.hyperlink.target if c.hyperlink else None)
         for row in ws.iter_rows() for c in row if c.hyperlink]
print("超链接单元格数 =", len(links))
below = [(co, t) for co, t in links if int("".join(ch for ch in co if ch.isdigit())) > 180]
print("位于第 180 行**之下**的超链接 =", len(below))
for x in below[:6]:
    print("   ", x[0], str(x[1])[:70])
print("合并单元格 =", len(ws.merged_cells.ranges), list(ws.merged_cells.ranges)[:5])
print("数据验证 =", len(ws.data_validations.dataValidation))
print("条件格式 =", len(list(ws.conditional_formatting)))
print("图片对象(wb/ws level) =", len(getattr(ws, "_images", [])), len(getattr(wb, "_images", [])))
print("图表 =", len(getattr(ws, "_charts", [])))
print("autoFilter =", ws.auto_filter.ref)
wb.close()
