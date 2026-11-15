# -*- coding: utf-8 -*-
"""验证 step18 拆半重试的触发条件与单行兜底。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_model import _SPLIT_HINT, _annotate_batch  # noqa: E402

cases = [
    '无法解析 LLM JSON 输出: {"rows":[{"row_id":"2","attributes"',
    "模型漏掉商品，未保存任何修改，请重新预览",
    "Connection timeout",
]
for c in cases:
    print("拆半?", bool(_SPLIT_HINT.search(c)), "|", c[:44])


class BoomLLM:
    calls = 0

    def chat_json(self, *a, **kw):
        BoomLLM.calls += 1
        raise ValueError("无法解析 LLM JSON 输出: {\"rows\":[{\"row_id\":")


rows = [{"id": str(i), "input": {}, "attributes": {"A": {"value": "x"}}} for i in range(4)]
got, warn = _annotate_batch(BoomLLM(), "要求", ["A"], rows, {"attributes": [], "definitions": {}})
print("\n拆到单行仍失败 → 更新", len(got), "条，告警", len(warn), "条；总调用", BoomLLM.calls, "次")
print("告警样例:", warn[0]["reason"][:50] if warn else "-")
