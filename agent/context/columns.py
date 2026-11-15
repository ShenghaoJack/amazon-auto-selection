"""列名归一化/定位/取值工具(自 `steps/step15.py`、`step16.py` 复制, 自包含)。

产物列名与主流水线保持一致, 但为隔离起见这里独立实现一份,
不 import steps/*; 改动这里不影响主流水线。
"""

from __future__ import annotations

import re


def norm_col(name) -> str:
    """把列名归一化为可匹配的紧凑字符串(去空白/下划线/括号/百分比)。"""
    return re.sub(r"[\s_\-（）()%]", "", str(name)).lower()


def find_col(df, candidates: list[str]):
    """按候选列名逐一定位真实列(先精确, 后包含)。找不到返回 None。"""
    cols = {norm_col(c): c for c in df.columns}
    for cand in candidates:
        key = norm_col(cand)
        if key in cols:
            return cols[key]
    for cand in candidates:
        key = norm_col(cand)
        for norm, col in cols.items():
            if key and key in norm:
                return col
    return None


def cell(row, col) -> str:
    """安全取单元格字符串, 空值/NaN 返回 ''。"""
    if col is None:
        return ""
    val = row.get(col)
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s


def to_num(value):
    """数值化(去逗号/币符/百分号); 非法返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, bool):
            return None
        return float(value)
    s = str(value).strip().replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    if not s or s.lower() in ("nan", "none", "null", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_label(value) -> str:
    """清洗标签值, 空/NaN 返回 '未知'。"""
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return "未知"
    return s
