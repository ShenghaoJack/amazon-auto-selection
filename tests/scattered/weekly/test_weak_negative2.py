# -*- coding: utf-8 -*-
"""验证：标题没提到 → 未明确；判否必须有明确否定表述。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _parse  # noqa: E402

title = ("Gravity Electric Salt and Pepper Grinder Set with Rechargeable Base, "
         "2-Speed Grinding Control Stainless Steel Pepper Mill Set")
desc = "EFFORTLESS MILLS — With a single touch the mills light up and grind. No funnel included."
rows = [{"id": "233", "input": {"标题": title, "五点描述": desc, "详细参数": "Material: Steel"},
         "attributes": {"是否带漏斗": {"value": ""}}}]

cases = [
    ("标题原文判否（用户遇到）", {"value": "否", "source": "标题", "basis": title}),
    ("五点里有 No funnel", {"value": "否", "source": "五点描述", "basis": "No funnel included"}),
    ("标题提到即判是", {"value": "是", "source": "标题", "basis": title}),
]
for label, cell in cases:
    raw = {"rows": [{"row_id": "233", "attributes": {"是否带漏斗": dict(cell)}}]}
    got, warn = _parse(raw, [dict(rows[0])], ["是否带漏斗"])
    val = got.get(("233", "是否带漏斗"), {}).get("value", "（未采用）")
    print("%-22s → 结果 %-6s 告警 %s" % (label, val, "有" if warn else "无"))
