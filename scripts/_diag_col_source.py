# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

comp = pd.read_excel("data/Cutting-Boards/13_查竞品下载/合并-13_查竞品下载.xlsx", dtype=str, nrows=1)
traf = pd.read_excel("data/Cutting-Boards/11_查流量来源/合并-11_查流量来源.xlsx", dtype=str, nrows=1)
detail = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str, nrows=1)

cols = ["品牌", "月销量", "评分", "商品标题", "流量词数", "配送方式", "上架时间", "来源文件"]
print(f"{'列':8} | {'明细(10)':8} | {'竞品(13)':14} | {'流量(11)':14}")
for c in cols:
    print(f"{c:8} | {('有' if c in detail.columns else '-'):8} | {('有' if c in comp.columns else '-'):14} | {('有' if c in traf.columns else '-') :14}")
print("\n竞品表含'配送'相关列:", [x for x in comp.columns if '配送' in str(x) or 'fulfill' in str(x).lower()])
print("竞品表含'上架'相关列:", [x for x in comp.columns if '上架' in str(x) or 'list' in str(x).lower()])
print("竞品表含'流量词'列:", [x for x in comp.columns if '流量词' in str(x)])
print("竞品表含'商品标题':", [x for x in comp.columns if '商品标题' in str(x) or '标题' in str(x)])
print("竞品表含'评分':", [x for x in comp.columns if '评分' in str(x)])
print("竞品表含'月销量':", [x for x in comp.columns if '月销' in str(x)])
print("竞品表含'品牌':", [x for x in comp.columns if '品牌' in str(x)])
print("流量表含'来源文件'标记(在合并时插入):", '来源文件' in traf.columns)
