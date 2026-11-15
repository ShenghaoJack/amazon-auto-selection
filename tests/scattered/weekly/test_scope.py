# -*- coding: utf-8 -*-
"""验证 scope：重判不得改动「当前值不在筛选范围内」的格。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _parse  # noqa: E402

rows = [
    {"id": "1", "input": {"标题": "A", "五点描述": "has a funnel", "详细参数": "x"},
     "attributes": {"是否带漏斗": {"value": "是"},      # 不在筛选范围
                    "是否可调节粗细": {"value": ""}}},   # 在筛选范围
]
raw = {"rows": [{"row_id": "1", "attributes": {
    "是否带漏斗": {"value": "否", "source": "五点描述", "basis": "has a funnel"},
    "是否可调节粗细": {"value": "是", "source": "五点描述", "basis": "has a funnel"},
}}]}
got, warn = _parse(raw, rows, ["是否带漏斗", "是否可调节粗细"],
                   scope={"是否带漏斗": {"", "未明确"}, "是否可调节粗细": {"", "未明确"}})
print("被采用的格:", {k[1]: v["value"] for k, v in got.items()})
print("告警:")
for w in warn:
    print("   ", w["row_id"], w["attribute"], "→", w["reason"])
