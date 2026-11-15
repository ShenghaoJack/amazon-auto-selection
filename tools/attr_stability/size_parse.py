# -*- coding: utf-8 -*-
"""尺寸字符串 → 长/宽/高 三列（每列可含多个尺寸，用 ` / ` 分隔）。

2026-10-26 用户口径:
  · 步骤17 只把尺寸**原样抄进 `尺寸`**（含原单位），不换算、不拆列；
  · 拆列在**步骤18** 做，且只要 **3 列**（长/宽/高），不要 12 列；
  · **套件（大中小三块）时三列各含 3 个值**，用 ` / ` 串起来，
    顺序 = 识别到 小/中/大 就按 小→中→大，识别不到就按原文顺序；
  · 单位统一换算成 cm（inch×2.54 / mm÷10 / feet×30.48）。

示例:
    >>> parse_size("小 20x15cm / 中 25x18cm / 大 30x22cm")
    {'长': '20 / 25 / 30', '宽': '15 / 18 / 22', '高': '', '组数': 3, '顺序': ['小','中','大']}
    >>> parse_size("32x17x22cm")
    {'长': '32', '宽': '17', '高': '22', '组数': 1, '顺序': ['单']}
"""
from __future__ import annotations

import re

# 单位 → 换算到 cm 的倍数
_UNIT_FACTOR = {
    "mm": 0.1, "毫米": 0.1,
    "cm": 1.0, "厘米": 1.0,
    "m": 100.0, "米": 100.0,
    "inch": 2.54, "in": 2.54, "inches": 2.54, "寸": 2.54, "英寸": 2.54, '"': 2.54, "”": 2.54,
    "ft": 30.48, "feet": 30.48, "foot": 30.48, "英尺": 30.48,
}
# 标签 → 排序权重（识别到就按这个排）
_LABEL_ORDER = {
    "小": 0, "s": 0, "small": 0, "小号": 0, "mini": 0,
    "中": 1, "m": 1, "medium": 1, "中号": 1, "mid": 1,
    "大": 2, "l": 2, "large": 2, "大号": 2, "xl": 3, "加大": 3,
}
_LABEL_RX = re.compile(
    r"(小号|中号|大号|加大|小|中|大|mini|small|medium|mid|large|xl|\b[SMXL]\b)",
    re.I)
# 三个数字（允许 x / X / × / * / by 分隔），数字可带小数
_NUM = r"(\d+(?:\.\d+)?)"
_DIMS_RX = re.compile(
    _NUM + r"\s*(?:x|X|×|\*|by|\s)\s*" + _NUM + r"(?:\s*(?:x|X|×|\*|by)\s*" + _NUM + r")?")
_UNIT_RX = re.compile(
    # ⚠ 单位必须**紧跟在数字后面**（`(?<=\d)`）——否则 "Small" 里的 m 会被当成「米」，
    #   把 `8x6` 算成 `800`（实测踩过）。
    r"(?<=\d)\s*(mm|毫米|cm|厘米|米|inch(?:es)?|英寸|寸|\"|”|feet|foot|英尺|ft|in|m)(?![a-z])",
    re.I)


def _to_cm(value: float, unit: str | None) -> float:
    if not unit:
        return value
    f = _UNIT_FACTOR.get(unit.strip().lower())
    if f is None:
        f = _UNIT_FACTOR.get(unit.strip())
    return round(value * (f if f is not None else 1.0), 2)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


def _split_groups(raw: str) -> list[str]:
    """按 ` / `、`；`、`,` 切分组 —— **但绝不能按 x 切**（那是维度分隔符）。"""
    s = str(raw or "").strip()
    if not s:
        return []
    # 优先按 " / " 切（步骤17 的提示词要求的分隔符）
    if " / " in s:
        return [p.strip() for p in s.split(" / ") if p.strip()]
    # 其次按 ; ； 切
    if re.search(r"[;；]", s):
        return [p.strip() for p in re.split(r"[;；]", s) if p.strip()]
    # 最后按 , ，切（但要避免把 "12,5 x 8" 的小数点切开 → 只在后面不是数字时切）
    parts = re.split(r"[,，](?!\d)", s)
    return [p.strip() for p in parts if p.strip()]


def _parse_one(group: str, default_unit: str | None) -> tuple[str, dict]:
    """解析一组 → (标签, {长,宽,高})。标签识别不到返回 ""。"""
    label = ""
    m = _LABEL_RX.search(group)
    if m:
        lab = m.group(1).strip().lower()
        if lab in _LABEL_ORDER:
            label = m.group(1).strip()
    dims: dict = {}
    dm = _DIMS_RX.search(group)
    if dm:
        nums = [dm.group(i) for i in (1, 2, 3)]
        # 单位：先看这组里出现的单位，没有就用 default_unit
        um = _UNIT_RX.search(group)
        unit = um.group(1) if um else default_unit
        for key, val in zip(("长", "宽", "高"), nums):
            if val is None:
                continue
            dims[key] = _to_cm(float(val), unit)
    return label, dims


def parse_size(raw: str) -> dict:
    """尺寸字符串 → `{"长": "a / b / c", "宽": ..., "高": ..., "组数": n, "顺序": [...]}`。

    组数 = 1 时直接给单个值（不带 `/`）；
    组数 > 1 时按 小→中→大 排序（识别不到标签就按原文顺序）。
    """
    groups = _split_groups(raw)
    if not groups:
        return {"长": "", "宽": "", "高": "", "组数": 0, "顺序": []}
    # 全串默认单位（如 "Set of 3: 8x6, 10x7, 12x9 in" → 后面每组共享 in）
    full_unit_m = _UNIT_RX.search(str(raw or ""))
    default_unit = full_unit_m.group(1) if full_unit_m else None

    parsed: list[tuple[int, int, str, dict]] = []
    for idx, g in enumerate(groups):
        label, dims = _parse_one(g, default_unit)
        if not dims:
            continue
        order = _LABEL_ORDER.get(label.lower()) if label else None
        parsed.append((order if order is not None else 1000 + idx, idx, label, dims))
    if not parsed:
        return {"长": "", "宽": "", "高": "", "组数": 0, "顺序": []}
    parsed.sort(key=lambda x: (x[0], x[1]))

    out: dict = {}
    for key in ("长", "宽", "高"):
        vals = [p[3].get(key) for p in parsed]
        if all(v is None for v in vals):
            out[key] = ""
        elif len(parsed) == 1:
            out[key] = _fmt(vals[0])
        else:
            out[key] = " / ".join(_fmt(v) for v in vals)
    out["组数"] = len(parsed)
    out["顺序"] = [p[2] or ("单" if len(parsed) == 1 else "尺寸%d" % (i + 1))
                   for i, p in enumerate(parsed)]
    return out
