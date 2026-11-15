# -*- coding: utf-8 -*-
"""视觉返回的**外层剥壳**(2026-09-24)。

`LLMClient.vision()` 走 `extract_json`, 但实测模型有时并不直接回我们要的 JSON, 而是回一层包装:

    {"type": "json_object", "response": {"两面材质是否不同": "否", "主材质": "相思木", ...}}
    {"type": "json_object", "content": "{\\n  \\"两面材质是否不同\\": \\"否\\", ...}"}
    {"type": "json_object"}                       ← 解析失败, 内容已丢

不剥壳就会**静默读不到字段**(实测「主体材质」核实里 13 行被误判成"视觉与现状一致"),
所以统一在这里剥: 逐层找 `response` / `content` / `追问` 等承载键, `content` 是字符串时再 JSON 解析一次。
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

__all__ = ["unwrap_vision"]

# 承载下一层的常见键(按优先级)
_WRAP_KEYS = ("response", "content", "result", "data", "json", "output",
              "追问", "answer", "text")
_MAX_DEPTH = 4


def _first_json_object(text: str) -> Any:
    """从字符串里抠出最外层 JSON 对象(容忍前后有说明文字/代码围栏)。"""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r'^```[A-Za-z]*\s*', '', s)
        s = re.sub(r'\s*```$', '', s)
    for cand in (s, s[s.find("{"):s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        if not cand:
            continue
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            continue
    return None


def unwrap_vision(rec: Any, want: Sequence[str] = ()) -> dict:
    """把视觉返回剥到**含 `want` 字段的那一层**; 剥不到就返回最内层 dict。

    `want`: 我们真正要读的字段名(如 `("两面材质是否不同",)`)。给空则只剥一层包装。
    """
    if not isinstance(rec, Mapping):
        return {}
    cur: Any = dict(rec)
    for _ in range(_MAX_DEPTH):
        if not isinstance(cur, Mapping):
            return {}
        if want and any(k in cur for k in want):
            return dict(cur)
        nxt = None
        for k in _WRAP_KEYS:
            v = cur.get(k)
            if isinstance(v, str):
                parsed = _first_json_object(v)
                if isinstance(parsed, Mapping):
                    nxt = parsed
                    break
            elif isinstance(v, Mapping):
                nxt = v
                break
        if nxt is None:
            return dict(cur)
        cur = nxt
    return dict(cur) if isinstance(cur, Mapping) else {}
