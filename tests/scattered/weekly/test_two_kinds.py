# -*- coding: utf-8 -*-
"""验证两类计划：归类（改值）/ 重判（LLM 参与）。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import plan_action  # noqa: E402
from tools.attr_stability.step18_workspace import is_fill_unknown  # noqa: E402


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def chat_json(self, prompt, **kw):
        self.prompt = prompt
        return self.payload


rows = [{"id": "1", "attributes": {a: {"value": "不适用"} for a in
                                   ("是否带漏斗", "是否可调节粗细")}}]
attrs = list(rows[0]["attributes"])

cases = [
    ("归类：不适用→否",
     {"kind": "归类", "mappings": {"是否带漏斗": {"不适用": "否"},
                                  "是否可调节粗细": {"不适用": "否"}}}),
    ("归类：空值→未明确",
     {"kind": "归类", "mappings": {"是否带漏斗": {"": "未明确"}}}),
    ("重判：细分材质", {"kind": "重判"}),
    ("重判 + 附带改值",
     {"kind": "重判", "mappings": {"是否带漏斗": {"不适用": "否"}}}),
    ("旧格式兼容 merge", {"action": "merge",
                      "mappings": {"是否带漏斗": {"无漏斗": "否"}}}),
    ("旧格式兼容 assign", {"action": "assign",
                       "assign": {"是否带漏斗": {"to": "否", "from": ["不适用"]}}}),
]
for label, payload in cases:
    try:
        print("%-18s → %s" % (label, plan_action(FakeLLM(payload), "要求", attrs, rows, {})))
    except ValueError as exc:
        print("%-18s → 拒绝：%s" % (label, str(exc)[:36]))

# 未明确不能直接归并成具体值（在 propose 里拦，这里看提示词口径）
llm = FakeLLM({"kind": "归类", "mappings": {"是否带漏斗": {"": "否"}}})
plan_action(llm, "把空值改成否", attrs, rows, {})
print("\n提示词含两类定义:", "【归类】" in llm.prompt and "【重判】" in llm.prompt)
print("提示词含跨属性/异常值:", "跨属性一致性" in llm.prompt and "异常值" in llm.prompt)
print("双要求走快路径?", is_fill_unknown("空值和未明确的值重新判定，不适用的值改为否"))
