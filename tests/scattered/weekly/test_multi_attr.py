# -*- coding: utf-8 -*-
"""多属性同时处理的可行性检查：批大小、assign 多属性、merge 限制。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import plan_action  # noqa: E402
from tools.attr_stability.step18_workspace import is_fill_unknown  # noqa: E402

print("每批行数（属性数 → 行/批）：")
for n in (1, 2, 3, 4, 5, 6, 8, 12, 18, 36):
    print("   %2d 个属性 → %2d 行/批" % (n, max(1, min(12, 36 // n))))


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, *a, **kw):
        return self.payload


rows = [{"id": "1", "attributes": {a: {"value": ""} for a in
                                   ("是否带漏斗", "是否可调节粗细", "是否可观察余量")}}]
attrs = list(rows[0]["attributes"])

# ① assign 同时给多个属性不同目标值
plan = {"action": "assign", "assign": {
    "是否带漏斗": {"to": "未明确", "from": ["", "未明确"]},
    "是否可调节粗细": {"to": "否"},
    "是否可观察余量": {"to": "否"},
}}
print("\n① 多属性 assign →", plan_action(FakeLLM(plan), "把空值改成未明确，没有该功能的改成否",
                                        attrs, rows, {}))

# ② 跨属性归并会被改写为 reannotate
print("② 多属性 merge →", plan_action(FakeLLM({"action": "merge",
                                                "mappings": {"是否带漏斗": {"x": "y"}}}),
                                       "把两个值合并", attrs, rows, {}))

# ③ 确定性快路径的识别（多条要求混在一起时不会误触发）
for text in ("把空值改成未明确", "把空值改成未明确，没有该功能的改成否"):
    print("③ 确定性快路径 %-24s → %s" % (text, is_fill_unknown(text)))
