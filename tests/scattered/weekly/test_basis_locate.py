# -*- coding: utf-8 -*-
"""验证：依据只要在任一字段出现过就采信，并自动纠正来源字段。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _locate_basis, _parse  # noqa: E402

inputs = {
    "标题": "Electric Salt and Pepper Grinder Set, USB Rechargeable",
    "五点描述": "Two grinders included. Twist the top to adjust coarseness.",
    "详细参数": "Power Source: Battery Powered; Material: Stainless Steel",
}

cases = [
    ("原句在五点描述", "Twist the top to adjust coarseness"),
    ("省略号跨段", "Electric Salt and Pepper Grinder...USB Rechargeable"),
    ("来源填错（其实在详细参数）", "Power Source: Battery Powered"),
    ("改写/翻译（对不上）", "电池供电，可充电"),
    ("空依据", ""),
]
for label, basis in cases:
    print("%-22s → 命中字段: %r" % (label, _locate_basis(basis, inputs)))

rows = [{"id": "150", "input": inputs, "attributes": {"供电方式": {"value": "未明确"}}}]
raw = {"rows": [{"row_id": "150", "attributes": {"供电方式": {
    "value": "电池式", "source": "标题", "basis": "Power Source: Battery Powered"}}}]}
got, warn = _parse(raw, rows, ["供电方式"])
print("\n来源填错时:", got.get(("150", "供电方式")), "| 告警", len(warn))
