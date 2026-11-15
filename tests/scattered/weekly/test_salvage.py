# -*- coding: utf-8 -*-
"""验证：被截断的 JSON 能救回完整行。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from core.llm import extract_json  # noqa: E402

trunc = ('{"rows":[{"xl_row":1,"attributes":{"a":"x"}},'
         '{"xl_row":2,"attributes":{"a":"y"}},'
         '{"xl_row":3,"attrib')
r = extract_json(trunc)
print("截断文本 → 救回行数:", len(r["rows"]), r["rows"])

print("完整 JSON 仍正常:", extract_json('{"rows":[{"xl_row":1}]}'))

# 带字符串里的花括号（真实依据文本里常有）
trunc2 = ('{"rows":[{"xl_row":1,"依据":"含 {括号} 的文本"},'
          '{"xl_row":2,"依据":"被截')
r2 = extract_json(trunc2)
print("含花括号字符串 → 救回行数:", len(r2["rows"]), r2["rows"])

try:
    extract_json("完全不是JSON")
except Exception as exc:  # noqa: BLE001
    print("非 JSON 仍报错:", str(exc)[:40])
