# -*- coding: utf-8 -*-
"""验证：依据未通过核对时，告警里带上了值/依据/来源/原文，前端可点开。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _parse  # noqa: E402

rows = [{"id": "150",
         "input": {"标题": "Battery Automatic Salt and Pepper Shaker",
                   "五点描述": "Comes with batteries included. Twist to grind.",
                   "详细参数": "Material: Stainless Steel"},
         "attributes": {"供电方式": {"value": ""}}}]

raw = {"rows": [{"row_id": "150", "attributes": {"供电方式": {
    "value": "干电池", "source": "五点描述",
    "basis": "Batteries Included（电池供电）"}}}]}

got, warn = _parse(raw, rows, ["供电方式"])
print("更新格数:", len(got), "| 告警数:", len(warn))
for w in warn:
    print("  reason     :", w.get("reason"))
    print("  模型值     :", w.get("proposed_value"))
    print("  模型依据   :", w.get("basis"))
    print("  自称来源   :", w.get("claimed_source"))
    print("  该字段原文 :", w.get("source_text"))
