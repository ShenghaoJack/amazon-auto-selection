"""Display-only precision helpers for step 15; never used for decisions."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_PERCENT = re.compile(rf"(?<![\d.,])({_NUMBER})(\s*[%％])")
_RATING = re.compile(rf"((?:评分|星级)(?!数)(?:为|约|是|达到|[：:\s])*)({_NUMBER})(?=\s*(?:分|[，,；;。\s]|$))")
_SEASON = re.compile(rf"((?:季节波动系数|季节波动|季节性系数|峰谷比)(?:为|约|是|达到|[：:\s])*)({_NUMBER})(?=\s*(?:倍|[，,；;。\s]|$))")
_SEASON_DETAIL = re.compile(rf"(?<![\d.,])({_NUMBER})(?=（峰\d{{4}}-\d{{2}})")
_DECIMAL = re.compile(r"(?<![\d.$])([+-]?\d{1,3}\.\d+)(?!\d)")
_DEMAND = re.compile(rf"((?:月需求量估算|月需求量|月均销量|月平均销量|平均月销量)(?:为|约|是|达到|[：:\s])*)({_NUMBER})(?=\s*(?:件|[，,；;。]|$))")
_GMV = re.compile(rf"((?:(?:样本|类目|市场)?\s*GMV|样本月销额(?:合计|估算)?)(?:为|约|是|达到|[：:\s])*)({_NUMBER})\s*(百万|千万|万)?\s*(美元|USD|欧元|EUR|英镑|GBP)(?=\s*(?:/月|每月|[，,；;。\s]|$))", re.I)
_CURRENCY_LABELS = {"USD":"美元", "GBP":"英镑", "EUR":"欧元", "CAD":"加元", "JPY":"日元", "MXN":"墨西哥比索", "BRL":"雷亚尔", "AUD":"澳元", "INR":"印度卢比", "AED":"迪拉姆", "SEK":"瑞典克朗", "SGD":"新加坡元"}


def _one(number: str) -> str:
    try:
        rounded = Decimal(number.replace(",", "")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return number
    if rounded == 0:
        rounded = abs(rounded)
    result = f"{rounded:,.1f}" if "," in number else f"{rounded:.1f}"
    return ("+" if number.startswith("+") and not result.startswith("-") else "") + result


def _integer(number: str) -> str:
    try:
        rounded = Decimal(number.replace(",", "")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return number
    return f"{rounded:,.0f}" if "," in number else f"{rounded:.0f}"


def _million_match(match):
    number = Decimal(match.group(2).replace(",", ""))
    scale = {None: Decimal("0.000001"), "万": Decimal("0.01"), "百万": Decimal(1), "千万": Decimal(10)}[match.group(3)]
    currency = {"美元": "USD", "欧元": "EUR", "英镑": "GBP"}.get(match.group(4), match.group(4).upper())
    million = number * scale
    suffix = "B" if abs(million) >= 1000 else "M"
    return match.group(1) + currency + " " + _one(str(million / 1000 if suffix == "B" else million)) + suffix


def _gmv_standard(value):
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"\s*([<>≥≤]=?)\s*(?:USD\s*)?([\d,]+(?:\.\d+)?)\s*(?:USD|美元)?\s*(?:/月)?\s*", value)
    if not match:
        return value
    number = Decimal(match.group(2).replace(",", ""))
    divisor, suffix = (Decimal(1000000000), "B") if abs(number) >= 1000000000 else (Decimal(1000000), "M")
    compact = format(number / divisor, "f")
    if "." in compact:
        compact = compact.rstrip("0").rstrip(".")
    if "." not in compact:
        compact += ".0"
    return f"{match.group(1)} USD {compact}{suffix}/月"


def format_text(value, *, season_detail: bool = False):
    if not isinstance(value, str):
        return value
    value = _PERCENT.sub(lambda m: _one(m.group(1)) + m.group(2), value)
    value = _RATING.sub(lambda m: m.group(1) + _one(m.group(2)), value)
    value = _SEASON.sub(lambda m: m.group(1) + _one(m.group(2)), value)
    value = _DEMAND.sub(lambda m: m.group(1) + _integer(m.group(2)), value)
    value = _GMV.sub(_million_match, value)
    # Model prose may mention both yearly ratios in the same sentence without
    # repeating the label before each number.
    parts = re.split(r"([。！？；;\n])", value)
    for index in range(0, len(parts), 2):
        if re.search(r"季节波动|季节性系数|峰谷比", parts[index]):
            parts[index] = _DECIMAL.sub(lambda m: _one(m.group(1)), parts[index])
    value = "".join(parts)
    if season_detail:
        value = _SEASON_DETAIL.sub(lambda m: _one(m.group(1)), value)
    return value


def format_indicator_text(row: dict) -> dict:
    """Format human-readable fields while leaving numeric data and status intact."""
    result = dict(row)
    name = result.get("指标")
    season = name in ("季节波动系数", "旺季月份", "淡季月份")
    for field in ("原表标准", "原表备注及口径", "证据与限制"):
        if field in result:
            result[field] = format_text(result[field], season_detail=season)
    if name == "类目GMV估算":
        result["原表标准"] = _gmv_standard(result.get("原表标准"))
    if name in ("CPC竞价", "平均客单价") and isinstance(result.get("原表标准"), str):
        result["原表标准"] = re.sub(r"([\d.]+)\s*美元", r"USD \1", result["原表标准"])
    if name in ("趋势方向（站内）", "趋势方向（Google）", "新卖家占比"):
        result["实际值"] = format_text(result.get("实际值"))
    return result


def format_workbook(wb) -> None:
    """Apply visual precision to report sheets without rounding numeric cells."""
    def headers(ws):
        return {str(cell.value): cell.column for cell in ws[1] if cell.value is not None}

    for sheet in ("综合分析", "关键词与季节性"):
        if sheet not in wb:
            continue
        ws = wb[sheet]
        columns = headers(ws)
        percent_cols = [index for name, index in columns.items() if "%" in name or "％" in name]
        for row in ws.iter_rows(min_row=2):
            for index in percent_cols:
                cell = row[index - 1]
                if isinstance(cell.value, (float, int)) and not isinstance(cell.value, bool):
                    cell.number_format = "0.0"
            for name in ("项目", "数值"):
                index = columns.get(name)
                if index:
                    cell = row[index - 1]
                    cell.value = format_text(cell.value)

    if "变体明细" in wb:
        ws = wb["变体明细"]
        column = headers(ws).get("评分")
        if column:
            for row in ws.iter_rows(min_row=2):
                cell = row[column - 1]
                if isinstance(cell.value, (float, int)) and not isinstance(cell.value, bool):
                    cell.number_format = "0.0"

    if "口径说明" in wb:
        ws = wb["口径说明"]
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.value = format_text(cell.value)

    if "选品指标判定" in wb:
        ws = wb["选品指标判定"]
        columns = headers(ws)
        for row in ws.iter_rows(min_row=2):
            get = lambda name: row[columns[name] - 1].value if name in columns else None
            if get("指标") in ("趋势方向（站内）", "趋势方向（Google）") and "实际值" in columns:
                actual = row[columns["实际值"] - 1]
                if isinstance(actual.value, str) and re.fullmatch(r"\s*[+-]?\d+(?:\.\d+)?\s*[%％]\s*", actual.value):
                    actual.value = float(actual.value.strip().rstrip("%％").strip())
                    if "单位" in columns:
                        row[columns["单位"] - 1].value = "%"
            item = format_indicator_text({name: get(name) for name in columns})
            for name in ("实际值", "原表标准", "原表备注及口径", "证据与限制"):
                if name in columns:
                    row[columns[name] - 1].value = item[name]
            actual = row[columns["实际值"] - 1] if "实际值" in columns else None
            if actual is not None and isinstance(actual.value, (float, int)) and not isinstance(actual.value, bool):
                if get("单位") == "%":
                    actual.number_format = '+0.0"%";-0.0"%";0.0"%"' if get("指标") in ("趋势方向（站内）", "趋势方向（Google）") else '0.0"%"'
                elif get("指标") in ("评分", "季节波动系数"):
                    actual.number_format = "0.0"
                elif get("指标") == "月需求量估算":
                    actual.number_format = "0"
                elif get("指标") == "类目GMV估算":
                    code = str(get("单位") or "").split("/")[0]
                    label = code or "币种未知"
                    actual.number_format = f'"{label} "0.0,,,"B/月"' if abs(actual.value) >= 1000000000 else f'"{label} "0.0,,"M/月"'

    if "AI市场建议" in wb:
        ws = wb["AI市场建议"]
        content_col = headers(ws).get("内容")
        if content_col:
            for row in ws.iter_rows(min_row=2):
                cell = row[content_col - 1]
                cell.value = format_text(cell.value)
