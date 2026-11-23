# -*- coding: utf-8 -*-
"""厚度精确值重提取(文字 + 图片视觉 + OCR) —— 输出「单块板体厚度(cm)」。

背景
====
交付表 `18_标准化/<cat>-属性标注-标准化交付.xlsx` 的「厚度」列此前只有**分档区间**
(`超薄(≤3mm)` / `标准(约10-15mm)` / `加厚(约20-45mm)` / `超厚(≥45mm)`): 步骤16 的属性说明
就把「典型取值」写成了这四档, 步骤17 的模型照着档位回, 步骤18 再按 enum 词表收敛 ——
区间是从提示词一路带下来的。用户要的是**单块板体的精确厚度(cm)**。

三层证据(实测 2466 行)
======================
1. **文本显式陈述**(约 280 行): 标题/五点里 `1mm thickness`、`thickness of 6mm`、
   `0.31 inches thick`。这是卖家对**单块**板体的直接陈述, 优先级最高。
2. **结构化尺寸**: `详细参数` 的 `Product Dimensions:15"L x 12"W x 0.4"Th`(Th 标签),
   以及 `商品尺寸`(亚马逊 Product Dimensions 的高)。
   —— 两个坑:
     · **标签错位**: 实测 `10"L x 0.6"W x 15"Th` 把 15" 标成了厚度(物理上不可能),
       所以 Th 标签值必须过"物理合理性"闸门, 并同时看三元组的**最小维**。
     · **套装整叠**: 套装商品的这一位常常量的是**整叠高度**。实测 Elihome 12 件套
       PD 写 `3"Th` 而五点写明单块 6mm; Zubebe 12 件套 PD 写 `4.13"Th` 而单块 `0.31"`;
       Zubebe 的 `商品尺寸` 也是整叠(4.1"), 而 Elihome 的 `商品尺寸` 却是单块(0.2")。
       **同名字段在不同 listing 里口径不同**, 所以只能靠文本/图片定案。
3. **图片视觉 + OCR**: 尺寸标注图/规格表上的 `0.6in`、侧视图、单块与整叠的对比图。
   文本给不出显式陈述、或文本自相矛盾时, 靠这一层定案。

套装判定(用户 2026-09-24 明确要求)
==================================
`套装数量 > 1` 时, 结构化尺寸很可能是**整叠之和**, 必须核实:
  · 文本/图片能给出**单块**厚度 → 用单块值;
  · 图片确认那一维就是整叠、且给不出单块 → **不把整叠高度当成厚度**, 记 `未明确`,
    并在「来源」里写明"套装 N 件: 结构化尺寸 X cm 为整叠高度, 单块厚度未明确";
  · 只是"疑为整叠"(图片未确认) → 保留结构化数值但标**待复核**。

单位: 一律输出 `cm`(1 inch = 2.54 cm, 1 mm = 0.1 cm), 写法与步骤18 numeric 归一
(`normalize_measure`)**一致** —— `%g` + `cm` 后缀, 如 `1.524cm`、`0.6cm`。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

from .text_llm import quote_in_text, run_batches
from .vision_json import unwrap_vision

__all__ = [
    "UNIT_TO_CM", "parse_number", "to_cm", "format_cm", "is_plausible",
    "set_count_of", "explicit_statements", "product_dimensions", "size_min_cm",
    "measure_tokens", "text_evidence", "text_verdict",
    "build_thickness_prompt", "THICKNESS_VISION_CORE", "decide", "ThicknessRefiner",
]

# ------------------------------------------------------------------ 单位与数值
UNIT_TO_CM: dict[str, float] = {
    "mm": 0.1, "毫米": 0.1,
    "cm": 1.0, "厘米": 1.0,
    "m": 100.0, "米": 100.0,
    "inch": 2.54, "inches": 2.54, "in": 2.54, '"': 2.54, "\u201d": 2.54,
    "\u2033": 2.54, "\u201c": 2.54, "''": 2.54, "英寸": 2.54, "吋": 2.54,
    "ft": 30.48, "英尺": 30.48,
}

_CM = (r'(?:mm|cm|m|inch|inches|in|"|\u201d|\u2033|\u201c|\u82f1\u5bf8|\u540b'
       r'|\u5398\u7c73|\u6beb\u7c73|\u7c73|ft|\u82f1\u5c3a)')
_NUM = r'(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)'

# 显式厚度陈述: 「N 单位 thick/thickness/profile/gauge」
# `profile` 必须收 —— 实测 `durable 0.8-inch profile`(B00D3BAD7B) 就是"单块厚度 0.8 英寸",
# 早期只认 thick/thickness 把这一行漏成了整叠高度 6.35cm。
_THICK_WORD = r'(?:thick|thickness|profile|gauge)'
RE_EXPL_A = re.compile(r'(?<![\d.])(' + _NUM + r')\s*(' + _CM + r')\s*(?:-|\s)?' + _THICK_WORD + r'\b', re.I)
RE_EXPL_CH = re.compile(r'(?<![\d.])(?:thick|thickness|profile|gauge)\b[^.\d]{0,24}?(' + _NUM + r')\s*(' + _CM + r')', re.I)
# 中文/带标签: 「厚度: 1.5cm」「Thickness: 0.6 inch」
RE_EXPL_LBL = re.compile(r'(?:\u539a\u5ea6|thickness)\s*[:\uff1a]?\s*(?:of\s+)?(' + _NUM + r')\s*(' + _CM + r')', re.I)
# 区间: 「0.5-0.7 inch thick」
RE_RANGE = re.compile(r'(' + _NUM + r')\s*(?:-|~|\uff5e|\u2013|\u2014)\s*(' + _NUM + r')\s*(' + _CM + r')?\s*(?:thick|thickness)\b', re.I)
# 限值表述(不算确切厚度)
RE_LIMIT = re.compile(r'(?:no\s+more\s+than|up\s+to|max(?:imum)?|at\s+least|min(?:imum)?|over|under'
                      r'|\u5c0f\u4e8e|\u5927\u4e8e|\u4e0d\u8d85\u8fc7)\s*$', re.I)

# ------------------------------------------------------------------ 文本 LLM 复核
TEXT_LLM_NAME = "厚度-文本复核.json"
TEXT_LLM_CKPT = "厚度-文本复核.checkpoint.json"
# 交给文本模型的字段(与视觉同一套原文, 但**只让它读文字**)
TEXT_LLM_FIELDS = ("标题", "亮点", "五点描述", "SKU", "详细参数", "商品尺寸", "商品尺寸（单位换算）")
THICK_TEXT_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
下面每行是一件砧板商品的**文字资料**。请只依据文字, 回答这一行**单块板体**的厚度。

硬性要求
1. 只报**单块板体**的厚度。若资料里给的数字是**整叠 / 整套 / 包装**的高度
   (例如 12 件套写 `4.13"Th`、`Set of 3 ... 2.4"H`), **不要**报那个数;
   若资料里另有单块厚度的说法, 报单块的; 确实没有就填 `未明确`。
2. 厚度常见写法: `0.6"` / `1.5mm` / `0.8-inch profile` / `3/8 inch thick` /
   `each board is 0.8 inch thick` / `厚度 2cm` / `Product Dimensions` 的 `Th` 位 /
   `XL size (17"x12"x1")` 里的第三个数。
3. **不要把长、宽当厚度**。`Product Dimensions` 的 `Th` 标签有时标错
   (如 `10"L x 0.6"W x 15"Th` 把 15" 标成厚度), 要结合数值大小判断 ——
   砧板单块厚度通常在 0.3mm~6cm 之间, 超出基本是读错了维度。
4. `原文` 必须是资料里的**原句片段, 照抄不要改写**(核验时会用它在该行资料里定位)。
5. 输出 **cm** 数值(1 inch = 2.54 cm, 1 mm = 0.1 cm), 最多 4 位小数。

输出严格 JSON(不要输出其它文字):
{"rows": [{"xl_row": <行号>, "单块厚度_cm": <数值 或 "未明确">,
           "原文": "<照抄的原文片段>", "是否整叠": "<是/否/无法判断>"}]}
"""


