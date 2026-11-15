# -*- coding: utf-8 -*-
"""验证 assign 动作：模型判断意图 → 代码确定性改值（不再判值）。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import plan_action  # noqa: E402


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def chat_json(self, prompt, **kw):
        self.prompt = prompt
        return self.payload


rows = [{"id": "150", "attributes": {"供电方式": {"value": ""}}}]
attrs = ["供电方式"]

# ① 模型判断为 assign
llm = FakeLLM({"action": "assign", "assign": {"供电方式": {"to": "未明确", "from": ["", "未明确"]}}})
print("① assign →", plan_action(llm, "将空值改成未明确", attrs, rows, {}))
print("   提示词里含 assign 说明:", "assign" in llm.prompt)

# ② 模型判断为 reannotate（要判值）
llm2 = FakeLLM({"action": "reannotate"})
print("② reannotate →", plan_action(llm2, "细分这些商品的具体材质", attrs, rows, {}))

# ③ assign 缺少目标值 → 拒绝
try:
    plan_action(FakeLLM({"action": "assign", "assign": {"供电方式": {}}}), "改一下", attrs, rows, {})
except ValueError as exc:
    print("③ 缺目标值 →", str(exc)[:32])

# ④ 越界属性 → 拒绝
try:
    plan_action(FakeLLM({"action": "assign", "assign": {"材质": {"to": "塑料"}}}), "改一下", attrs, rows, {})
except ValueError as exc:
    print("④ 越界属性 →", str(exc)[:32])
