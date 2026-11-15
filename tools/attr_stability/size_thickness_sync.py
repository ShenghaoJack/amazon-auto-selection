# -*- coding: utf-8 -*-
"""「厚度」与「尺寸规格·高(第三维)」的交叉对齐(2026-09-24 第三轮)。

用户口径
========
1. **厚度列要与尺寸规格里的厚度(高, 第三维)对齐** —— 两列不能互相矛盾;
2. 多件套可能是**每件厚度一样、长宽不同**, 也可能是**每件厚度都不同** ——
   **多个厚度就把每个厚度都写上**;
3. **没有厚度的写 `缺`**(取代原先的「未明确」), 与尺寸规格里缺位的写法一致;
4. 两列冲突时: **以核实到的单块厚度为准, 同时修正尺寸规格的高**。

为什么必须做这一层
==================
实测 2466 行里 `尺寸规格` 有 2437 行、其中 2056 行第三维有值; 而它与 `厚度` **不一致的有 246 行**。
不一致的主导原因就是用户点出的「整叠」: `尺寸规格` 那一维常是**整包/整叠高度** ——
  · 5 件套 `50.80x35.56x10.16 cm`, 单块厚 2.03cm(= 10.16 ÷ 5);
  · 2 件套 `大号:38.1x27.94x4.06 cm,小号:33.02x27.94x4.06 cm`, 单块厚 2.03cm(= 4.06 ÷ 2);
  · 12 件套 `30.5x20.3x7.6 cm`, 五点写明单块 6mm。
判定"这一维是不是整叠"用 `高 ≈ 件数 × 单块厚度`(容差 25%), 命中即把该维换成单块厚度。

写法
====
· 单件: `1.524cm`
· 多件且各件厚度相同: `1.5cm`(只写一个值)
· 多件且各件厚度不同: `大号:0.5cm,小号:0.4cm`(标签与 `尺寸规格` 逐段对应)
· 没有厚度: `缺`
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "parse_size_spec", "parse_cm", "format_cm", "reconcile", "apply_to_rows",
    "MISSING",
]

MISSING = "缺"
UNKNOWN_OLD = ("未明确", "None", "")
STACK_TOL = 0.25          # 「高 ≈ 件数 × 单块厚」的容差
SAME_TOL = 0.06           # 同值判定容差
PLAUSIBLE = (0.03, 15.0)  # 单块厚度的物理合理区间(cm)
STACK_MIN_H = 3.5         # 套装里这一维超过它就"像整叠"(见 _looks_like_stack)

# 一段尺寸: 「标签:」+ 长 x 宽 [x 高] + 单位
_SEG = re.compile(
    r'(?:(?P<label>[^,，:：]{1,12})[:：])?\s*'
    r'(?P<l>\d+(?:[.,]\d+)?)\s*[x×*]\s*(?P<w>\d+(?:[.,]\d+)?)'
    r'(?:\s*[x×*]\s*(?P<h>缺|\d+(?:[.,]\d+)?))?'
    r'\s*(?P<unit>cm|厘米)?',
    re.I)


def parse_cm(v: Any) -> Optional[float]:
    """`1.524cm` / `2.03` → float; 其它(缺/未明确/空) → None。"""
    s = str(v or "").strip()
    m = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(?:cm|厘米)?', s, re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def format_cm(x: Optional[float]) -> str:
    """按既有写法输出: `%g` + `cm`(如 `1.524cm`); None → `缺`。"""
    if x is None:
        return MISSING
    return "%gcm" % round(float(x), 4)


def format_num(x: Optional[float]) -> str:
    """只输出数字(用于**就地替换** `尺寸规格` 里的第三个数, 单位在外面不动)。"""
    if x is None:
        return MISSING
    return "%g" % round(float(x), 4)


def parse_size_spec(v: Any) -> list[dict]:
    """解析 `尺寸规格` → `[{label, l, w, h, h_span, raw}]`。

    `h=None` 表示该段第三维是 `缺`/缺失; `h_span` 是第三维在原文里的字符区间
    (便于**只改第三个数**、其余原样保留)。
    """
    s = str(v or "").strip()
    if not s or s in ("未明确", "None"):
        return []
    out = []
    for m in _SEG.finditer(s):
        def _num(g: str) -> Optional[float]:
            t = m.group(g)
            if not t or t == "缺":
                return None
            try:
                return float(t.replace(",", "."))
            except ValueError:
                return None
        out.append({
            "label": (m.group("label") or "").strip(),
            "l": _num("l"), "w": _num("w"), "h": _num("h"),
            "has_h_token": m.group("h") is not None,
            "h_span": m.span("h") if m.group("h") is not None else None,
            "unit": m.group("unit") or "",
            "raw": m.group(0),
        })
    return out


def _same(a: float, b: float) -> bool:
    return abs(a - b) / max(abs(a), abs(b), 1e-9) <= SAME_TOL


def _plausible(x: Optional[float]) -> bool:
    return x is not None and PLAUSIBLE[0] <= x <= PLAUSIBLE[1]


def _is_stack(h: Optional[float], t: Optional[float], n: Optional[int]) -> bool:
    """`h` 是否为整叠高度(≈ 件数 × 单块厚度)。"""
    if h is None or t is None or not n or n < 2:
        return False
    return abs(h - n * t) / max(h, n * t, 1e-9) <= STACK_TOL


def _looks_like_stack(h: Optional[float], n: Optional[int]) -> bool:
    """**没有**已核实厚度时, 单看 `h` 判它像不像"整叠"。

    套装里若 `h / k`(k = 2..件数) 落进单块合理区间, 就认为这一维是**多块叠起来的高度**
    —— 实测 3 件套写 `43.2x30.5x6.1 cm`(6.1 ≈ 3×2.03)、5 件套 `50.8x35.56x10.16`
    (10.16 = 5×2.032)、10 件套 `40.64x25.4x13.0048`(13 ÷ 10 = 1.3)。
    单块合理的上限取 3.5cm: 再厚就该是多块叠的了(单件真正的厚砧板不受此限, n<2 直接返回 False)。
    """
    if h is None or not n or n < 2:
        return False
    if h < STACK_MIN_H:            # 3.5cm 以下不像整叠, 别误伤
        return False
    for k in range(2, n + 1):
        q = h / k
        if PLAUSIBLE[0] <= q <= 3.5:
            return True
    return False


def reconcile(size_spec: Any, thickness: Any, set_count: Optional[int]) -> dict:
    """对齐一行 → `{厚度, 尺寸规格, 说明, 改动}`。

    `thickness` = 本轮之前**已核实**的厚度(文本/视觉得来), `set_count` = 件数。
    返回的新 `尺寸规格` 只改第三维, 长宽/标签/单位/空格**原样保留**。
    """
    segs = parse_size_spec(size_spec)
    t = parse_cm(thickness)
    if not _plausible(t):
        t = None
    n = set_count if (set_count and set_count > 1) else (len(segs) if len(segs) > 1 else None)

    if not segs:
        # 没有可解析的尺寸规格: 厚度直接用核实值, 没有就 缺
        new_t = format_cm(t)
        return {"厚度": new_t, "尺寸规格": size_spec, "说明": "无尺寸规格可对齐",
                "改动": str(thickness or "").strip() != new_t}

    notes: list[str] = []
    # ---- 1) 逐段判定"整叠", 是整叠就把该段的高作废
    #      · 有已核实厚度 t 时用 `高 ≈ 件数×t` 判;
    #      · **没有 t 时**(如步骤18 给的区间/前轮判为未明确) 用 `高/k` 的合理性判 ——
    #        否则会把套装的整叠高度当成单块厚度写进厚度列(2026-09-24 用户发现的回归)。
    eff: list[Optional[float]] = []
    for s in segs:
        h = s["h"]
        if _is_stack(h, t, n):
            notes.append("尺寸规格的高 %g 为整叠(≈%d×%g)" % (h, n, t))
            eff.append(None)
        elif t is None and _looks_like_stack(h, n):
            notes.append("尺寸规格的高 %g 像整叠(%d 件套), 不作为单块厚度" % (h, n))
            eff.append(None)
        elif _plausible(h):
            eff.append(h)
        else:
            eff.append(None)
    known = [x for x in eff if x is not None]
    # 单块厚度的基准: **以已核实的 t 为准**(用户口径); 没有 t 时用尺寸规格里已有的高
    base = t
    if base is None and known and all(_same(x, known[0]) for x in known):
        base = known[0]
    # ---- 2) 各件厚度是否不同(不同则逐件写, 相同则一个值)
    distinct = {round(x, 4) for x in known}
    per_piece = len(distinct) > 1
    per: list[Optional[float]] = []
    for x in eff:
        per.append(x if x is not None else (None if per_piece else base))
    if per_piece:
        notes.append("各件厚度不同 → 逐件写出")
    # ---- 3) 厚度写法
    vals = [x for x in per if x is not None]
    if not vals:
        new_th = MISSING
        per = [None] * len(segs)
    elif not per_piece:
        one = base if base is not None else vals[0]
        new_th = format_cm(one)
        if t is not None and known and not _same(one, known[0]):
            notes.append("按核实到的单块厚度 %s 对齐" % format_cm(t))
        per = [one] * len(segs)      # 各件统一 → 缺位也按同厚度补齐
    else:
        parts = []
        for s, x in zip(segs, per):
            lab = s["label"] or ""
            parts.append(("%s:%s" % (lab, format_cm(x))) if lab else format_cm(x))
        new_th = ",".join(parts)
    # ---- 4) 回写尺寸规格的第三维: 数值**真的不同**才改(纯格式差异如 `2.0`/`2` 不动)
    spec_per = []
    for s, x in zip(segs, per):
        h = s["h"]
        if h is not None and x is not None and abs(h - x) <= 1e-6:
            spec_per.append(h)
        else:
            spec_per.append(x)
    new_spec = _rewrite_spec(str(size_spec or ""), segs, spec_per)
    changed = (str(thickness or "").strip() != new_th) or \
              (str(size_spec or "").strip() != new_spec)
    return {"厚度": new_th, "尺寸规格": new_spec,
            "说明": "；".join(dict.fromkeys(notes)) or "已一致", "改动": changed}


def _rewrite_spec(text: str, segs: Sequence[Mapping[str, Any]],
                  per: Sequence[Optional[float]]) -> str:
    """把每段的第三个数换成新值。**只替换该 token**, 长宽/标签/单位/空格保持原样;
    原文本里该段**没有**第三个数时, 在长宽之后补 `x<值>`。"""
    if not text:
        return text
    edits = []
    for s, x in zip(segs, per):
        if not s.get("h_span"):
            continue
        a, b = s["h_span"]
        val = format_num(x)          # **只换数字**, `cm` 在原位不动
        if text[a:b] != val:
            edits.append((a, b, val))
    out = text
    for a, b, val in sorted(edits, key=lambda e: -e[0]):
        out = out[:a] + val + out[b:]
    return _append_h(out, per)


def _append_h(text: str, per: Sequence[Optional[float]]) -> str:
    """给"只有长x宽"的段补上第三维(段序与 `per` 对应, 倒序插入以保偏移)。"""
    ms = list(_SEG.finditer(text))
    if len(ms) != len(per):
        return text                       # 解析段数对不上 → 不动, 避免写坏
    out = text
    for m, x in list(zip(ms, per))[::-1]:
        if m.group("h") is not None:
            continue
        w_end = m.end("w")
        out = out[:w_end] + "x" + format_num(x) + out[w_end:]
    return out


def apply_to_rows(declared: Mapping[Any, Mapping[str, Any]],
                  thickness_by: Mapping[Any, Any],
                  set_count_by: Mapping[Any, Any]) -> dict:
    """对整表跑一遍 → `{xl_row: {厚度, 尺寸规格, 说明, 改动}}`。"""
    out: dict = {}
    for xl, d in declared.items():
        r = reconcile(d.get("尺寸规格"), thickness_by.get(xl, d.get("厚度")),
                      set_count_by.get(xl))
        r["原厚度"] = str(thickness_by.get(xl, d.get("厚度")) or "")
        r["原尺寸规格"] = str(d.get("尺寸规格") or "")
        out[xl] = r
    return out
