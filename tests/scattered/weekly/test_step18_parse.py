# -*- coding: utf-8 -*-
"""验证：模型漏行/漏属性时不再整批作废。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _parse  # noqa: E402

rows = [
    {"id": "a", "input": {"五点描述": "has a funnel and easy adjust"},
     "attributes": {"是否带漏斗": {"value": "未明确"}, "是否可调节粗细": {"value": "未明确"}}},
    {"id": "b", "input": {"五点描述": "twist to adjust"},
     "attributes": {"是否带漏斗": {"value": "未明确"}, "是否可调节粗细": {"value": "未明确"}}},
]
attrs = ["是否带漏斗", "是否可调节粗细"]

# ① 正常：两行都回
raw_ok = {"rows": [
    {"row_id": "a", "attributes": {"是否带漏斗": {"value": "是", "source": "五点描述", "basis": "has a funnel"},
                                  "是否可调节粗细": {"value": "是", "source": "五点描述", "basis": "easy adjust"}}},
    {"row_id": "b", "attributes": {"是否带漏斗": {"value": "否", "source": "五点描述", "basis": "twist to adjust"},
                                  "是否可调节粗细": {"value": "是", "source": "五点描述", "basis": "twist to adjust"}}},
]}
got, warn = _parse(raw_ok, rows, attrs)
print("① 完整返回 → 更新 %d 格，告警 %d" % (len(got), len(warn)))

# ② 漏一行 + 某行漏一个属性（用户遇到的报错）
raw_bad = {"rows": [
    {"row_id": "a", "attributes": {"是否带漏斗": {"value": "是", "source": "五点描述", "basis": "has a funnel"}}},
]}
got, warn = _parse(raw_bad, rows, attrs)
print("② 漏行漏属性 → 更新 %d 格，告警 %d 条" % (len(got), len(warn)))
for w in warn[:3]:
    print("     ", w["row_id"], w["attribute"], w["reason"][:30])

# ③ 越界行号
raw_out = {"rows": [{"row_id": "zzz", "attributes": {"是否带漏斗": {"value": "是"}}}]}
try:
    _parse(raw_out, rows, attrs)
except ValueError as exc:
    print("③ 完全无可用行 → 仍报错:", str(exc)[:34])
