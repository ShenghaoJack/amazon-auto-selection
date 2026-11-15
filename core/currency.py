"""站点币种及价格文本中的币种校验。价格数值不会进行汇率换算。"""
from __future__ import annotations

import re

SITE_CURRENCY = {
    "美国": "USD", "英国": "GBP", "德国": "EUR", "法国": "EUR",
    "意大利": "EUR", "西班牙": "EUR", "加拿大": "CAD", "日本": "JPY",
    "墨西哥": "MXN", "巴西": "BRL", "澳大利亚": "AUD", "印度": "INR",
    "阿联酋": "AED", "荷兰": "EUR", "瑞典": "SEK", "新加坡": "SGD",
}
CURRENCY_NAMES = {
    "USD": "美元", "GBP": "英镑", "EUR": "欧元", "CAD": "加元",
    "JPY": "日元", "MXN": "墨西哥比索", "BRL": "雷亚尔", "AUD": "澳元",
    "INR": "印度卢比", "AED": "阿联酋迪拉姆", "SEK": "瑞典克朗", "SGD": "新加坡元",
}
_EXPLICIT = {
    "US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD",
    "MXN$": "MXN", "MX$": "MXN", "R$": "BRL", "S$": "SGD",
    "د.إ": "AED", "د.إ.": "AED", "DHS": "AED", "DH": "AED",
    "£": "GBP", "€": "EUR", "₹": "INR", "₩": "KRW",
}
_CODES = set(CURRENCY_NAMES) | {"CNY", "RMB", "KRW", "MYR"}
_MARKER = re.compile(
    r"(?i)(?<![A-Za-z])(?:" + "|".join(re.escape(x) for x in sorted(_CODES | set(_EXPLICIT), key=len, reverse=True))
    + r")(?![A-Za-z])|[$¥￥]"
)


def currency_markers(value, site: str = "") -> set[str]:
    """返回明确或结合站点可确定的货币；裸 $ 按美元类站点本币识别。"""
    if not isinstance(value, str):
        return set()
    expected = SITE_CURRENCY.get(site.strip(), "")
    found = set()
    for m in _MARKER.finditer(value):
        token = m.group().upper()
        if token == "$":
            found.add(expected if expected in {"USD", "CAD", "AUD", "MXN", "BRL", "SGD"} else "USD")
        elif token in {"¥", "￥"}:
            found.add("JPY" if expected == "JPY" else "CNY")
        else:
            code = _EXPLICIT.get(token, token)
            found.add("CNY" if code == "RMB" else code)
    return found


def currency_conflict(value, site: str) -> set[str]:
    expected = SITE_CURRENCY.get(site.strip(), "")
    found = currency_markers(value, site)
    return found - {expected} if expected else found


def strip_currency(value: str) -> str:
    return _MARKER.sub("", value)
