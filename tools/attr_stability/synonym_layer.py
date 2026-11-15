# -*- coding: utf-8 -*-
"""全局同义词层(2026-10-21): 词库 + 自动挖出的规则 + 名称归一。

设计目标: **零人工维护**的同义归一 —— 不再靠往代码里手写 `if 支持X → 带X`。

三层协同:
  ① 全局词库 `_catalog/_synonyms.json`      —— 确认属性集时自动写入(规范名 + 别名)
  ② 规则库   `_catalog/_synonym_rules.json` —— 从词库的(规范名, 别名)对里**自动挖**
  ③ LLM 归并(调用方)                          —— 前两层没覆盖的新概念仍交给 LLM

作用域: **全局**(跨类目共享, 学得更快)。文件放在 `data/_catalog/` 下, 与各类目子目录并列。
"""
from __future__ import annotations

import difflib
import json
import os

SYNONYMS_FILE = "_synonyms.json"
RULES_FILE = "_synonym_rules.json"

# 只保留"通用中文构词"的挖掘结果 —— 这些是机械变体, 换类目也成立。
# 太短/太长的差异不成规则(容易误伤), 见 `_acceptable_rule`。
MIN_SUPPORT = 3          # 同一规则至少被这么多个(规范名, 别名)对支持, 才写进规则库
_MAX_DIFF_LEN = 6        # 差异片段最长字数(超过更像语义改写, 不适合当词法规则)


def shared_dir(cat_root) -> str:
    """`data/<类目>` → `data/_catalog`(全局共享层)。"""
    return os.path.join(os.path.dirname(os.path.abspath(str(cat_root))), "_catalog")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def load_layer(cat_root) -> dict:
    """读词库 + 规则库。缺失/损坏一律退化为空(不阻断步骤16)。"""
    d = shared_dir(cat_root)
    return {
        "synonyms": _read_json(os.path.join(d, SYNONYMS_FILE)),
        "rules": _read_json(os.path.join(d, RULES_FILE)),
    }


def _alias_index(synonyms: dict) -> dict:
    """别名 → 规范名(展开词库, 便于 O(1) 命中)。"""
    idx: dict[str, str] = {}
    for canonical, info in (synonyms or {}).items():
        if not isinstance(info, dict):
            continue
        for al in (info.get("别名") or []):
            al = str(al).strip()
            if al and al != canonical:
                idx[al] = canonical
    return idx


def canonical_name(name: str, layer: dict | None) -> str:
    """把名字归一到已确认的规范名。

    优先级: ① 词库精确命中(别名→规范名) ② 规则库按长度降序套用 ③ 原样返回。
    """
    s = str(name or "").strip()
    if not s or not layer:
        return s
    hit = _alias_index(layer.get("synonyms")).get(s)
    if hit:
        return hit
    rules = layer.get("rules") or {}
    for src in sorted(rules, key=len, reverse=True):
        dst = rules.get(src)
        if isinstance(src, str) and isinstance(dst, str) and src and src in s:
            return s.replace(src, dst)
    return s


def _diff_pairs(canonical: str, alias: str):
    """对齐两个名字, 产出 (别名片段, 规范名片段) 的替换对。"""
    sm = difflib.SequenceMatcher(None, alias, canonical)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = alias[i1:i2], canonical[j1:j2]
        if a and b:
            yield a, b


def _acceptable_rule(src: str, dst: str) -> bool:
    """只接受"短、纯中文、非空"的机械替换, 避免把语义改写误当词法规则。"""
    if not src or not dst or src == dst:
        return False
    if len(src) > _MAX_DIFF_LEN or len(dst) > _MAX_DIFF_LEN:
        return False
    return all("\u4e00" <= ch <= "\u9fff" for ch in src + dst)


def mine_rules(synonyms: dict, min_support: int = MIN_SUPPORT) -> dict:
    """从词库的(规范名, 别名)对里挖通用替换规则。返回并按支持度过滤。"""
    counts: dict[tuple, int] = {}
    for canonical, info in (synonyms or {}).items():
        if not isinstance(info, dict):
            continue
        for al in (info.get("别名") or []):
            al = str(al).strip()
            if not al or al == canonical:
                continue
            for src, dst in _diff_pairs(str(canonical), al):
                if _acceptable_rule(src, dst):
                    counts[(src, dst)] = counts.get((src, dst), 0) + 1
    return {src: dst for (src, dst), n in counts.items() if n >= min_support}


def record_spec(cat_root, cat: str, attributes: list, *, now: str = "") -> dict:
    """确认属性集后调用: 把(规范名 + 别名)沉淀进全局词库, 并重挖规则。

    只增不改: 已存在的规范名保留其历史别名; 出现次数累加。
    永不抛异常(沉淀失败不能影响确认动作本身)。
    """
    try:
        d = shared_dir(cat_root)
        syn_path = os.path.join(d, SYNONYMS_FILE)
        synonyms = _read_json(syn_path)
        changed = 0
        for a in (attributes or []):
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip()
            if not name:
                continue
            entry = synonyms.setdefault(name, {"别名": [], "出现次数": 0})
            aliases = entry.setdefault("别名", [])
            for al in (a.get("aliases") or []):
                al = str(al).strip()
                if al and al != name and al not in aliases:
                    aliases.append(al)
                    changed += 1
            entry["出现次数"] = int(entry.get("出现次数") or 0) + 1
            entry["来源"] = cat
            if now:
                entry["更新"] = now
        if not synonyms:
            return {"ok": False, "reason": "empty"}
        _write_json(syn_path, synonyms)
        rules = mine_rules(synonyms)
        _write_json(os.path.join(d, RULES_FILE), rules)
        return {"ok": True, "规范名": len(synonyms), "别名新增": changed, "规则": len(rules)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
