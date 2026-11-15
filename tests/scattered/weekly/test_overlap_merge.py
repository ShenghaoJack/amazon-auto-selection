# -*- coding: utf-8 -*-
"""验证新增的证据重叠 LLM 判定链路：prompt 可生成、merge 应用正确。"""
import json, sys
sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
from steps.step16_attribute_annotation import (
    _evidence_overlap_pairs, _build_overlap_judge_prompt, _apply_overlap_merges,
)

f = r"D:\deepseek工作区\auto-workflow-python\data\Dehumidifiers-US-202609\Dehumidifiers-US-202609-attribute-spec.json"
attrs = json.load(open(f, encoding="utf-8-sig"))["attributes"]
sys.stdout.reconfigure(encoding="utf-8")

pairs = _evidence_overlap_pairs(attrs, min_shared=1, min_len=5, max_df=2)
print("检出重叠组数:", len(pairs))

by_name = {str(a.get("name") or ""): a for a in attrs}
prompt = _build_overlap_judge_prompt(pairs, by_name)
print("prompt 长度:", len(prompt), "字符")
print("prompt 含'待判断的属性对':", "待判断的属性对" in prompt)
print()

# 模拟 LLM 返回：第1组(断电记忆重启↔自动重启) merge，其余 keep_both
decisions = []
for a, b, shared in pairs:
    if (a, b) == ("是否带断电记忆重启", "是否带自动重启") or (a, b) == ("是否带自动重启", "是否带断电记忆重启"):
        decisions.append({"a": a, "b": b, "action": "merge", "keep": "是否带自动重启", "reason": "同一功能"})
    else:
        decisions.append({"a": a, "b": b, "action": "keep_both", "reason": "边界"})

new_attrs, n = _apply_overlap_merges(attrs, decisions)
print("合并组数:", n)
print("属性数: %d -> %d" % (len(attrs), len(new_attrs)))
names = [str(a.get("name")) for a in new_attrs]
print("是否带自动重启 在:", "是否带自动重启" in names)
print("是否带断电记忆重启 在(应为False):", "是否带断电记忆重启" in names)