def build_thickness_text_prompt(batch: Sequence[Mapping[str, Any]], max_chars: int = 1400) -> str:
    """厚度「文本复核」批量提示词(纯文字, 一次多行)。"""
    out = [THICK_TEXT_CORE, "", "=" * 60, "## 本次待判定的行", ""]
    for r in batch:
        out.append("### xl_row=%s" % r.get("xl_row"))
        for c in TEXT_LLM_FIELDS:
            v = r.get(c)
            if not v:
                continue
            s = str(v)
            if len(s) > max_chars:
                s = s[:max_chars] + "…"
            out.append("- %s: %s" % (c, s))
        out.append("")
    return "\n".join(out)


def accept_thickness_text(rec: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[bool, str, Optional[float]]:
    """核验文本 LLM 的一条结果 → `(是否可用, 说明, cm)`。

    · `原文` 必须真的出现在该行资料里(挡幻觉);
    · `原文` 里的 数值+单位 必须能换算成合理 cm, 且与模型报的 `单块厚度_cm` 一致;
    · 模型自述"是整叠" → 不采用。
    """
    raw = rec.get("单块厚度_cm")
    if raw is None or (isinstance(raw, str) and ("未明确" in raw or not raw.strip())):
        return False, "未明确", None
    try:
        cm = float(str(raw).strip().rstrip("cm厘米"))
    except ValueError:
        m = RE_NUM_UNIT.search(str(raw))
        cm = to_cm(m.group(1), m.group(2)) if m else None
    if not is_plausible(cm):
        return False, "cm 不合理", None
    if str(rec.get("是否整叠") or "").strip() == "是":
        return False, "自述为整叠高度", None
    quote = rec.get("原文")
    if not quote_in_text(quote, [row.get(c) for c in TEXT_LLM_FIELDS]):
        return False, "原文核验未通过", None
    # 引用里的数字要与结论自洽(允许 8% 偏差, 因为可能四舍五入/取整)
    qcm = None
    m = RE_NUM_UNIT.search(str(quote))
    if m:
        qcm = to_cm(m.group(1), m.group(2))
    if qcm is not None and is_plausible(qcm) and abs(qcm - cm) / max(qcm, 1e-9) > 0.08:
        return False, "引用与数值不一致", None
    return True, "", round(cm, 4)

RE_PRODUCT_DIMS = re.compile(r'Product\s+Dimensions\s*:\s*([^|]+)', re.I)
RE_DIM_TOKEN = re.compile(
    r'(' + _NUM + r')\s*(' + _CM + r')?\s*["\u201d]?\s*(L|W|H|D|B|TH|T)\b', re.I)
RE_NUM_UNIT = re.compile(r'(' + _NUM + r')\s*(' + _CM + r')', re.I)
RE_NUM_UNIT_OPT = re.compile(r'(' + _NUM + r')\s*(' + _CM + r')?', re.I)

EXPLICIT_FIELDS = ("标题", "亮点", "五点描述", "SKU", "详细参数")
DIM_FIELDS = ("商品尺寸（单位换算）", "商品尺寸", "包装尺寸（单位换算）")

# 物理合理区间(单块板体厚度): 0.03cm(0.3mm 软垫) ~ 15cm(6in 厚砧板)
PLAUSIBLE_LO, PLAUSIBLE_HI = 0.03, 15.0
SAME_TOL = 0.12        # 同值判定容差(相对)


# 需要高清读图(读小字)的属性名线索: OCR 文字 / 雕刻文字 / 认证标记 / logo 等
# 视觉接口只吃图片: A+ 链接里混着 `.mp4` 视频 → 传进去会稳定 400 Bad Request
# (实测 Row 1506 等 400 行的 A+ 链接里就带 aplus-media/sc/xxx.mp4)。
_RE_NOT_IMAGE = re.compile(r'\.(?:mp4|m3u8|mov|webm|avi|mkv|flv|wmv|ts)(?:$|[?#])', re.I)


def _is_image_url(url: str) -> bool:
    """是否像个图片 URL(排除视频/其它非图片资源)。"""
    return not _RE_NOT_IMAGE.search(str(url or ""))


# ------------------------------------------------------------------ 基础换算
def parse_number(s: Any) -> Optional[float]:
    """解析数值: `1.5` / `1,5`(德语小数) / `1/2` / `1 1/2`。"""
    if s is None:
        return None
    t = str(s).strip().replace(",", ".")
    m = re.fullmatch(r'(\d+)\s+(\d+)\s*/\s*(\d+)', t)
    if m:
        d = float(m.group(3))
        return float(m.group(1)) + (float(m.group(2)) / d if d else 0.0)
    m = re.fullmatch(r'(\d+)\s*/\s*(\d+)', t)
    if m:
        d = float(m.group(2))
        return float(m.group(1)) / d if d else None
    try:
        return float(t)
    except ValueError:
        return None


def to_cm(value: Any, unit: Any) -> Optional[float]:
    """数值 + 单位 → cm; 单位缺失/未知 → None(**不猜单位**)。"""
    n = parse_number(value)
    if n is None:
        return None
    f = UNIT_TO_CM.get(str(unit or "").strip().lower())
    if f is None:
        return None
    return n * f


def is_plausible(cm: Optional[float]) -> bool:
    return cm is not None and PLAUSIBLE_LO <= cm <= PLAUSIBLE_HI


def format_cm(cm: float) -> str:
    """按步骤18 numeric 归一的写法输出(`%g` + `cm`), 如 `1.524cm`。"""
    return "%gcm" % round(float(cm), 4)


def _same(a: float, b: float) -> bool:
    return abs(a - b) / max(abs(a), abs(b), 1e-9) <= SAME_TOL


def _cluster(vals: Sequence[float]) -> list[float]:
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and _same(out[-1][0], v):
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def measure_tokens(text: Any) -> list[tuple[float, str]]:
    """把尺寸串拆成 `(数值, 单位)`。

    处理 `24 x 18 x 0.5 inches` 这种**只有组末带单位**的写法(亚马逊 `商品尺寸` 列常见):
    组内缺单位的按本串第一个出现的单位补齐。
    """
    s = str(text or "")
    if not s:
        return []
    toks = [(parse_number(m.group(1)), (m.group(2) or ""))
            for m in RE_NUM_UNIT_OPT.finditer(s)]
    toks = [t for t in toks if t[0] is not None]
    default_unit = next((u for _, u in toks if u), "")
    return [(n, u or default_unit) for n, u in toks]


# ------------------------------------------------------------ 套装件数(1=单件)
SET_COUNT_RE = [
    re.compile(r'set\s+of\s+(\d+)', re.I),
    re.compile(r'(\d+)\s*[- ]?\s*(?:pcs|pieces|piece|pack|pk|count|ct)\b', re.I),
    re.compile(r'pack\s+of\s+(\d+)', re.I),
    re.compile(r'(\d+)\s*\u4ef6\u5957'),
]


def set_count_of(row: Mapping[str, Any], declared: Any = None) -> tuple[Optional[int], str]:
    """推定**这一行是几件套**。返回 `(件数或 None, 依据)`。

    优先用交付表里已标注的「套装数量」(`declared`, 如 `3件套`/`1件`);
    没有或判不出时, 从标题/SKU 的 `Set of 3` / `12 Pcs` / `4-Piece` 兜底推。
    `None` = 判不出(按单件处理, 但结构化尺寸照旧要过图片)。
    """
    if declared not in (None, "", "未明确", "None"):
        s = str(declared)
        m = re.search(r'(\d+)', s)
        if m:
            n = int(m.group(1))
            if n >= 1:
                return n, "套装数量=%s" % s
    text = " ".join(str(row.get(c) or "") for c in ("标题", "SKU"))
    for rx in SET_COUNT_RE:
        m = rx.search(text)
        if m:
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 200:
                return n, "标题/SKU: %s" % m.group(0)
    return None, ""


# ------------------------------------------------------------------ 文本证据
def explicit_statements(row: Mapping[str, Any]) -> list[dict]:
    """文本里的**显式单块厚度陈述**(标题/五点/SKU/详细参数)。

    只认把厚度说死的写法(数字+单位+thick/thickness、厚度: N 单位);
    `extra thick`、`thick enough` 这种没数字的**不算**证据。
    区间(`0.5-0.7 inch thick`)标 `tier=explicit_range`, 交图片/仲裁定案。
    """
    out: list[dict] = []
    for field in EXPLICIT_FIELDS:
        text = str(row.get(field) or "")
        if not text:
            continue
        for rx in (RE_EXPL_LBL, RE_EXPL_A, RE_EXPL_CH):
            for m in rx.finditer(text):
                if RE_LIMIT.search(text[max(0, m.start() - 24):m.start()]):
                    continue
                cm = to_cm(m.group(1), m.group(2))
                if is_plausible(cm):
                    out.append({"cm": round(cm, 4), "tier": "explicit",
                                "field": field, "snippet": m.group(0).strip()})
                break   # 同一处文本不被多条正则重复记
        for m in RE_RANGE.finditer(text):
            a, b, unit = m.group(1), m.group(2), (m.group(3) or "")
            if not unit:
                continue
            ca, cb = to_cm(a, unit), to_cm(b, unit)
            if is_plausible(ca) and is_plausible(cb):
                out.append({"cm": round((ca + cb) / 2, 4), "tier": "explicit_range",
                            "field": field, "snippet": m.group(0).strip(),
                            "range": [round(ca, 4), round(cb, 4)]})
    return out


def product_dimensions(row: Mapping[str, Any]) -> tuple[Optional[float], Optional[float], str]:
    """`详细参数` 的 `Product Dimensions` → (Th 标签值 cm, 三元组最小维 cm, 原文)。

    标签不可信(实测 `10"L x 0.6"W x 15"Th` 把 15" 标成厚度), 所以同时给"最小维",
    由上层按物理合理性与套装件数取舍。
    """
    text = str(row.get("详细参数") or "")
    m = RE_PRODUCT_DIMS.search(text)
    if not m:
        return None, None, ""
    seg = m.group(1).strip()
    toks: list[tuple[float, str, str]] = []
    for num, unit, label in RE_DIM_TOKEN.findall(seg):
        n = parse_number(num)
        if n is not None:
            toks.append((n, (unit or ""), label))
    if len(toks) < 2:
        return None, None, seg
    default_unit = next((u for _, u, _ in toks if u), "")
    cms = [to_cm(n, u or default_unit) for n, u, _ in toks]
    if any(c is None for c in cms):
        return None, None, seg
    th_labeled = [c for (_, _, lab), c in zip(toks, cms) if lab.lower() in ("th", "t")]
    th = round(th_labeled[0], 4) if th_labeled else None
    return th, round(min(cms), 4), seg


def size_min_cm(row: Mapping[str, Any], col: str) -> Optional[float]:
    """某一列(商品尺寸/包装尺寸)里最小的那一维 → cm。"""
    cms = [to_cm(n, u) for n, u in measure_tokens(row.get(col))]
    cms = [c for c in cms if c is not None]
    if len(cms) < 2:
        return None
    return round(min(cms), 4)


def text_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """汇总一行的全部文本证据。"""
    expl = explicit_statements(row)
    pd_th, pd_min, pd_raw = product_dimensions(row)
    size_min = None
    for col in DIM_FIELDS:
        size_min = size_min_cm(row, col)
        if size_min is not None:
            break
    ev: list[dict] = list(expl)
    if is_plausible(pd_th):
        ev.append({"cm": pd_th, "tier": "pd_th", "field": "详细参数",
                   "snippet": "Product Dimensions: " + pd_raw[:80]})
    if is_plausible(pd_min):
        ev.append({"cm": pd_min, "tier": "pd_min", "field": "详细参数",
                   "snippet": "Product Dimensions 最小维: " + pd_raw[:80]})
    if is_plausible(size_min):
        ev.append({"cm": size_min, "tier": "size_min", "field": "商品尺寸（单位换算）",
                   "snippet": "商品尺寸最小维"})
    return {"evidence": ev, "explicit": expl, "pd_th": pd_th,
            "pd_min": pd_min, "size_min": size_min, "pd_raw": pd_raw}


def text_verdict(row: Mapping[str, Any]) -> dict[str, Any]:
    """文本侧结论。

    · `decided`   有**唯一**显式陈述 → 直接定案(不必看图)
    · `ambiguous` 显式陈述互相矛盾, 或只有区间 → 交图片视觉/仲裁
    · `none`      没有显式陈述 → 交图片视觉
    """
    info = text_evidence(row)
    expl = [e for e in info["evidence"] if e["tier"] == "explicit"]
    rng = [e for e in info["evidence"] if e["tier"] == "explicit_range"]
    clusters = _cluster([e["cm"] for e in expl]) if expl else []
    if len(clusters) == 1 and not rng:
        cm = clusters[0]
        best = min(expl, key=lambda e: abs(e["cm"] - cm))
        return {"status": "decided", "cm": round(cm, 4), "tier": "explicit",
                "source": "文本显式: %s「%s」" % (best["field"], best["snippet"]),
                "info": info}
    if clusters or rng:
        return {"status": "ambiguous", "cm": None, "tier": "explicit_multi",
                "source": "", "info": info}
    return {"status": "none", "cm": None, "tier": "", "source": "", "info": info}


# ------------------------------------------------------------------ 图片视觉
THICKNESS_VISION_CORE = """你是亚马逊砧板类目(Cutting Boards)的产品数据审校员。
任务：读图 + 读文本，给出**这一行商品「单块板体」的厚度，单位 cm**。

判定规则(逐条遵守)
1. **只认单块板体的厚度**。如果本行是**套装/多件**(件数见下方"套装数量")，
   图上量到的很可能是**整叠高度**（例如 12 块叠在一起的总高）—— 那不是厚度，
   **不要采用**；图片里若能看到单独一块的厚度标注，那才是答案。
2. 优先读取图片里的**尺寸标注图 / 规格表 / 参数小字(OCR)**：`0.6in`、`1.5cm`、
   `Thickness: 0.8 inch`、`10mm`、`1/2 inch`。看清数字与单位再报。
3. **区分长/宽/厚**：标注通常是 `长 x 宽 x 厚` 或 `L x W x H`。厚度一般是**最小的那一维**，
   但不要把宽度、高度、手柄长度当成厚度。
4. 图片与文本冲突时：文本里若有"**单块**厚度"的明确陈述（`6mm thickness` /
   `0.31 inches thick` / `thickness of 6mm`），以文本为准；否则以图片里读到的数字为准。
5. 单位换算：1 inch = 2.54 cm，1 mm = 0.1 cm。输出保留 2~4 位有效数字的 cm 数值。
6. 看不清、图上没有厚度信息、或只有"加厚/超厚"这类形容词 → 填 `未明确`；**不要猜**。
   合理范围 0.03~15 cm，超出基本是读错了维度。
7. `依据` 写清到底看到了什么(第几张图、图上原文数字、或文本原句)；
   `是否整叠高度` 明确回答：你给出的这个数(或唯一可见的那个厚度方向的量)是不是**整叠**高度。
"""

_THICK_OUT_TPL = {
    "xl_row": None,
    "厚度_cm": "<单块厚度数值(不加单位) 或 \"未明确\">",
    "依据": "<图片OCR/图片视觉读到的原文数字 + 文本原句>",
    "看到的单位与原始写法": "<如 0.6in / 6mm / 未见到>",
    "图片是否读到厚度数值": "<是/否（只有真的从图片/图上小字读到厚度才填 是）>",
    "是否整叠高度": "<是/否/无法判断>",
}
# 视觉返回里我们真正要读的字段(用于 vision_json 剥壳时定位)
_WANT_KEYS = tuple(_THICK_OUT_TPL)


def build_thickness_prompt(row: Mapping[str, Any], *, n_images: int = 0, notes: str = "",
                           max_chars: int = 1200) -> str:
    """厚度专用的视觉/OCR 提示词(一行一调, 图片 URL 直传)。"""
    out = [THICKNESS_VISION_CORE.rstrip(), "", "=" * 60]
    if notes:
        out += [notes.strip(), "=" * 60]
    if n_images:
        out += ["", "## 随附图片", "",
                "共 %d 张，均为**本行商品**的图片（主图在前，其后是 A+ 图文）。" % n_images]
    ev = []
    for col in ("标题", "亮点", "五点描述", "SKU", "商品尺寸", "商品重量（单位换算）"):
        v = row.get(col)
        if not v:
            continue
        s = str(v)
        if len(s) > max_chars:
            s = s[:max_chars] + "…"
        ev.append("- **%s**：%s" % (col, s))
    if ev:
        out += ["", "## 本行文本资料（用于互证；**不要**照抄长度/宽度当厚度）", ""] + ev
    out += [
        "", "## 输出格式（严格 JSON，不要输出其它文字）", "```json",
        json.dumps(_THICK_OUT_TPL, ensure_ascii=False, indent=2),
        "```", "",
        "`厚度_cm` 只填数字（不带单位）或字符串 \"未明确\"。",
    ]
    return "\n".join(out)


def _vision_value(rec: Mapping[str, Any]) -> Optional[float]:
    """从视觉返回里取出可用的 cm 数值(只认真正的数字)。"""
    raw = rec.get("厚度_cm")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        cm = float(raw)
    else:
        s = str(raw).strip()
        if not s or "未明确" in s or s.lower() in ("none", "null", "n/a", "unknown"):
            return None
        m = RE_NUM_UNIT.search(s)
        if m:
            cm = to_cm(m.group(1), m.group(2))
        else:
            m2 = re.search(r'(' + _NUM + r')', s)
            cm = parse_number(m2.group(1)) if m2 else None
    return cm if is_plausible(cm) else None


# ------------------------------------------------------------------ 定案
def decide(row: Mapping[str, Any], vision_rec: Optional[Mapping[str, Any]] = None,
           set_count: Optional[int] = None, set_label: str = "",
           text_rec: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """文本 + 文本LLM复核 + 图片 + 套装件数 → 最终结论。

    优先级:
      1. 文本**正则显式**单块陈述唯一              → 采用(不必看图)
      2. **文本LLM复核**(原文引用已核验)给出单块值  → 采用
      3. **图片里真的读到**单块厚度数值             → 采用(依据=图片原文)
      4. 结构化尺寸, 且不是套装整叠                → 采用(标待复核)
      5. 套装整叠被图片确认、又拿不到单块值         → `未明确` + 来源写明整叠高度与件数
      6. 其余 → `未明确`

    返回 `{value, cm, source, tier, review, evidence}`; `value=None` 表示未明确。

    说明: 视觉模型若只是"复述文本里的 Product Dimensions", **不算图片证据**
    (`图片是否读到厚度数值=否`), 此时走结构化尺寸分支 —— 免得把文本值伪装成图片读数。
    """
    tv = text_verdict(row)
    info = tv["info"]
    expl = [e for e in info["evidence"] if e["tier"].startswith("explicit")]
    is_set = bool(set_count and set_count > 1)
    label = str(set_label or "").strip()
    if label in ("None", "nan", "未明确"):
        label = ""
    if not label and set_count:
        label = "%d 件(标题/SKU 推定)" % set_count
    set_note = ("本行是**套装**(%s)" % label) if is_set else ""

    # 文本 LLM 复核(已过原文核验)
    t_cm = None
    t_quote = ""
    if text_rec and text_rec.get("ok"):
        try:
            t_cm = float(text_rec.get("cm"))
        except (TypeError, ValueError):
            t_cm = None
        t_quote = str(text_rec.get("原文") or "").strip()
        if not is_plausible(t_cm):
            t_cm = None

    # 视觉返回有时被包一层({"type":.., "response":{..}} 或 {"content": "<json>"}) → 先剥壳
    vision_rec = unwrap_vision(vision_rec, _WANT_KEYS)
    if not isinstance(vision_rec, Mapping) or not any(k in vision_rec for k in _WANT_KEYS):
        vision_rec = None
    v_cm = _vision_value(vision_rec or {})
    v_note = img_read = stack = ""
    if vision_rec:
        v_note = str(vision_rec.get("依据") or "").strip()
        unit_raw = str(vision_rec.get("看到的单位与原始写法") or "").strip()
        if unit_raw and unit_raw != "未见到":
            v_note = (v_note + "（" + unit_raw + "）").strip()
        img_read = str(vision_rec.get("图片是否读到厚度数值") or "").strip()
        stack = str(vision_rec.get("是否整叠高度") or "").strip()
        if img_read not in ("是", "否"):
            img_read = "是" if (re.search(r'图\s*\d|图片|OCR|标注图', v_note)
                                and not re.search(r'图片(中)?(未|没)(见到|有)', v_note)) else "否"

    # ① 文本显式(正则)
    if tv["status"] == "decided":
        return {"value": format_cm(tv["cm"]), "cm": tv["cm"], "tier": "explicit",
                "source": tv["source"], "review": False, "evidence": info["evidence"]}

    # ② 文本 LLM 复核(原文已核验)
    if t_cm is not None:
        src = "文本复核: 单块厚度 %s，原文「%s」" % (format_cm(t_cm), t_quote[:90])
        if is_set:
            src += "（%s：已取单块值, 非整叠）" % (label or "套装")
        suspect = False
        if v_cm is not None and img_read == "是" and not _same(t_cm, v_cm):
            src += "；图片读数为 %s(取文本值)" % format_cm(v_cm)
            suspect = True
        return {"value": format_cm(t_cm), "cm": round(t_cm, 4), "tier": "text_llm",
                "source": src[:600], "review": suspect, "evidence": info["evidence"]}

    # ③ 图片真的读到单块厚度
    if v_cm is not None and img_read == "是":
        src = "图片视觉/OCR: " + (v_note or "图片读出厚度")
        if expl:
            others = "、".join(sorted({format_cm(e["cm"]) for e in expl}))
            src += "；已否文本互斥陈述(%s)" % others
        suspect = (stack == "是")
        if suspect:
            src += " [模型另注该厚度方向疑为整叠高度, 待复核]"
        return {"value": format_cm(v_cm), "cm": round(v_cm, 4), "tier": "vision",
                "source": src[:600], "review": suspect, "evidence": info["evidence"]}

    pd_th, pd_min, size_min = info["pd_th"], info["pd_min"], info["size_min"]
    # 套装: 结构化尺寸极可能是整叠 —— 图片确认是整叠时**绝不**当厚度用
    if is_set and stack == "是":
        total = pd_th if is_plausible(pd_th) else (pd_min if is_plausible(pd_min) else size_min)
        detail = ("结构化尺寸 %s 为整叠高度" % format_cm(total)) if is_plausible(total) \
            else "结构化尺寸亦不可用"
        return {"value": None, "cm": None, "tier": "set_stack",
                "source": ("未明确（%s；%s，单块厚度未明确）%s"
                           % (set_note, detail, ("；" + v_note[:200]) if v_note else ""))[:600],
                "review": True, "evidence": info["evidence"]}

    warn = ""
    if is_set:
        warn = " [%s，结构化尺寸疑为整叠高度，待复核]" % (label or "套装")
    if stack == "是":
        warn = " [图片判为整叠高度]"
    elif vision_rec and vision_rec.get("厚度_cm") in (None, "", "未明确"):
        warn += " [图片未见厚度标注]"
    if warn and v_note:
        warn += " " + v_note[:200]

    if is_plausible(pd_th) and is_plausible(size_min) and _same(pd_th, size_min):
        return {"value": format_cm(pd_th), "cm": pd_th, "tier": "pd_consensus",
                "source": ("结构化尺寸: Product Dimensions Th=%s 与 商品尺寸最小维一致%s"
                           % (format_cm(pd_th), warn))[:600],
                "review": True, "evidence": info["evidence"]}
    if is_plausible(pd_th):
        return {"value": format_cm(pd_th), "cm": pd_th, "tier": "pd_th",
                "source": ("结构化尺寸: Product Dimensions Th=%s%s"
                           % (format_cm(pd_th), warn))[:600],
                "review": True, "evidence": info["evidence"]}
    if is_plausible(pd_min):
        return {"value": format_cm(pd_min), "cm": pd_min, "tier": "pd_min",
                "source": ("结构化尺寸: Product Dimensions 最小维=%s%s"
                           % (format_cm(pd_min), warn))[:600],
                "review": True, "evidence": info["evidence"]}
    if is_plausible(size_min):
        return {"value": format_cm(size_min), "cm": size_min, "tier": "size_min",
                "source": ("结构化尺寸: 商品尺寸最小维=%s%s"
                           % (format_cm(size_min), warn))[:600],
                "review": True, "evidence": info["evidence"]}
    return {"value": None, "cm": None, "tier": "unknown",
            "source": ("未明确" + warn)[:600], "review": True, "evidence": info["evidence"]}


def _vision_with_fallback(ctx, prompt: str, urls: list[str], *, detail: str,
                          max_tokens: int):
    """调视觉接口; 全量图片失败时**退化为前 2 张再试一次**。

    为什么需要: A+ 链接里可能混入非图片资源(如 `.mp4`, 已在 `_is_image_url` 过滤),
    或某一张图过大/不可达 → 接口稳定返回 400。此时整行会被判失败并回落文本证据,
    代价是白丢一次图片证据; 只用前 2 张(主图 + 首张详情图)往往还能拿到结论。
    返回 `(结果, 错误说明)`; 成功时错误说明为 ""。
    """
    attempts = [urls]
    if len(urls) > 2:
        attempts.append(urls[:2])
        attempts.append(urls[:1])
    last = ""
    for i, us in enumerate(attempts):
        try:
            return ctx.llm().vision(prompt, us, temperature=0.2, max_tokens=max_tokens,
                                    detail=detail, max_images=None), ""
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            if i + 1 < len(attempts):
                last += " → 退化为前 %d 张重试" % len(attempts[i + 1])
    return None, last


# ------------------------------------------------------------------ 编排
class ThicknessRefiner:
    """跑一遍「厚度精确值」重提取: 文本 → (需要的行)图片视觉/OCR → 落盘。

    · 并发数 = `THICKNESS_WORKERS`(默认取 `ATTR_STAB_WORKERS`, 再默认 6);
    · 行级断点: 每完成一行写 `厚度-精确值.checkpoint.json`, 中断重启只补剩余;
    · 单行失败隔离: 失败行 1 行/次串行重试, 最多 3 轮。
    """

    OUT_NAME = "厚度-精确值.json"
    CKPT_NAME = "厚度-精确值.checkpoint.json"

    def __init__(self, ctx, *, log=None):
        self.ctx = ctx
        self._log = log or (lambda msg: None)
        cfg = ctx.config
        self.workers = int(cfg.get("THICKNESS_WORKERS") or cfg.get("ATTR_STAB_WORKERS") or 6)
        self.detail = str(cfg.get("THICKNESS_VISION_DETAIL") or "high").strip().lower()
        self.max_images = int(cfg.get("THICKNESS_MAX_IMAGES") or 5)
        self.aplus_images = int(cfg.get("THICKNESS_APLUS_IMAGES") or 3)
        self.force_vision = str(cfg.get("THICKNESS_FORCE_VISION") or "").strip().lower() \
            in ("1", "true", "yes", "on")
        self.text_batch = int(cfg.get("THICKNESS_TEXT_BATCH") or 8)
        self.set_counts: dict[int, int] = {}
        self.set_labels: dict[int, str] = {}
        # {xl_row: {cm, 原文, ok, why}} —— 文本 LLM 复核结果(纯文字层)
        self.text_recs: dict[int, dict] = {}

    # ------------------------------------------------------------ 文本复核
    def text_pass(self, rows: Sequence[Mapping[str, Any]], out_dir: str, *,
                  only_xls: Optional[set] = None, batch: int = 0, workers: int = 0) -> dict:
        """**文本 LLM 复核**: 纯文字批量找「单块厚度」, 并要求给出**原文引用**。

        为什么要这一层: 正则只会越补越漏(`0.8-inch profile`、`each board is 0.8 inch thick`、
        `XL size (17"x12"x1")` 第三个数…)。而文本层又比"结构化尺寸"可靠 ——
        套装的 `Product Dimensions` 那一维常是整叠高度。所以让模型读文字,
        再用**引用核验**(原文必须真的出现在该行资料里)把幻觉挡掉。
        """
        batch = batch or self.text_batch
        workers = workers or self.workers
        items = []
        by_key: dict[str, Mapping[str, Any]] = {}
        for r in rows:
            try:
                xl = int(r.get("xl_row"))
            except (TypeError, ValueError):
                continue
            if only_xls is not None and xl not in only_xls:
                continue
            by_key[str(xl)] = r
            items.append(r)
        if not items:
            return dict(self.text_recs)

        def _accept(rec: Mapping[str, Any]) -> bool:
            row = by_key.get(str(rec.get("xl_row")))
            if row is None:
                return False
            ok, why, cm = accept_thickness_text(rec, row)
            rec["_ok"] = bool(ok)
            rec["_why"] = why
            rec["_cm"] = cm
            return True     # 一律收下(未明确/核验不过的也留档), 由 decide 判断

        recs = run_batches(
            self.ctx, items, build_thickness_text_prompt, accept=_accept,
            key_of=lambda r: str(r.get("xl_row")),
            batch=batch, workers=workers,
            log=self._log, label="厚度文本复核",
            ckpt_path=os.path.join(out_dir, TEXT_LLM_CKPT),
            plan_key="%d|%d" % (len(items), batch))
        for k, rec in recs.items():
            try:
                xl = int(k)
            except (TypeError, ValueError):
                continue
            self.text_recs[xl] = {
                "cm": rec.get("_cm"), "原文": str(rec.get("原文") or ""),
                "是否整叠": str(rec.get("是否整叠") or ""),
                "ok": bool(rec.get("_ok")), "why": str(rec.get("_why") or ""),
                "原始": rec.get("单块厚度_cm"),
            }
        ok_n = sum(1 for v in self.text_recs.values() if v.get("ok"))
        self._log("厚度文本复核完成: %d 行有结论(其中经原文核验可用 %d 行)"
                  % (len(self.text_recs), ok_n))
        path = os.path.join(out_dir, TEXT_LLM_NAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cat": getattr(self.ctx.config, "cat", ""),
                       "rows": {str(k): v for k, v in self.text_recs.items()}},
                      fh, ensure_ascii=False)
        os.replace(tmp, path)
        return dict(self.text_recs)

    def load_text_recs(self, out_dir: str) -> dict:
        """读回文本复核结果(供 `redecide` 复用, 不重调模型)。"""
        path = os.path.join(out_dir, TEXT_LLM_NAME)
        if not os.path.exists(path):
            return {}
        try:
            d = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for k, v in (d.get("rows") or {}).items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
        self.text_recs = out
        return out

    # ---- 图片 URL: 详情图在前, A+ 图文在后
    @staticmethod
    def _split_urls(raw: Any) -> list[str]:
        # 分隔符兼顾 `,` 与 `;`：step10 产出用 `;`，step14 统一成 `,`（见改动清单 §19）
        s = str(raw or "").strip().strip('"\'')
        return [u.strip().strip('"\'') for u in re.split(r"[|;,\n]+", s) if u.strip()]

    @classmethod
    def row_image_urls(cls, row: Mapping[str, Any], max_main: int, max_aplus: int) -> list[str]:
        urls: list[str] = []
        for col, cap in (("图片链接", max_main), ("A+图片链接", max_aplus),
                         ("主图", 1), ("商品主图", 0)):
            if cap <= 0:
                continue
            for u in cls._split_urls(row.get(col))[:cap]:
                u = re.sub(r"\._[A-Za-z0-9,]+(?:_[A-Za-z0-9,]+)*_\.", ".", u)
                if u.startswith("http") and _is_image_url(u) and u not in urls:
                    urls.append(u)
        return urls

    def set_count(self, row: Mapping[str, Any]) -> tuple[Optional[int], str]:
        xl = row.get("xl_row")
        try:
            xl = int(xl)
        except (TypeError, ValueError):
            xl = None
        declared = self.set_labels.get(xl) if xl is not None else None
        return set_count_of(row, declared)

    def needs_vision(self, row: Mapping[str, Any]) -> bool:
        if self.force_vision:
            return True
        return text_verdict(row)["status"] != "decided"

    def _notes(self, row: Mapping[str, Any]) -> str:
        """交给视觉模型的文本侧证据 + 套装件数(互证用)。"""
        info = text_evidence(row)
        n, how = self.set_count(row)
        lines = []
        if n:
            lines.append("## 套装数量")
            lines.append("- %d 件（%s）%s" % (
                n, how,
                "  ← **多件**：结构化尺寸/整叠图上的厚度方向很可能是**整叠之和**，"
                "必须只报**单块**厚度。" if n > 1 else ""))
        else:
            lines.append("## 套装数量")
            lines.append("- 未明确（标题/SKU 未见明确件数）")
        expl = [e for e in info["evidence"] if e["tier"].startswith("explicit")]
        if expl:
            lines.append("## 文本里出现的厚度陈述(可能针对不同尺寸/不同件, 请判断)")
            lines.append("- " + "；".join("%s「%s」" % (e["field"], e["snippet"]) for e in expl[:4]))
        if info["pd_raw"]:
            lines.append("## 本行 Product Dimensions 原文")
            lines.append("- %s" % info["pd_raw"][:160])
            lines.append("注意: 套装商品的这一位量的是**整叠高度**时, 不能当单块厚度。")
        return "\n".join(lines)

    def run(self, rows: Sequence[Mapping[str, Any]], out_dir: str, *,
            set_labels: Optional[Mapping[Any, str]] = None, progress: bool = True,
            retry_unusable: bool = False) -> dict:
        """`set_labels`: {xl_row: 交付表「套装数量」原值}, 如 {2: "3件套"}。

        `retry_unusable=True`: 只重跑**已有产物里视觉记录不可用**(剥壳后仍拿不到字段
        —— 模型把 `response_format` 原样回吐或返回空) 的行, 其余沿用已有记录。
        """
        os.makedirs(out_dir, exist_ok=True)
        ckpt_path = os.path.join(out_dir, self.CKPT_NAME)
        out_path = os.path.join(out_dir, self.OUT_NAME)
        self.set_labels = {}
        for k, v in (set_labels or {}).items():
            try:
                self.set_labels[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        row_by: dict[int, Mapping[str, Any]] = {}
        for i, r in enumerate(rows, start=2):
            xl = r.get("xl_row")
            row_by[int(xl) if xl is not None else i] = r

        todo = [xl for xl in row_by if self.needs_vision(row_by[xl])]
        seed: dict[int, dict] = {}
        if retry_unusable:
            prev = (self.load(out_dir).get("rows") or {})
            keep, retry = 0, []
            for xl in todo:
                u = unwrap_vision((prev.get(str(xl)) or {}).get("视觉"), _WANT_KEYS)
                if any(k in u for k in _WANT_KEYS):
                    seed[xl] = {"xl_row": xl, "vision": u}
                    keep += 1
                else:
                    retry.append(xl)
            self._log("视觉记录复查: 可用 %d 行沿用, 需重跑 %d 行" % (keep, len(retry)))
            todo = retry
        plan_key = "%d|%s|%d|%d|retry" % (len(rows), self.detail, self.max_images,
                                          self.aplus_images) if retry_unusable \
            else "%d|%s|%d|%d" % (len(rows), self.detail, self.max_images, self.aplus_images)
        done: dict[int, dict] = dict(seed)
        if os.path.exists(ckpt_path):
            try:
                with open(ckpt_path, encoding="utf-8") as fh:
                    o = json.load(fh)
                if (o.get("input_key") or "") == plan_key:
                    for it in (o.get("done") or []):
                        if isinstance(it, dict) and it.get("xl_row") is not None:
                            done[int(it["xl_row"])] = it
            except (OSError, ValueError):
                done = {}
        if done:
            self._log("厚度精确值断点续跑: 已读回 %d 行, 只跑剩余" % len(done))

        def _save_ckpt() -> None:
            try:
                tmp = ckpt_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"input_key": plan_key,
                               "done": [done[k] for k in sorted(done)]},
                              fh, ensure_ascii=False)
                os.replace(tmp, ckpt_path)
            except OSError:
                pass

        def _fill_one(xl: int) -> Optional[dict]:
            row = row_by.get(xl)
            if row is None:
                return None
            urls = self.row_image_urls(row, self.max_images, self.aplus_images)
            if not urls:
                return {"xl_row": xl, "vision": None, "skipped": "无图片"}
            prompt = build_thickness_prompt(row, n_images=len(urls), notes=self._notes(row))
            out, err = _vision_with_fallback(self.ctx, prompt, urls,
                                            detail=self.detail, max_tokens=1200)
            if out is None:
                self._log("厚度视觉失败(行 %s): %s" % (xl, err))
                return None
            return {"xl_row": xl, "vision": out if isinstance(out, dict) else None}

        pending = [xl for xl in todo if xl not in done]
        if pending:
            n, t0 = len(pending), __import__("time").time()
            use = self.workers if n > 1 else 1
            if use > 1:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=use) as ex:
                    futs = {ex.submit(_fill_one, xl): xl for xl in pending}
                    fin = 0
                    for fut in as_completed(futs):
                        xl = futs[fut]
                        r = fut.result()
                        fin += 1
                        if r is not None:
                            done[xl] = r
                            _save_ckpt()
                        if progress and (fin % 25 == 0 or fin == n):
                            self._log("   厚度视觉 行 %d/%d (累计 %d, %.0fs)"
                                      % (fin, n, len(done), __import__("time").time() - t0))
            else:
                for xl in pending:
                    r = _fill_one(xl)
                    if r is not None:
                        done[xl] = r
                        _save_ckpt()
        missing = [xl for xl in todo if xl not in done]
        rnd = 0
        while missing and rnd < 3:
            rnd += 1
            self._log("   厚度视觉 %d 行失败, 逐行重试第 %d 轮" % (len(missing), rnd))
            still = []
            for xl in missing:
                r = _fill_one(xl)
                if r is not None:
                    done[xl] = r
                    _save_ckpt()
                else:
                    still.append(xl)
            missing = still
        if missing:
            self._log("   厚度视觉 %d 行重试后仍失败(回落文本证据)" % len(missing))

        # ---- 定案 + 落盘
        if not missing and os.path.exists(ckpt_path):
            try:
                os.remove(ckpt_path)
            except OSError:
                pass
        return self._finalize(row_by, {xl: (done.get(xl) or {}).get("vision") for xl in row_by},
                              out_path)

    def _finalize(self, row_by: Mapping[int, Mapping[str, Any]],
                  vision_by: Mapping[int, Any], out_path: str) -> dict:
        """按当前判定逻辑给每行定案并落盘(不调用模型)。"""
        results: dict[str, Any] = {}
        stats: dict[str, int] = {}
        for xl in sorted(row_by):
            sc, _how = self.set_count(row_by[xl])
            verdict = decide(row_by[xl], vision_by.get(xl), set_count=sc,
                             set_label=self.set_labels.get(xl, ""),
                             text_rec=self.text_recs.get(xl))
            v = verdict["value"]
            results[str(xl)] = {
                "厚度": v if v else "未明确",
                "cm": verdict["cm"],
                "层级": verdict["tier"],
                "套装件数": sc,
                "来源": verdict["source"],
                "需复核": bool(verdict["review"]),
                "证据": verdict["evidence"],
                "视觉": vision_by.get(xl),
            }
            stats[verdict["tier"]] = stats.get(verdict["tier"], 0) + 1
        n_vision = sum(1 for v in vision_by.values() if v)
        payload = {"cat": getattr(self.ctx.config, "cat", ""), "unit": "cm",
                   "rows": results, "统计": stats, "视觉调用行数": n_vision}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, out_path)
        self._log("厚度精确值已写入 %s (视觉复核 %d 行; 层级 %s)" % (out_path, n_vision, stats))
        return payload

    def redecide(self, rows: Sequence[Mapping[str, Any]], out_dir: str, *,
                 set_labels: Optional[Mapping[Any, str]] = None) -> dict:
        """**用已有的视觉记录**重跑定案(不调用模型)。

        调了判定逻辑(如套装口径)后不必重跑 2000+ 次图片调用 —— 产物 JSON 里存了原始
        `视觉` 记录, 直接拿来重新定案即可。
        """
        self.set_labels = {}
        for k, v in (set_labels or {}).items():
            try:
                self.set_labels[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        payload = self.load(out_dir)
        old = payload.get("rows") or {}
        self.load_text_recs(out_dir)          # 复用文本复核结果, 不重调模型
        row_by: dict[int, Mapping[str, Any]] = {}
        for i, r in enumerate(rows, start=2):
            xl = r.get("xl_row")
            row_by[int(xl) if xl is not None else i] = r
        vision_by = {int(k): (v or {}).get("视觉") for k, v in old.items()}
        return self._finalize(row_by, vision_by, os.path.join(out_dir, self.OUT_NAME))

    @staticmethod
    def load(out_dir: str) -> dict:
        path = os.path.join(out_dir, ThicknessRefiner.OUT_NAME)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
