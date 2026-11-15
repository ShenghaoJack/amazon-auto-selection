# -*- coding: utf-8 -*-
"""验证: 是否X + 非布尔X 合并时，强制保留非布尔那个。"""
import sys
sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
from steps.step16_attribute_annotation import _apply_overlap_merges, _build_overlap_judge_prompt
sys.stdout.reconfigure(encoding="utf-8")

attrs = [
    {"name": "是否带香氛", "hint": "见Lavender写是，Unscented写否；是/否/未明确"},
    {"name": "香型", "hint": "如薰衣草/香草/无香"},
    {"name": "是否带水泵", "hint": "见Built-In Pump写是；是/否/未明确"},
    {"name": "排水方式", "hint": "如连续排水管/水箱集水/水泵上排"},
]
# LLM 错误地选了「是否」那个当 keep
decisions = [
    {"a": "是否带香氛", "b": "香型", "action": "merge", "keep": "是否带香氛"},
    {"a": "是否带水泵", "b": "排水方式", "action": "merge", "keep": "是否带水泵"},
]
out, n = _apply_overlap_merges(attrs, decisions)
names = [a["name"] for a in out]
print("合并组数:", n)
print("保留的属性:", names)
ok = ("香型" in names and "排水方式" in names
      and "是否带香氛" not in names and "是否带水泵" not in names)
print("是否X 被正确改成 非是否X 保留:", ok)

prompt = _build_overlap_judge_prompt(
    [("是否带香氛", "香型", ["fragrance"])],
    {"是否带香氛": attrs[0], "香型": attrs[1]})
print("\nprompt 含『是否 vs 非是否』规则:", "是否 vs 非是否" in prompt)
print("prompt 含『keep 一律选不含』:", "keep" in prompt and "不含「是否/有无」" in prompt)
