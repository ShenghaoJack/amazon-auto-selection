# -*- coding: utf-8 -*-
"""跨属性一致性检查（声明式）。2026-10-08 新增。

为什么需要它
===============================================================================
实测 Cutting Boards：`主体材质=竹木` 却有 28 行 `产品类型=实木砧板` —— 竹子不是实木，
两者不可能同时成立。但这类错误**蕴含式关系挖不出来**：M9 挖掘确实发现
`竹制砧板 ⇒ 竹木`（P=0.996, lift=3.5），可**反方向不成立**（竹木 也配 实木砧板 28 次），
所以「A 成立则 B 必成立」的规则永远抓不到它。要抓必须用**互斥**：
`主体材质 含「竹」⟹ 产品类型 不得含「实木」`。

设计原则（与关系校验一致）
===============================================================================
  · **只登记，不静默改值** —— 判错了宁可让人看见，也不要悄悄把值改掉；
  · **声明写在 rules.json 里**，不把类目知识藏进代码；
  · 未命中/证据不足（空值、哨兵）不判，避免把「没填」当成「填错」。

rules.json 里的声明形状::

    "cross_checks": {
      "互斥": [
        {"说明": "竹子不是实木",
         "左": {"属性": "主体材质", "含": ["竹"]},
         "右": {"属性": "产品类型", "不得含": ["实木"]}}
      ]
    }
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

__all__ = ["load_cross_checks", "EMPTY_VALUES", "check_row", "check_rows", "rule_applicable",
           "check_parts_dependency",
           "conflicts_report"]

# 这些值表示「没有信息」，不参与判冲突（否则会把"没填"误判成"填错"）
EMPTY_VALUES = frozenset({
    "", "未明确", "未标注", "不适用", "未知", "待确认", "None", "none", "无",
})


def load_cross_checks(rules_obj: Mapping[str, Any] | None) -> dict[str, list[dict]]:
    """从 rules.json（已解析成 dict）里取声明；形状不对的条目丢弃。"""
    if not isinstance(rules_obj, Mapping):
        return {}
    raw = rules_obj.get("cross_checks")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, list[dict]] = {}
    for kind, items in raw.items():
        if str(kind).startswith("_") or not isinstance(items, Sequence):
            continue
        keep = []
        for it in items:
            if not isinstance(it, Mapping):
                continue
            left, right = it.get("左"), it.get("右")
            if not (isinstance(left, Mapping) and isinstance(right, Mapping)):
                continue
            if not (left.get("属性") and right.get("属性")):
                continue
            if not any(left.get(k) for k in ("含", "不得含")):
                continue
            if not any(right.get(k) for k in ("含", "不得含")):
                continue
            keep.append(dict(it))
        if keep:
            out[str(kind)] = keep
    return out


def _tokens(value: Any) -> list[str]:
    """把单元格值拆成可比较的词：[值本身, 以及 `+` 分隔的各分量]。"""
    s = str(value or "").strip()
    if not s or s in EMPTY_VALUES:
        return []
    parts = [p.strip() for p in s.split("+") if p.strip()]
    out = [s]
    out += [p for p in parts if p not in out]
    return out


def _side_ok(cond: Mapping[str, Any], value: Any) -> bool:
    toks = _tokens(value)
    if not toks:
        return False                        # 空/哨兵 → 不参与判定
    words = [str(w) for w in (cond.get("含") or [])]
    if words and not any(any(w in t for t in toks) for w in words):
        return False
    bans = [str(w) for w in (cond.get("不得含") or [])]
    if bans and any(any(w in t for t in toks) for w in bans):
        return False
    return True


def rule_applicable(attrs: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    """两侧有信息且左侧前提成立，才算实际检查过此约束。"""
    left, right = rule["左"], rule["右"]
    lv, rv = attrs.get(str(left["属性"])), attrs.get(str(right["属性"]))
    return bool(_tokens(lv) and _tokens(rv) and _side_ok(left, lv))


def check_row(attrs: Mapping[str, Any],
              decl: Mapping[str, Sequence[Mapping[str, Any]]],
              xl_row: Any = None) -> list[dict]:
    """判一行；返回命中列表（可能为空）。

    ★ 任一侧**没有信息**（空/哨兵）时一律不判 —— 「没填」不等于「填错」。
      缺了这一步会把 `产品类型=未明确` 的每一行都误报成冲突。
    """
    hits: list[dict] = []
    for kind, items in (decl or {}).items():
        for it in items:
            left, right = it["左"], it["右"]
            la, ra = str(left["属性"]), str(right["属性"])
            lv, rv = attrs.get(la), attrs.get(ra)
            if not rule_applicable(attrs, it):
                continue
            # 左侧成立 → 右侧**必须**成立，否则算冲突
            if _side_ok(right, rv):
                continue
            hits.append({
                "类型": str(kind),
                "行号": xl_row,
                "说明": str(it.get("说明") or ""),
                "左属性": la, "左值": str(lv or ""),
                "右属性": ra, "右值": str(rv or ""),
                "判据": "%s=%r 成立，但 %s=%r 违反 %s"
                        % (la, str(lv or ""), ra, str(rv or ""),
                           "、".join("%s%s" % (k, v) for k, v in right.items()
                                     if k != "属性")),
            })
    return hits


def check_rows(deliv: Iterable[Mapping[str, Any]],
               decl: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict]:
    """`deliv` 是 `[{'xl_row':…, 'attributes':{…}}, …]`。"""
    out: list[dict] = []
    for d in deliv or []:
        if not isinstance(d, Mapping):
            continue
        attrs = d.get("attributes")
        if not isinstance(attrs, Mapping):
            continue
        out += check_row(attrs, decl, d.get("xl_row"))
    return out


def check_parts_dependency(deliv: Iterable[Mapping[str, Any]],
                           rules_obj: Mapping[str, Any] | None) -> list[dict]:
    """检查 `params.parts_when` 声明的「**分量数 ↔ 信号属性**」双向一致性。

    用户口径（2026-10-08）：
      · 其他属性表明**多材质**时（如 `是否双面不同材质=是`）→ 主体材质写 **2 个** `A+B`；
      · 没有该信号时 → 主体材质**只写 1 个**。

    声明形状（属性级）::

        "主体材质": {"params": {
            "max_parts": 2,
            "parts_when": [{"属性": "是否双面不同材质", "含": ["是"]}]
        }}

    双向判定（`n_parts` = 值按 `+` 拆出的分量数）：
      · 信号成立 且 n_parts < 2  → 违例「该写 2 个却只写了 1 个」
      · 信号不成立 且 n_parts > 1 → 违例「没有多材质信号却写了 2 个」
      · 信号属性是 空/未明确/哨兵 → **不判**（「没填」不等于「填错」）

    返回违例列表；空 = 通过。
    """
    out: list[dict] = []
    spec_by = {str(a.get("name")): a for a in (rules_obj or {}).get("attributes") or []
               if isinstance(a, Mapping) and a.get("name")}
    cols: set[str] = set()
    for d in deliv or []:
        a0 = d.get("attributes") if isinstance(d, Mapping) else None
        if isinstance(a0, Mapping):
            cols |= {str(k) for k in a0}
    # ★ 信号属性**整列缺失**（比如刚新增属性、交付表还是上一轮的）→ 明确报「未检查」，
    #   不能静默通过。这和"没有交付表就不算通过"是同一条原则。
    for name, a in spec_by.items():
        p = a.get("params") if isinstance(a.get("params"), Mapping) else {}
        conds = p.get("parts_when")
        if not isinstance(conds, Sequence) or isinstance(conds, (str, bytes)):
            continue
        for c in conds:
            sa = str((c or {}).get("属性") or "") if isinstance(c, Mapping) else ""
            if sa and cols and sa not in cols:
                out.append({"未检查": True, "行号": None, "属性": name, "值": "",
                            "说明": "信号属性「%s」不在交付表里 → 未检查（不等于通过）；"
                                    "新增属性后需重跑步骤17" % sa})
    for d in deliv or []:
        if not isinstance(d, Mapping):
            continue
        attrs = d.get("attributes")
        if not isinstance(attrs, Mapping):
            continue
        for name, a in spec_by.items():
            p = a.get("params") if isinstance(a.get("params"), Mapping) else {}
            conds = p.get("parts_when")
            if not isinstance(conds, Sequence) or isinstance(conds, (str, bytes)):
                continue
            v = str(attrs.get(name) or "").strip()
            if _tokens(v) == [] or v in EMPTY_VALUES:
                continue
            n_parts = len([x for x in v.split("+") if x.strip()])
            # 逐条件判信号；任一条件成立即算「有多材质信号」；全部不可判则跳过
            signal, judged = False, False
            sig_desc = []
            for c in conds:
                if not isinstance(c, Mapping):
                    continue
                sa = str(c.get("属性") or "")
                sv = str(attrs.get(sa) or "").strip()
                if not sa or sv in EMPTY_VALUES or _tokens(sv) == []:
                    continue
                judged = True
                want = [str(x) for x in (c.get("含") or [])]
                if not want or any(w and w in sv for w in want):
                    signal = True
                    sig_desc.append("%s=%s" % (sa, sv))
            if not judged:
                continue                       # 信号缺失 → 不判
            if signal and n_parts < 2:
                out.append({"行号": d.get("xl_row"), "属性": name, "值": v,
                            "说明": "有多材质信号（%s）→ 应写 2 个 `A+B`，实际只写了 %d 个"
                                    % ("、".join(sig_desc) or "是", n_parts)})
            elif (not signal) and n_parts > 1:
                out.append({"行号": d.get("xl_row"), "属性": name, "值": v,
                            "说明": "无多材质信号 → 应只写 1 个，实际写了 %d 个" % n_parts})
    return out


def conflicts_report(hits: Sequence[Mapping[str, Any]]) -> dict:
    """汇总成一个可写盘的对象。"""
    by_pair: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for h in hits or []:
        pair = "%s↔%s" % (h.get("左属性"), h.get("右属性"))
        by_pair[pair] = by_pair.get(pair, 0) + 1
        key = h.get("说明") or pair
        by_rule[key] = by_rule.get(key, 0) + 1
    return {
        "_说明": "跨属性一致性冲突（**只登记，不静默改值**；未明确/空值不判）",
        "冲突总数": len(hits or []),
        "按属性对": by_pair,
        "按规则": by_rule,
        "明细": list(hits or []),
    }
