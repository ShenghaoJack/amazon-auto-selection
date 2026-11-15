# -*- coding: utf-8 -*-
"""验证：标题没提到漏斗不能判「否」。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _parse, _weak_negative  # noqa: E402

title = ("Gravity Electric Salt and Pepper Grinder Set with Rechargeable Base, "
         "2-Speed Grinding Control Stainless Steel Pepper Mill Set with "
         "Adjustable Coarseness, LED Light & 176ml Large Capacity")
rows = [{"id": "233", "input": {"标题": title,
                                "五点描述": "No funnel included. 不含漏斗，直接倒入瓶口。",
                                "详细参数": ""},
         "attributes": {"是否带漏斗": {"value": ""}}}]

cases = [
    ("④ 标题原文当依据（用户遇到的问题）",
     {"value": "否", "source": "标题", "basis": title}),
    ("⑤ 明确否定：No funnel included",
     {"value": "否", "source": "五点描述", "basis": "No funnel included"}),
    ("⑥ 明确否定：不含漏斗",
     {"value": "否", "source": "五点描述", "basis": "不含漏斗，直接倒入瓶口"}),
    ("⑦ 判「是」不受此限",
     {"value": "是", "source": "标题", "basis": title}),
]
for label, cell in cases:
    raw = {"rows": [{"row_id": "233", "attributes": {"是否带漏斗": cell}}]}
    got, warn = _parse(raw, [dict(rows[0])], ["是否带漏斗"])
    print("%-28s → 采用 %d 格 | 告警: %s"
          % (label, len(got), warn[0]["reason"][:36] if warn else "无"))
