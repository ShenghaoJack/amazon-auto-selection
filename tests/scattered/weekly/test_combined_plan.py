# -*- coding: utf-8 -*-
"""验证：一条要求里同时「改固定值」+「按证据重判」时，模型能给出组合计划。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import plan_action  # noqa: E402
from tools.attr_stability.step18_workspace import is_fill_unknown  # noqa: E402


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, *a, **kw):
        return self.payload


rows = [{"id": "1", "attributes": {a: {"value": ""} for a in
                                   ("是否带漏斗", "是否可调节粗细")}}]
attrs = list(rows[0]["attributes"])
req = "空值和未明确的值重新判定，不适用的值改为否"

# ① 模型给出组合计划：reannotate + assign
combo = {"action": "reannotate",
         "assign": {"是否带漏斗": {"to": "否", "from": ["不适用"]},
                    "是否可调节粗细": {"to": "否", "from": ["不适用"]}}}
print("① 组合计划 →", plan_action(FakeLLM(combo), req, attrs, rows, {}))

# ② 只有 assign
pure = {"action": "assign", "assign": {"是否带漏斗": {"to": "未明确", "from": ["", "未明确"]}}}
print("② 纯 assign →", plan_action(FakeLLM(pure), "把空值改成未明确", attrs, rows, {}))

# ③ 只有 reannotate
print("③ 纯 reannotate →", plan_action(FakeLLM({"action": "reannotate"}), req, attrs, rows, {}))

# ④ 组合的 assign 越界 → 拒绝
bad = {"action": "reannotate", "assign": {"材质": {"to": "否"}}}
try:
    plan_action(FakeLLM(bad), req, attrs, rows, {})
except ValueError as exc:
    print("④ 越界 assign →", str(exc)[:30])

# ⑤ 确定性快路径不该接住这种双要求
print("⑤ 双要求走快路径?", is_fill_unknown(req))
print("   单要求走快路径?", is_fill_unknown("把空值改成未明确"))
