"""Dimension-aware numeric parsing shared by convergence and delivery rules."""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
_INTERVAL = re.compile(rf"^({_NUMBER})\s*(.*?)\s*[-~–—至]\s*({_NUMBER})\s*(.*?)$")


def numeric_target(params: Mapping) -> tuple[str, str | None]:
    from .units import QUANTITY_BASE, quantity_of_unit, convert

    unit = str(params.get("unit") or "").strip()
    quantity = params.get("quantity") or params.get("_物理量")
    factors = params.get("unit_factors") or {}
    if not isinstance(factors, Mapping):
        raise ValueError("unit_factors 必须是单位到换算系数的对象")
    for factor in factors.values():
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
            raise ValueError("单位换算系数必须是有限正数")
    if not unit and factors:
        targets = [str(u) for u, f in factors.items() if math.isclose(f, 1.0)]
        if not targets:
            raise ValueError("数值规则缺少目标单位，且系数表没有基准单位")
        unit = next((u for u in targets if u.lower() in {v.lower() for v in QUANTITY_BASE.values()}), targets[0])
        if any(convert(1, u, unit) not in (None, 1.0) for u in targets):
            raise ValueError("系数表存在多个不同的目标单位")
    if not unit and quantity:
        unit = QUANTITY_BASE.get(str(quantity), "")
    target_quantity = quantity_of_unit(unit)
    if quantity and target_quantity and target_quantity != quantity:
        raise ValueError("声明物理量与目标单位不一致")
    known_quantities = {quantity_of_unit(str(u)) for u in factors} - {None, "ambiguous"}
    if len(known_quantities) > 1:
        raise ValueError("单位系数表混入了其他物理量")
    quantity = quantity or target_quantity or (next(iter(known_quantities)) if known_quantities else None)
    if quantity == "ambiguous":
        raise ValueError("目标单位存在物理量歧义")
    for u in factors:
        q = quantity_of_unit(str(u))
        if quantity and q and q not in (quantity, "ambiguous"):
            raise ValueError("单位系数表混入了其他物理量")
    return unit, quantity


def _already_target_spelling(raw, target):
    original = str(raw).strip()
    if re.fullmatch(rf"{_NUMBER}\s*{re.escape(target)}", original):
        return True
    interval = _INTERVAL.fullmatch(original)
    if interval:
        _, left_unit, _, right_unit = interval.groups()
        units = (left_unit.strip(), right_unit.strip())
        return target in units and all(unit in ("", target) for unit in units)
    return False


def normalize_numeric(value: Any, params: Mapping, *, preserve_target_format=False) -> tuple[str, str]:
    from .converge import normalize_form
    from .units import parse_measure, convert

    text = normalize_form(value).strip()
    try:
        target, quantity = numeric_target(params)
    except ValueError:
        return text, "failed"
    if not text or not target:
        return text, "failed"
    factors = {str(u).strip().lower(): f for u, f in (params.get("unit_factors") or {}).items()}

    def scalar(number_text: str) -> tuple[float | None, bool]:
        # A single decimal comma is decimal punctuation; grouped commas stay thousands.
        number_text = re.sub(r"^([-+]?\d+),(\d{1,2})(?=\s*[^\d]|$)", r"\1.\2", number_text)
        if re.fullmatch(_NUMBER, number_text):
            return float(number_text.replace(",", ".")), True
        parsed = parse_measure(number_text, prefer=quantity)
        if parsed is None:
            custom = re.fullmatch(rf"({_NUMBER})\s*(.+)", number_text)
            if custom and custom.group(2).strip().lower() in factors:
                return float(custom.group(1).replace(",", ".")) * factors[custom.group(2).strip().lower()], False
            return None, False
        number, actual_quantity, source_unit = parsed
        if not math.isfinite(number) or actual_quantity == "ambiguous" or (quantity and actual_quantity != quantity):
            return None, False
        if factors:
            factor = factors.get(source_unit.lower())
            if factor is None:
                if source_unit.lower() == target.lower():
                    factor = 1.0
                else:
                    return None, False
            return number * factor, False
        return convert(number, source_unit, target), False

    interval = _INTERVAL.fullmatch(text)
    if interval:
        lo, lu, hi, hu = interval.groups()
        a, assumed_a = scalar(lo + (lu or hu))
        b, assumed_b = scalar(hi + (hu or lu))
        if a is None or b is None or not math.isfinite(a) or not math.isfinite(b) or a > b:
            return text, "failed"
        if preserve_target_format and not (assumed_a or assumed_b) and _already_target_spelling(value, target):
            return str(value).strip(), "ok"
        return f"{a:.12g}-{b:.12g}{target}", "assumed" if assumed_a or assumed_b else "ok"
    number, assumed = scalar(text)
    if number is None or not math.isfinite(number):
        return text, "failed"
    if preserve_target_format and not assumed and _already_target_spelling(value, target):
        return str(value).strip(), "ok"
    return f"{number:.12g}{target}", "assumed" if assumed else "ok"


def band_for(value: float, bands) -> str:
    for band in bands or []:
        if not isinstance(band, (list, tuple)) or len(band) != 3:
            raise ValueError("分档必须是三元素列表")
        if isinstance(band[0], str):
            label, lo, hi = band
        else:
            lo, hi, label = band
        if (lo is None or value > lo) and (hi is None or value <= hi):
            return str(label)
    return "未明确"


def interval_bounds(value: Any, unit: str = "") -> tuple[float, float] | None:
    """Parse a numeric interval without collapsing it to a representative value."""
    text = str(value).strip()
    if unit:
        text, status = normalize_numeric(text, {"unit": unit})
        if status == "failed":
            return None
        text = text[:-len(unit)]
    match = re.fullmatch(rf"({_NUMBER})\s*[-~–—至]\s*({_NUMBER})", text)
    if not match:
        return None
    lo, hi = (float(n.replace(",", ".")) for n in match.groups())
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) and lo <= hi else None
