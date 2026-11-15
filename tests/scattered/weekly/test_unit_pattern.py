# -*- coding: utf-8 -*-
"""验证数值属性的"数字+单位"兜底：功率 hint 现在能命中高价段。"""
import sys, re
sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
from steps.step16_attribute_annotation import _hint_keywords, _unit_pattern
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")

hint = "本属性统一用 W。从五点/详细参数找 Amps / 电流 / Wattage / 功率；只给安培的按 电压120V 换算 W=A×120。"
print("关键词:", _hint_keywords(hint))
pat = _unit_pattern(hint)
print("unit_pattern:", pat.pattern if pat else None)

SPLIT = r"D:\deepseek工作区\auto-workflow-python\data\Dehumidifiers-US-202609\14_流量拆分\流量拆分数据-需人工补充.xlsx"
df = pd.read_excel(SPLIT, sheet_name="AI已补充")
prices = pd.to_numeric(df["价格($)"].astype(str).str.replace(r"^[$€£]\s*", "", regex=True), errors="coerce")
df = df.assign(__p=prices)
text_cols = [c for c in ("标题", "五点描述", "详细参数") if c in df.columns]
blob = df[text_cols].astype(str).agg(" ".join, axis=1).str.lower()
hi = blob[(df["__p"] >= 280) & (df["__p"] < 3499)]
lo = blob[((df["__p"] >= 6) & (df["__p"] < 18)) | ((df["__p"] >= 18) & (df["__p"] < 24))]
print("高价段:", len(hi), "行, unit 命中:", int(hi.apply(lambda t: pat.search(t) is not None).sum()))
print("低价段:", len(lo), "行, unit 命中:", int(lo.apply(lambda t: pat.search(t) is not None).sum()))
print("高价覆盖率:", round(int(hi.apply(lambda t: pat.search(t) is not None).sum()) / max(1, len(hi)), 3))
