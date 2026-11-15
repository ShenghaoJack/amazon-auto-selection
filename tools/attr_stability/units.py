# -*- coding: utf-8 -*-
"""单位表按**物理量**分类 + 混量检测。

**来源**：本模块先在 `scripts/_plan_pilot/units.py` 试点验证（见
`冲突与值合法性校验-改动清单-20261007.md` §4 组 0-9），验证通过后落生产。
试点侧改为 re-export 本模块，避免两份实现。

**为什么需要它**
----------------
实测两个旧单位表都不够用：

  · `converge._UNIT_TO_CM` —— **只有长度单位**（cm/mm/m/ft/inch/…）
    → `容量` 的 `L`、`最大风速` 的 `mph`/`CFM` 全部解析失败；
    后果：`容量` 逃过 `is_measure_like` 路由 → 被当枚举聚类 →
    `55L`/`53L`/`108L` 成了三个"类别"。

  · `plan_engine.UNIVERSAL_UNIT_FACTORS` —— 有体积/重量/功率，但
    `"m/s": 1.0, "km/h": 1.0, "mph": 1.0` **全是 1.0**（假换算），
    且**没有风量**（CFM/FPM）→ `最大风速` 里 `100m/s` 与 `246MPH` 永远归不到一起。

**本模块提供**
--------------
1. `parse_measure(v)` —— 解析「数值 + 单位」→ `(数值, 物理量, 单位名)`，
   支持 `/`（m/s）、`³`（m³/h）、中文单位、大小写
2. `convert(...)` —— 同物理量内真换算；**跨物理量一律返回 None（绝不猜）**
3. `is_measure_like(values)` —— 计量路由判据（0-8）
4. `mixed_quantity_report(values)` —— **混量检测**（0-9）：
   同一属性出现 ≥2 个物理量时报警（如 `最大风速` 同时含 `mph` 风速与 `CFM` 风量，
   这是**两个物理量，不能用一列数值表示**，必须拆属性或声明主口径）
5. `unit_factors_for(quantity, target_unit)` —— 给 `rule_numeric` 用的 `unit_factors`，
   只含该物理量、系数相对目标单位

**歧义单位**：`oz` 既可是体积(fl oz)也可是重量(oz)。标为 `ambiguous`，**不猜**，
由调用方给 `prefer` 或按类目声明决定（该类目既有口径就是"单位口径待确认"）。

**注意**：生产 `std_rules._parse_num_unit` 的单位正则
（`^([\\d.]+)\\s*([A-Za-z%μ°Ω]+)$`）**不认 `/`、`³`、中文，且大小写敏感** ——
所以本模块自带 `pilot_numeric` 作为该缺陷的正确实现（阶段 2 应回填进 `rule_numeric`）。
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import Any, Sequence

from .converge import normalize_form

__all__ = [
    "QUANTITY_UNITS", "QUANTITY_BASE", "UNIT_TO_QUANTITY", "AMBIGUOUS_UNITS",
    "parse_measure", "convert", "quantity_of_unit", "units_of_quantity",
    "unit_factors_for", "is_measure_like", "looks_multi_dim",
    "mixed_quantity_report", "dominant_quantity", "pilot_numeric",
]

# ------------------------------------------------------------------ 单位表
# 每类：单位名(小写) → 换算到**基准单位**的系数
QUANTITY_UNITS: dict[str, dict[str, float]] = {
    # 长度（基准 cm）
    "length": {
        "cm": 1.0, "厘米": 1.0, "公分": 1.0,
        "mm": 0.1, "毫米": 0.1,
        "m": 100.0, "米": 100.0,
        "km": 100000.0, "千米": 100000.0, "公里": 100000.0,
        "inch": 2.54, "inches": 2.54, "in": 2.54, "英寸": 2.54, '"': 2.54,
        "ft": 30.48, "feet": 30.48, "foot": 30.48, "英尺": 30.48, "'": 30.48,
        "yd": 91.44, "码": 91.44,
    },
    # 面积（基准 m²）。平方、立方与长度是独立物理量，不能互换。
    "area": {
        **dict.fromkeys(("m2", "m²", "m^2", "㎡", "平方米", "平方公尺", "sqm", "sq.m", "sq.m.",
                         "sq m", "square meter", "square meters", "square metre", "square metres",
                         "square_meter", "square_meters", "square_metre", "square_metres"), 1.0),
        **dict.fromkeys(("ft2", "ft²", "ft^2", "sq.ft", "sq.ft.", "sq ft", "sq. ft", "sq. ft.",
                         "square foot", "square feet", "square_foot", "square_feet", "平方英尺"), 0.09290304),
        **dict.fromkeys(("in2", "in²", "in^2", "sq.in", "sq.in.", "sq in", "sq. in", "sq. in.",
                         "square inch", "square inches", "square_inch", "square_inches", "平方英寸"), 0.00064516),
        **dict.fromkeys(("cm2", "cm²", "cm^2", "平方厘米", "square centimeter", "square centimeters",
                         "square_centimeter", "square_centimeters", "square centimetre", "square centimetres"), 0.0001),
        **dict.fromkeys(("mm2", "mm²", "mm^2", "平方毫米"), 0.000001),
        "yd2": 0.83612736, "yd²": 0.83612736, "square yard": 0.83612736,
        "ha": 10000.0, "公顷": 10000.0, "acre": 4046.8564224, "acres": 4046.8564224,
    },
    # 体积（基准 ml）
    "volume": {
        "ml": 1.0, "毫升": 1.0, "cc": 1.0, "cm3": 1.0, "cm³": 1.0,
        "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0, "litres": 1000.0, "升": 1000.0, "公升": 1000.0,
        "m3": 1000000.0, "m³": 1000000.0,
        **dict.fromkeys(("ft3", "ft³", "ft^3", "cu.ft", "cu ft", "cubic foot", "cubic feet",
                         "cubic_foot", "cubic_feet", "立方英尺"), 28316.846592),
        **dict.fromkeys(("in3", "in³", "in^3", "cu.in", "cu in", "cubic inch", "cubic inches",
                         "cubic_inch", "cubic_inches", "立方英寸"), 16.387064),
        "gal": 3785.41, "gallon": 3785.41, "gallons": 3785.41, "加仑": 3785.41,
        "qt": 946.353, "quart": 946.353, "quarts": 946.353,
        "pt": 473.176, "pint": 473.176, "pints": 473.176,
        "cup": 236.588, "cups": 236.588, "杯": 236.588,
        "fl oz": 29.5735, "floz": 29.5735, "fl. oz": 29.5735, "fl.oz": 29.5735,
        "fluid ounce": 29.5735, "fluid ounces": 29.5735, "液量盎司": 29.5735,
    },
    # 重量（基准 g）
    "weight": {
        "g": 1.0, "克": 1.0, "gram": 1.0, "grams": 1.0,
        "mg": 0.001, "毫克": 0.001,
        "kg": 1000.0, "千克": 1000.0, "公斤": 1000.0,
        "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592, "磅": 453.592,
        "t": 1000000.0, "吨": 1000000.0,
        "ozw": 28.3495, "盎司": 28.3495,
    },
    # 功率（基准 W）
    "power": {
        "w": 1.0, "瓦": 1.0, "瓦特": 1.0, "watt": 1.0, "watts": 1.0,
        "kw": 1000.0, "千瓦": 1000.0,
        "hp": 745.7, "马力": 745.7,
    },
    # 电量（基准 mAh）
    "charge": {
        "mah": 1.0, "毫安时": 1.0,
        "ah": 1000.0, "安时": 1000.0,
    },
    # 风速 / 线速度（基准 m/s）—— 修正旧表 m/s / km/h / mph 全为 1.0 的假换算
    "speed": {
        "m/s": 1.0, "mps": 1.0, "米/秒": 1.0, "m/sec": 1.0,
        "km/h": 0.2777778, "kmh": 0.2777778, "kph": 0.2777778,
        "公里/小时": 0.2777778, "千米/小时": 0.2777778,
        "mph": 0.44704, "英里/小时": 0.44704,
        "ft/s": 0.3048, "fps": 0.3048, "ft/sec": 0.3048,
        # 实测取值里出现的**拼写形式**（`203 feet per second` / `风速82-164 Ft/s`）
        "feet per second": 0.3048, "foot per second": 0.3048,
        "feet/second": 0.3048, "feet per sec": 0.3048,
        "ft/min": 0.00508, "fpm": 0.00508, "英尺/分": 0.00508, "英尺/分钟": 0.00508,
    },
    # 转速（基准 rpm）—— 实测 `110,000 RPM`；与线速度是**不同物理量**，不可混算
    "rotational": {
        "rpm": 1.0, "r/min": 1.0, "转/分": 1.0, "转每分钟": 1.0,
        "rps": 60.0, "转/秒": 60.0,
    },
    # 风量（基准 m³/h）—— 旧表**完全没有**这一类
    "airflow": {
        "cfm": 1.69901, "立方英尺/分": 1.69901,
        "m3/h": 1.0, "m³/h": 1.0, "立方米/小时": 1.0, "cmh": 1.0,
        "m3/min": 60.0, "m³/min": 60.0,
        "l/s": 3.6, "升/秒": 3.6,
    },
    # 噪音（基准 dB；不可跨类换算）
    "noise": {"db": 1.0, "分贝": 1.0, "dba": 1.0},
    # 时间（基准 s）
    "time": {
        "s": 1.0, "秒": 1.0, "sec": 1.0, "secs": 1.0,
        "min": 60.0, "mins": 60.0, "分钟": 60.0,
        "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "小时": 3600.0,
    },
    # 压力（基准 kPa）
    "pressure": {
        "kpa": 1.0, "千帕": 1.0,
        "pa": 0.001, "帕": 0.001,
        "psi": 6.894757,
        "bar": 100.0, "mbar": 0.1,
    },
    # 频率（基准 Hz）
    "frequency": {"hz": 1.0, "赫兹": 1.0, "khz": 1000.0, "千赫": 1000.0},
    # 电压（基准 V）
    "voltage": {"v": 1.0, "伏": 1.0, "伏特": 1.0, "mv": 0.001},
    # 电流（基准 A）
    "current": {"a": 1.0, "安": 1.0, "安培": 1.0, "ma": 0.001},
    # 计数（基准 件）—— **量词**也是单位。实测 `配件数量` 的值是 `4个`/`5个`/`3件`，
    # 不认量词就会被判成"非数值"（Dog Dryer 报 101 格，占该类目非数值的 64%）。
    # 注意：**不含 `档`** —— `3档` 是枚举（风速档位），不是计数。
    "count": {
        "件": 1.0, "个": 1.0, "只": 1.0, "枚": 1.0, "条": 1.0, "片": 1.0,
        "支": 1.0, "组": 1.0, "台": 1.0, "套": 1.0, "包": 1.0, "盒": 1.0,
        "pcs": 1.0, "pc": 1.0, "piece": 1.0, "pieces": 1.0, "pack": 1.0,
        "packs": 1.0, "set": 1.0, "sets": 1.0, "count": 1.0, "ct": 1.0,
    },
}

QUANTITY_BASE: dict[str, str] = {
    "length": "cm", "area": "m2", "volume": "ml", "weight": "g", "power": "W", "charge": "mAh",
    "speed": "m/s", "airflow": "m3/h", "noise": "dB", "time": "s",
    "pressure": "kPa", "frequency": "Hz", "voltage": "V", "current": "A",
    "rotational": "rpm", "count": "件",
}

# 歧义单位：不带上下文无法判定物理量（不猜）
AMBIGUOUS_UNITS: dict[str, tuple[str, ...]] = {
    "oz": ("volume", "weight"),
    "盎司": ("volume", "weight"),
}

# 单位 → 物理量（歧义的排除在外）
UNIT_TO_QUANTITY: dict[str, str] = {}
for _q, _us in QUANTITY_UNITS.items():
    for _u in _us:
        UNIT_TO_QUANTITY.setdefault(_u, _q)

_NUM_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
# 分数（实测 `1/4杯`、`3/4 tsp`）：`a/b` 视作 a÷b
_FRAC_RE = re.compile(r"^([-+]?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$")
# 计数组合的**尾缀**（实测 `3件套` = "3-piece set"）：量词夹在中间会让常规后缀匹配失败
# （`3件套` 以 `套` 结尾 → head=`3件` 不是数字 → 解析失败）。剥掉尾缀再试一次。
_TAIL_MODIFIERS = ("套", "组合", "装", "套装")
_ALL_UNITS = sorted(
    list(UNIT_TO_QUANTITY) + list(AMBIGUOUS_UNITS), key=len, reverse=True)


def _num_of(head: str) -> float | None:
    """把数值头部转成 float，支持**千分位逗号**（`110,000 RPM`）与**分数**（`1/4 cup`）。"""
    s = head.strip().replace(",", "")
    if _NUM_RE.match(s):
        return float(s)
    m = _FRAC_RE.match(s)
    if m:
        d = float(m.group(2))
        return float(m.group(1)) / d if d else None
    return None


def quantity_of_unit(unit: str) -> str | None:
    u = str(unit or "").strip().lower()
    if u in AMBIGUOUS_UNITS:
        return "ambiguous"
    return UNIT_TO_QUANTITY.get(u)


def units_of_quantity(q: str) -> list[str]:
    return sorted(QUANTITY_UNITS.get(q, {}))


def unit_factors_for(quantity: str, target_unit: str = "",
                     case_variants: bool = True) -> dict[str, float]:
    """给 `rule_numeric` 用的 `unit_factors`：**只含该物理量**的单位，系数相对 `target_unit`。

    为什么不能沿用一张扁平大表：`rule_numeric` 的算法是 `val = n * factors[u]`
    （`std_rules.py` L140），即所有系数必须相对**同一个目标单位**。旧表把
    长度(→cm)/体积(→ml)/功率(→W) 混在一张表里，再加上
    `"m/s": 1.0, "km/h": 1.0, "mph": 1.0` 全是 1.0（假换算），
    于是 `246MPH` 会被算成 246（当成 m/s）—— 数值直接错 2.2 倍。
    正确做法：按物理量取子表，系数 = `单位系数 / 目标单位系数`。
    目标单位不属于该物理量时，退回该物理量的基准单位。

    `case_variants=True`：额外输出大小写变体。**必需** —— 生产侧
    `std_rules._parse_num_unit` 是 `^([\\d.]+)\\s*([A-Za-z%μ°Ω]+)$`，**大小写敏感**，
    `246MPH` 取出的单位是 `MPH` 而不是 `mph`，只给一种写法会直接查不到。
    （注意该正则**不认 `/`、`³`、中文单位** —— `100m/s`/`m³/h`/`厘米` 一律解析失败，
    这是生产 `rule_numeric` 的另一个缺陷，用 `pilot_numeric` 绕开。）
    """
    us = QUANTITY_UNITS.get(quantity)
    if not us:
        return {}
    tu = str(target_unit or "").strip().lower()
    base = us.get(tu)
    if base is None:
        base = us[QUANTITY_BASE.get(quantity, "")] if QUANTITY_BASE.get(quantity) in us else 1.0
    out: dict[str, float] = {}
    for u, f in us.items():
        f2 = round(f / base, 8)
        if not case_variants:
            out[u] = f2
            continue
        for v in {u, u.lower(), u.upper(), u.capitalize()}:
            out.setdefault(v, f2)
    return out


# ------------------------------------------------------------------ 解析
_PAREN_TAIL = re.compile(r"\s*[（(]([^（()）]*)[）)]\s*$")


def _strip_conversion_paren(s: str) -> str:
    """剥掉尾部的「换算注释」括号：`7英寸（约17.78cm）` → `7英寸`。

    中文商品资料常见「7英寸（约17.78cm）」这种"数值+换算注释"写法；
    只有括号里同时含**数字**和**已知单位**（换算注释特征）才剥，
    `3件套(含2个配件)` 这类不含单位的括号不动。
    """
    m = _PAREN_TAIL.search(s)
    if not m:
        return s
    inner = m.group(1).lower()
    if re.search(r"\d", inner) and any(u in inner for u in _ALL_UNITS):
        return s[:m.start()].strip()
    return s


def parse_measure(v: Any, prefer: str | None = None,
                  ) -> tuple[float, str, str] | None:
    """解析"数值 + 单位" → `(数值, 物理量, 单位名)`；解析不出返回 None。

    `prefer`: 遇到歧义单位（`oz`）时指定物理量；不指定则该值判为 `ambiguous`。
    只接受**纯数字 + 单位**（`246 MPH` / `100m/s` / `2000 CFM` / `55L` / `7ft`），
    **不接受** `12 x 8.7 x 0.5 inches` 这类多维串（那走尺寸归一）。
    """
    s = normalize_form(v).strip()
    return _parse_measure_normalized(s, prefer)


@lru_cache(maxsize=32768)
def _parse_measure_normalized(s: str, prefer: str | None):
    if not s:
        return None
    # 先试原串，再试"去掉尾部换算注释括号"的串（`7英寸（约17.78cm）` → `7英寸`）。
    candidates = [s]
    stripped = _strip_conversion_paren(s)
    if stripped and stripped != s:
        candidates.append(stripped)
    for cand in candidates:
        low = cand.lower()
        for u in _ALL_UNITS:
            if not low.endswith(u):
                continue
            head = low[: -len(u)].strip()
            n = _num_of(head)
            if n is None:
                continue
            q = quantity_of_unit(u)
            if q == "ambiguous":
                q = prefer if prefer in AMBIGUOUS_UNITS.get(u, ()) else "ambiguous"
            return (n, q, u)
    # 尾缀重试：`3件套` → `3件` → 3（件）
    for cand in candidates:
        low = cand.lower()
        for mod in _TAIL_MODIFIERS:
            if low.endswith(mod):
                inner = parse_measure(cand[: len(cand) - len(mod)], prefer=prefer)
                if inner is not None:
                    return inner
    return None


def convert(value: float, from_unit: str, to_unit: str) -> float | None:
    """同物理量内换算；跨物理量或不认识的单位 → None（**绝不猜**）。"""
    fq = quantity_of_unit(from_unit)
    tq = quantity_of_unit(to_unit)
    if not fq or not tq or fq != tq or fq == "ambiguous":
        return None
    fu = QUANTITY_UNITS[fq].get(str(from_unit).strip().lower())
    tu = QUANTITY_UNITS[tq].get(str(to_unit).strip().lower())
    if fu is None or tu is None:
        return None
    return value * fu / tu


# ------------------------------------------------------------------ 计量判定
def is_measure_like(values: Sequence[Any], ratio: float = 0.5,
                    prefer: str | None = None) -> bool:
    """取值大多是**带明确单位的数值** → 属计量值，不该做 enum 聚类（0-8）。

    与旧实现的差别：旧版只认长度单位（`_UNIT_TO_CM`），所以 `容量`(L)、
    `最大风速`(mph/CFM) 逃过路由、被当枚举聚类。本版按物理量全表判定。
    仍然**不认无单位数字**（否则「3件套」「3档」会被当计量值 —— 实测误判）。

    ⚠ **多维尺寸串要单独判**（见 `looks_multi_dim`）：纯三维串（`13.5x6.5x15.49 cm`）
    本函数判 False（整串解析不出"数值+单位"），但**混合属性**（多数 `8英寸` + 少数三维串）
    会因"多数可解析"而判 True → 被声明 `numeric` → 那少数三维串全部落"非数值"
    （实测 Salt&Pepper `高度/尺寸` **261 格**）。
    """
    vals = [str(v).strip() for v in values if str(v).strip()]
    if not vals:
        return False
    ok = sum(1 for v in vals if parse_measure(v, prefer=prefer) is not None)
    return ok / len(vals) >= ratio


_DIM_SPLIT_RE = re.compile(r"[x×*]")


def looks_multi_dim(values: Sequence[Any], ratio: float = 0.5) -> bool:
    """取值多为**多维串**（`13.5x6.5x15.49 cm` / `38.1x25.4x缺 cm`）→ 是**尺寸**，不是单值计量。

    为什么必须与 `is_measure_like` 分开：`rule_numeric` 只能容纳**一个数值**，
    三维串进去必然解析失败 → 实测 Salt&Pepper `高度/尺寸` **261 格**全部落成"非数值"。
    而该属性**只有约 19% 的取值是多维**（多数是 `8英寸`），所以 `is_measure_like` 仍判 True。
    用 `ratio`（默认 0.5，生产路由用 0.15）拦住这类**混合属性**。
    正确做法：这类属性走**尺寸处理**（保留原串 / 拆多维列 / Q3 补缺），不声明 numeric 单值型。
    """
    vals = [str(v).strip() for v in values if str(v).strip()]
    if not vals:
        return False
    ok = 0
    for v in vals:
        if "缺" in v or "<缺键>" in v:
            ok += 1                      # 含缺的多维串也算尺寸（Q3 的补数目标）
            continue
        toks = [t for t in _DIM_SPLIT_RE.split(v) if t.strip()]
        if len(toks) >= 2 and any(parse_measure(t) is not None for t in toks):
            ok += 1
    return ok / len(vals) >= ratio


def mixed_quantity_report(values: Sequence[Any], min_share: float = 0.15,
                          prefer: str | None = None) -> dict[str, Any]:
    """混量检测（0-9）：同一属性里是否出现 ≥2 个**物理量**（各占 ≥min_share）。

    这是"结构性错误属性"的体检 —— 一个属性只能有一个物理量，混了就必须
    拆属性或声明主口径，**不是归一能解决的**。
    实测 `最大风速`：`mph`/`m/s`/`FPM`(风速) 与 `CFM`(风量) 混在一起。
    """
    parsed = [parse_measure(v, prefer=prefer) for v in values]
    parsed = [p for p in parsed if p is not None]
    cnt = Counter(p[1] for p in parsed)
    total = sum(cnt.values())
    if not total:
        return {"能解析": 0, "物理量": {}, "混量": False, "主口径": None, "说明": "无带单位取值",
                "少数物理量": {}}
    shares = {q: round(n / total, 3) for q, n in cnt.items()}
    major = [q for q, s in shares.items() if s >= min_share and q != "ambiguous"]
    mixed = len(major) >= 2
    dom = cnt.most_common(1)[0][0]
    minority = {q: n for q, n in cnt.items() if q != dom}
    return {
        "能解析": total, "取值总数": len(values), "物理量": dict(cnt),
        "占比": shares, "混量": mixed, "主口径": dom, "少数物理量": minority,
        "说明": ("属性内混了 %d 个物理量(%s) → 需拆属性或声明主口径"
                 % (len(major), "/".join(sorted(major)))) if mixed
                else "单一物理量(%s)" % dom,
    }


def dominant_quantity(values: Sequence[Any], prefer: str | None = None) -> str | None:
    r = mixed_quantity_report(values, prefer=prefer)
    return r.get("主口径")


def pilot_numeric(v: Any, target_unit: str, bands: Sequence | None = None,
                  quantity: str | None = None, prefer: str | None = None,
                  zero_issue: bool = False, zero_suffix: str = "",
                  ) -> tuple[dict, str]:
    """**units 版** numeric 归一：用 `parse_measure` 解析 → `convert` 换到目标单位。

    语义与生产 `rule_numeric` 对齐（含 `zero_issue` 异常值闸门与 `bands` 分档）：

    | 生产 `rule_numeric` 行为 | 本函数 |
    |---|---|
    | 空/哨兵 → `{子列: 未明确}`, ST_UNKNOWN | ✅ |
    | 解析失败 → 原值 + ST_REVIEW | ✅ 返回 `({}, ST_REVIEW)` |
    | `val = n * factors[u]`, 取整 | ✅ `convert(n, u, 目标单位)` 取整 |
    | `zero_issue` 且值为 0 → 0 + 待复核 | ✅ |
    | `bands` 命中 → 档位标签, 否则 `未明确` | ✅ |

    存在原因：生产 `_parse_num_unit` 的单位正则只认纯字母，
    `100m/s`（含 `/`）、`m³/h`（含 `³`）、`厘米`（中文）全部落到 ST_REVIEW。
    **TODO（阶段 2）**：把本函数回填进 `std_rules.rule_numeric`。
    """
    s = normalize_form(v).strip()
    if not s:
        return {}, "未明确—无证据"
    pm = parse_measure(s, prefer=prefer)
    if pm is None:
        return {}, "待人工复核—语义无法判断"
    n, q, u = pm
    if quantity and q not in (quantity, "ambiguous"):
        return {}, "待人工复核—语义无法判断"          # 非主口径物理量 → 交人裁定
    tgt = str(target_unit or "").strip().lower() or QUANTITY_BASE.get(q, u)
    val = convert(n, u, tgt)
    if val is None:
        return {}, "待人工复核—语义无法判断"
    ival = int(round(val))
    if zero_issue and ival == 0:
        return {"数值": 0, "档位": "未明确"}, "待人工复核—语义无法判断"
    out = {"数值": ival}
    if bands:
        for lo, hi, label in bands:
            if (lo is None or ival > lo) and (hi is None or ival <= hi):
                out["档位"] = label
                break
        out.setdefault("档位", "未明确")
    return out, "已单位转换"
