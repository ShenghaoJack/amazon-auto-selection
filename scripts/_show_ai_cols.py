# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ai = pd.read_excel("data/Cutting-Boards/14_流量拆分/流量拆分数据-需人工补充.xlsx", sheet_name="AI已补充", dtype=str, nrows=1)
cols = list(ai.columns)
print("AI列数:", len(cols))
raw = pd.read_excel("data/Cutting-Boards/14_流量拆分/流量拆分数据-需人工补充.xlsx", sheet_name="原始表", dtype=str, nrows=1)
print("原始表列数:", len(raw.columns))
print("已删列(应0):", [c for c in ["来源文件","是否成功","状态","抓取时间"] if c in cols])
print()
# 分段展示
core = ["父ASIN","变体ASIN","标题","五点描述","图片链接","来源ASIN"]
# 找竞品块结束: 评分/月销量属竞品? 简单按已知: 竞品特征列起始 SKU;流量特征列起始 自然搜索词/流量来源
print("明细核心列:")
for c in cols:
    if c in core:
        print("  ", c)
comp_start = cols.index("SKU") if "SKU" in cols else None
# 竞品块 = core 之后 到 流量列前(自然搜索词/流量来源)
marker = None
for m in ["自然搜索词", "流量来源"]:
    if m in cols:
        marker = m
        break
print("明细核心之后、竞品块(到流量列前):")
if comp_start is not None and marker is not None:
    seg = cols[cols.index("来源ASIN")+1 if "来源ASIN" in cols else 6 : cols.index(marker)]
    print("   ", " | ".join(seg))
    print("\n流量列块:")
    seg2 = cols[cols.index(marker):]
    # 流量列到计算列前: 计算列=流量占比/变体月销量/变体月销额/新老品/主图
    cut = len(seg2)
    for cc in ["流量占比","变体月销量","变体月销额","新老品","主图"]:
        if cc in seg2 and seg2.index(cc) < cut:
            cut = seg2.index(cc)
    print("   ", " | ".join(seg2[:cut]))
    print("\n计算列(尾部):", " | ".join(seg2[cut:]))
else:
    print("   marker/comp_start 定位失败")
