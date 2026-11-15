# -*- coding: utf-8 -*-
"""学习映射（Learned Value Map）—— 枚举映射表的**正确来源**。

设计（2026-10-08 用户口径）
===============================================================================
枚举映射表**可以存在**，但它的来源必须是**标准化跑完后自动生成**，
而不是人手写；生成后供**下一次同类目运行复用**。

于是链路变成两段：

    第一次（冷启动）
        converge_mechanical() → build_classify_payload() → **LLM 语义归类**
        → apply_classification() 落地
        → **把「原值 → 受控值」沉淀到 `<cat>.learned-map.json`**

    第二次起（复用）
        读 `<cat>.learned-map.json` → **直接确定性套用**，这些值**不再调 LLM**
        → 只有**没见过的新值**才再走 LLM 归类，并继续沉淀

这样既「自动」（不靠人手写表），又「省」（重复值零 LLM 调用），
且换类目无需改代码 —— 每个类目自己长出自己的映射表。

文件形状（自描述，便于人工审计与回滚）::

    {
      "_说明": "...",
      "cat": "Cutting Boards-US-202608",
      "版本": 1,
      "生成时间": "2026-10-08T16:20:00",
      "属性": {
        "主体材质": {"竹木": "竹制", "柚木": "木制", ...},
        ...
      },
      "统计": {"主体材质": {"条数": 14, "首次生成轮次": "..."}}
    }
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Mapping

__all__ = ["MAP_SUFFIX", "load_learned", "save_learned", "merge_learned",
           "apply_learned", "learned_path"]

MAP_SUFFIX = ".learned-map.json"
_EMPTY = ("", "未明确", "未标注", "不适用", "未知", "待确认", "None", "none")


def learned_path(cat_dir: str, cat_id: str) -> str:
    return os.path.join(str(cat_dir), "%s%s" % (cat_id, MAP_SUFFIX))


def load_learned(cat_dir: str, cat_id: str) -> dict:
    """读学习映射；不存在/损坏时返回 `{}`（**绝不因它中断流水线**）。"""
    p = learned_path(cat_dir, cat_id)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as fh:
            o = json.load(fh)
    except (OSError, ValueError):
        return {}
    attrs = o.get("属性") if isinstance(o, Mapping) else None
    if not isinstance(attrs, Mapping):
        return {}
    out: dict[str, dict[str, str]] = {}
    for a, m in attrs.items():
        if isinstance(m, Mapping):
            out[str(a)] = {str(k): str(v) for k, v in m.items()
                           if str(k).strip() and str(v).strip()}
    return out


def save_learned(cat_dir: str, cat_id: str, mapping: Mapping[str, Mapping[str, str]],
                 cat: str = "") -> str:
    """原子写学习映射（先写 .tmp 再 replace），返回路径。"""
    p = learned_path(cat_dir, cat_id)
    payload = {
        "_说明": ("由标准化自动生成：原值 → 受控值。**不要手写**。"
                  "每次 run 会合并新值；下次同类目运行直接复用，不再调用 LLM。"),
        "cat": cat or cat_id,
        "版本": 1,
        "生成时间": datetime.now().isoformat(timespec="seconds"),
        "属性": {a: dict(sorted(m.items())) for a, m in sorted(mapping.items()) if m},
        "统计": {a: {"条数": len(m)} for a, m in sorted(mapping.items()) if m},
    }
    tmp = p + ".tmp"
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def merge_learned(old: Mapping[str, Mapping[str, str]],
                  new: Mapping[str, Mapping[str, str]],
                  allowed: Mapping[str, Any] | None = None) -> dict:
    """把本轮新学到的映射并进旧表。

    `allowed`：`{属性: [受控值…]}` —— 目标值**不在当前词表内的一律不沉淀**
    （避免把模型偶发的"新增: X"或已废弃的值学进表里，下次照抄即产出非法值；
     这正是旧 `<cat>.dict.json` 的 `mapping` 被废弃的原因）。
    """
    out: dict[str, dict[str, str]] = {a: dict(m) for a, m in (old or {}).items()}
    for a, m in (new or {}).items():
        ok = {str(v).strip() for v in ((allowed or {}).get(a) or [])}
        tgt = out.setdefault(a, {})
        for k, v in (m or {}).items():
            k, v = str(k).strip(), str(v).strip()
            if not k or not v or k == v:
                continue
            if ok and v not in ok:
                continue                     # 目标不在词表内 → 不沉淀
            tgt[k] = v
        if not tgt:
            out.pop(a, None)
    return out


def apply_learned(std: dict, learned: Mapping[str, Mapping[str, str]],
                  cells: Mapping[str, Any] | None = None,
                  allowed: Mapping[str, Any] | None = None) -> tuple[dict, int]:
    """用学习映射**确定性**填 `std`，返回 `(std, 命中格数)`。

    与 LLM 归类的关系：这里只负责**已经见过**的值；没见过的仍会进
    `build_classify_payload` 交给 LLM。所以这是"省一次算力"，不是"替代归类"。
    """
    n = 0
    for attr, table in (learned or {}).items():
        if not table:
            continue
        ok = {str(v).strip() for v in ((allowed or {}).get(attr) or [])}
        raws: dict = {}
        for k, d in (cells or {}).items():
            rid_s, _, path = str(k).partition("|")
            if path != "attr." + attr or not rid_s.isdigit() or not isinstance(d, Mapping):
                continue
            v = str(d.get("mode") or "").strip()
            if not v:
                for c in (d.get("values") or []):
                    if isinstance(c, Mapping) and str(c.get("value") or "").strip():
                        v = str(c["value"]).strip()
                        break
            if v:
                raws[int(rid_s)] = v
        for (rid, a) in list(std):
            if a == attr and rid not in raws:
                raws[rid] = str(std.get((rid, a)) or "")
        for rid, raw in raws.items():
            tgt = table.get(raw)
            if not tgt:
                continue
            if ok and tgt not in ok:
                continue                     # 学到的目标已被词表淘汰 → 不用
            if std.get((rid, attr)) != tgt:
                std[(rid, attr)] = tgt
                n += 1
    return std, n
