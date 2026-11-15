# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

t = pd.read_excel("data/Cutting-Boards/11_查流量来源/合并-11_查流量来源.xlsx", dtype=str)
v = t["全部流量词"].fillna("").astype(str)
print("非空行:", int((v.str.strip() != "").sum()), "/", len(t))
lens = v.str.len()
print("长度分布: 中位", int(lens.median()), "最大", int(lens.max()))
# 找几个较长单元格看分隔
big = v[v.str.len() > 50].head(3)
for i, x in big.items():
    print(f"--- len={len(x)} 含换行={x.count(chr(10))} 含空格={x.count(' ')} 含逗号={x.count(',')}")
    print(repr(x[:300]))
# 单词数推断: 若空格分隔
sp = v[v.str.contains(" ", regex=False)]
print("含空格行:", len(sp))
nl = v[v.str.contains(chr(10), regex=False)]
print("含换行行:", len(nl))
cm = v[v.str.contains(",", regex=False)]
print("含逗号行:", len(cm))
