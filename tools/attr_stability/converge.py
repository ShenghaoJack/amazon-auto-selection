# -*- coding: utf-8 -*-
"""归类与标准化（收敛层）：把并集产出的**多样提名**收敛回受控值

在流水线里的位置
================================================================================
    步骤17  单遍标注            → 召回不足
    步骤17  N 遍标注 + 并集      → 补召回（值多样、未收敛）
    步骤18  **归类 + 标准化**    → 收敛到受控值   ← 本模块

为什么必须有这一层
================================================================================
    并集口径会产出多个提名（如 `内置可充电电池（USB充电）` / `内置可充电电池`），
    若不收敛，交付表里同一属性就有多种写法 → 表面"不一致"。
    但这不是问题 —— 只要后面有归类层把同义/同物提名**收敛到同一个受控值**，
    就能同时拿到「高召回」与「高一致」。
    反过来说：**用"多数表决"去换一致，是以丢弃少数提名为代价的，方向错了。**

三层分工（与整个项目一致）
================================================================================
    · **机械归一**（本模块，代码）：全半角/空白/分隔符/大小写/单位写法 —— 确定性的
    · **语义归类**（大模型）：同义、同物、上下位（如 `不锈钢` → 颜色主值 `银色`
      属类目语义规则 S1）—— 必须看上下文，代码写不死
    · **校验与登记**（本模块，代码）：大模型输出必须落在受控值内，
      否则登记为「建议新增受控值」，**不得静默通过**

本模块只做①③，②的载荷由 `build_classify_payload()` 生成、交大模型。

用法（离线验证）
================================================================================
  python -m attr_stability.converge --prefix r3
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "normalize_form", "normalize_measure", "normalize_size", "converge_mechanical",
    "convergence_report", "build_classify_payload", "apply_classification",
    "ConvergenceResult",
]


# ================================================================ 机械归一
_PUNCT_MAP = {
    '（': '(', '）': ')', '，': ',', '；': ';', '：': ':', '、': ',',
    '／': '/', '×': 'x', '＊': '*', '－': '-', '～': '~',
    '　': ' ', '\u200b': '',
}


def normalize_form(v: Any) -> str:
    """机械归一：全半角统一、去空白、分隔符统一、大小写统一。

    **只做确定性变换，不做任何语义合并** —— 近义词/上下位留给大模型。
    例：
      'ＡＢＣ　１２３'   → 'abc 123'
      '2.0'            → '2'
      '1,000 ml'       → '1,000 ml'   （千分位不动）
      '不锈钢、钛'       → '不锈钢,钛'
    """
    if v is None:
        return ''
    if isinstance(v, bool):
        return '是' if v else '否'
    s = str(v)
    # 全角 → 半角（NFKC 会把全角字母数字标点转半角）
    s = unicodedata.normalize('NFKC', s)
    for a, b in _PUNCT_MAP.items():
        s = s.replace(a, b)
    s = re.sub(r'\s+', ' ', s).strip()
    # 去无意义尾零：2.0 → 2，7.50 → 7.5（只对纯数字串）
    m = re.fullmatch(r'(-?\d+)\.(\d+)', s)
    if m:
        s = ('%s.%s' % (m.group(1), m.group(2))).rstrip('0').rstrip('.')
    return s


# ================================================================ 计量归一
# 长度单位 → cm 的换算系数（厚度/尺寸都用长度口径）
_UNIT_TO_CM = {
    'cm': 1.0, '厘米': 1.0, '公分': 1.0, 'centimeter': 1.0, 'centimetre': 1.0,
    'mm': 0.1, '毫米': 0.1, 'millimeter': 0.1, 'millimetre': 0.1,
    'm': 100.0, '米': 100.0, 'meter': 100.0, 'metre': 100.0,
    'inch': 2.54, '英寸': 2.54, '"': 2.54, '\u2033': 2.54, '\u201d': 2.54,
    'in': 2.54, 'ft': 30.48, '英尺': 30.48, "'": 30.48, '\u2032': 30.48,
}
_MEASURE_RE = re.compile(r'^(\d+(?:[.,]\d+)?)\s*([A-Za-z"\'\u2032\u2033\u201d\u5bf8\u7c73\u5398\u5206\u516c]*)\s*$')
# 区间写法：`0.5-0.7 cm` / `0.5~0.7cm` / `0.5 – 0.7 cm`
_RANGE_RE = re.compile(
    r'^(\d+(?:[.,]\d+)?)\s*([A-Za-z"\'\u2033\u5bf8]*)\s*[-~\u2013\u2014\u81f3]\s*'
    r'(\d+(?:[.,]\d+)?)\s*([A-Za-z"\'\u2033\u5bf8]*)\s*$')


def _to_num(num: str) -> Optional[float]:
    if ',' in num and '.' not in num:          # 德语区小数逗号 `1,5`
        num = num.replace(',', '.')
    try:
        return float(num)
    except ValueError:
        return None


def _conv(x: float, key: str, target: str) -> Optional[str]:
    key = (key or '').lower()
    if not key:
        return None
    if key not in _UNIT_TO_CM:
        return None
    val = x * _UNIT_TO_CM[key] / _UNIT_TO_CM.get(target.lower(), 1.0)
    return '%g' % round(val, 4)


def normalize_measure(v: Any, unit: str = 'cm', *, unit_factors=None, quantity=None) -> tuple[str, str]:
    """把计量值统一成 **`数字+单位`、中间无空格** 的写法。

    返回 `(规范值, 状态)`，状态取值：
      · `ok`       成功（含单位换算）
      · `assumed`  原文无单位 → 按目标单位处理（**需人工确认**）
      · `failed`   解析不了（如 `加厚`、`大号2.5cm,小号1.1cm`）→ **原值保留**

    为什么必须做：实测厚度列 1,417 格有 **127 种写法** ——
    `1cm` / `1 cm` / `1.5cm` / `2 cm` / `1.5mm` / `1.5 mm` / `2.7` / `加厚` …
    其中 657 格只是"数字与单位之间有空格"，175 格用了 mm，2 格用了 inch。

    支持的形态：
      · 单值          `1.5 mm` → `0.15cm`、`2 inch` → `5.08cm`
      · **区间**      `0.5-0.7 cm` → `0.5-0.7cm`（两端各自换算）
      · 逗号小数      `1,5 cm` → `1.5cm`
    换算后按 `%g` 输出（去尾零），避免 `2.0cm` 这种写法。
    """
    from .numeric_values import normalize_numeric
    return normalize_numeric(v, {"unit": unit, "unit_factors": unit_factors or {}, "quantity": quantity})



# ------------------------------------------------------------------ 尺寸归一
# 维度标签（L/W/H/D/B/Th = 长/宽/高/深/宽/厚），只用于"标签与数值是否自洽"的检查
_LABELED = re.compile(
    r'(\d+(?:[.,]\d+)?)\s*(L|W|H|D|B|TH|T|Länge|Breite|Höhe|Tiefe'
    r'|\u957f|\u5bec|\u5bbd|\u9ad8|\u539a|\u6df1)\b', re.I)
# 数值 + 可选单位。**小数逗号必须先被这个正则吃掉**（`1,5` 是一个数），
# 否则后面按逗号分段会把德语小数拆成两个尺寸。
_NUM_UNIT = re.compile(
    r'(\d+(?:[.,]\d+)?)\s*'
    r'(mm|cm|m|inch|in|ft|"|\u2033|\u6beb\u7c73|\u5398\u7c73|\u7c73|\u82f1\u5bf8)?',
    re.I)
# 强分隔符：出现即视为"另一个尺寸"（而非同一尺寸的下一维）
# ⚠ 逗号必须算强分隔符 —— 因为 `normalize_form` 会把 `、` 规范成 `,`，
# 而"数字内部的小数逗号（`1,5`）"已经被 `_NUM_UNIT` 整个吃掉了，
# 所以**残留的逗号一定是分隔符**。（早期版本漏了这条，导致
# `40 x 28 cm、34 x 23 cm` 被合成一个三维 `40x28x34 cm`。）
_SEP_STRONG = re.compile(r'[,;\uff1b+\uff0b&]|\u3001|\u548c|\u53ca|\band\b|\u4ee5\u53ca')
# 尺寸标签（大号/中号/S/M/L…）。必须用**后顾断言**排除 `cm` 里的 `m`/`c`：
# 早期版本用 `\b` 边界，结果把 `cm` 的 `m` 当成了尺寸标签，输出 `m:29x20x缺 cm`。
_SIZE_LABEL = re.compile(
    r'(?<![A-Za-z0-9])(XXL|XL|XS|L|M|S'
    r'|\u5927\u53f7|\u4e2d\u53f7|\u5c0f\u53f7'
    r'|\u5bb6\u5ead\u88c5|\u65c5\u884c\u88c5|\u8ff7\u4f60|\u52a0\u5927|\u52a0\u539a)', re.I)


_UNIT_TOKEN = re.compile(
    r'(mm|cm|m|inch|in|ft|"|\u2033|\u6beb\u7c73|\u5398\u7c73|\u7c73|\u82f1\u5bf8)',
    re.I)


def _nums_of(text: str) -> list[tuple[float, str, int, int]]:
    """抽出 `(数值, 单位, 起, 止)` 序列。小数逗号在 `_NUM_UNIT` 里已被吃掉。

    注意：`_NUM_UNIT` 只认**真正的单位**（cm/mm/inch…），
    **不认维度标签**（L/W/H/D/Th）—— 那些是"这是哪一维"的记号，不是单位。
    所以 `31.2L x 1B x 22D cm` 里三个数值的单位都为空，
    真实单位要靠整组的文本跨度去认（见 `_group_sizes`）。
    """
    out = []
    for m in _NUM_UNIT.finditer(text):
        n = _to_num(m.group(1))
        if n is None:
            continue
        out.append((n, (m.group(2) or '').lower(), m.start(), m.end()))
    return out


def _group_sizes(text: str) -> list[tuple[str, list[tuple[float, str]], str]]:
    """拆成"尺寸组"。返回 `[(标签, [(数值,单位)…], 该组文本跨度)]`。

    实测必须处理这些形态（都是真实数据）：
      `25 x 15 cm + 29 x 20 cm + 39 x 28 cm`      → 3 组（`+` 是强分隔符）
      `大号24 x 40 cm、小号20 x 32 cm`              → 2 组（`、` + 标签）
      `40 x 28 cm、34 x 23 cm、30 x 20 cm`         → 3 组
      `小号25.9×16.7cm + 中号33.3×23.5cm`          → 2 组
      `31.2L x 1B x 22D cm`                        → 1 组三维（单位取组末的 cm）
      `210 x 280 x 10 mm`                          → 1 组三维
      `1,5 x 20 cm`（德语小数）                      → 1 组（逗号在数字内，不是分隔符）

    **早期版本只按 `、;,` 分段，且 `、` 先被归一成 `,` 后失去区分度**，
    于是 `25 x 15 cm + 29 x 20 cm + 39 x 28 cm` 被读成三维 `25x15x29 cm`
    —— 两个不同尺寸被静默合并成一个，而且看起来还很"规范"。
    这比不归一更糟，所以本函数有配套用例 `d142_test_size_norm.py`。
    """
    toks = _nums_of(text)
    if not toks:
        return []
    groups: list[list[tuple[float, str, int, int]]] = [[toks[0]]]
    for i in range(1, len(toks)):
        sep = text[toks[i - 1][3]:toks[i][2]]
        if _SEP_STRONG.search(sep) or len(groups[-1]) >= 3:
            groups.append([])
        groups[-1].append(toks[i])
    res = []
    prev_end = 0
    for gi, g in enumerate(groups):
        pre = text[prev_end:g[0][2]]
        if gi > 0:
            pre = _SEP_STRONG.sub(' ', pre)      # 去掉分隔符本身再找标签
        lab = ''
        for m in _SIZE_LABEL.finditer(pre):
            lab = m.group(0)
        # 组的文本跨度 = 本组首个数值 到 下一组首个数值（或串尾）。
        # 必须含尾部，否则 `... 22D cm` 末尾的单位认不到。
        nxt = groups[gi + 1][0][2] if gi + 1 < len(groups) else len(text)
        span = text[g[0][2]:nxt]
        res.append((lab, [(n, u) for n, u, _, _ in g], span))
        prev_end = g[-1][3]
    return res


def _size_one(text: str, unit: str) -> tuple[str, str]:
    """把一整串尺寸归一成 `长x宽x高 单位`；多尺寸用 `、` 连接；缺的维度写 `缺`。

    ⚠ 同时检查"维度标签与数值大小是否自洽"：DE/UK 有
    `31.2L x 1B x 22D cm` 这种写法 —— 若真按 L=31.2/B=1/D=22 读，厚度 22cm 荒谬，
    而 FR 同一产品写作 `31.2x22x1cm`。**标签贴错了数字**。
    这类标 `suspicious` 交人工，不擅自重排（靠猜排序会制造新错误）。
    """
    s0 = normalize_form(text).strip()
    if not s0:
        return '', 'failed'
    labeled = _LABELED.findall(s0)
    suspicious = False
    if len(labeled) >= 2:
        lab = {}
        for n, u in labeled:
            lab.setdefault(u.upper()[0], _to_num(n))
        lv = lab.get('L')
        others = [v for k, v in lab.items() if k != 'L' and v is not None]
        if lv is not None and others and any(o > lv for o in others):
            suspicious = True
    groups = _group_sizes(s0)
    if not groups:
        return s0, 'failed'
    tgt = unit or 'cm'
    out = []
    any_assumed = False
    for label, g, span in groups:
        if len(g) > 3:
            return s0, 'failed'
        # 组的单位：优先取该组成员自带的单位，否则取**整组跨度**里的单位
        # （`31.2L x 1B x 22D cm` 的单位只在组末出现，必须靠 span 才能认到）
        u = next((uu for _, uu in g if uu), '')
        if not u:
            mu = _UNIT_TOKEN.search(span)
            u = mu.group(0).lower() if mu else ''
        if not u:
            any_assumed = True
        vals = []
        for n, uu in g:
            if uu or u:
                c = _conv(n, uu or u, tgt)
                vals.append(c if c else ('%g' % round(n, 2)))
            else:
                vals.append('%g' % round(n, 2))
        while len(vals) < 3:
            vals.append('缺')
        one = '%s %s' % ('x'.join(vals[:3]), tgt)
        out.append('%s:%s' % (label, one) if label else one)
    joined = '、'.join(out)
    if suspicious:
        return joined, 'suspicious'
    if any_assumed:
        return joined, 'assumed'
    return joined, 'ok'


def normalize_size(v: Any, unit: str = 'cm') -> tuple[str, str]:
    """尺寸统一成 `长x宽x高 单位`（`x` 连接、单位前一个空格、缺的写 `缺`）。

    例：
      · `40 x 30 cm`              → `40x30x缺 cm`
      · `23.8 x 14.3 x 0.2 cm`    → `23.8x14.3x0.2 cm`
      · `31.2L x 1W x 22Th cm`    → `31.2x1x22 cm`（去掉 L/W/Th 标签）
      · `210 x 280 x 10 mm`       → `21x28x1 cm`（单位换算）
      · `25 x 15 cm + 29 x 20 cm` → `25x15x缺 cm、29x20x缺 cm`（**不合并**）
      · `大号24 x 40 cm、小号20 x 32 cm` → `大号:24x40x缺 cm、小号:20x32x缺 cm`

    返回 `(规范值, 状态)`：`ok` / `assumed`（无单位）/ `suspicious`（标签与数值矛盾）
    / `failed`（解析不了，原值保留）
    """
    if v is None:
        return '', 'failed'
    return _size_one(str(v), unit)


def _key(v: Any) -> str:
    """比较键：机械归一 + 小写 + 去掉所有标点空白（用于"只是写法差异"判定）"""
    s = normalize_form(v).lower()
    return re.sub(r'[\s,;:/()\[\]{}*+\-]', '', s)


_ALIAS_PENDING = {'', '待人工', '待归类'}
"""同义表里表示"人还没定"的目标值 —— 是**标记不是受控值**。
收敛时遇到它们要当成"未命中"（→ 待归类队列），绝不能写进 `标准` 列。"""

_PREFIX_SEP = re.compile(r'[（(，,;；:：]')


def _variants(v: str) -> list[str]:
    """**保守**的降级写法：受控值 + 附加说明时的候选切分。

    为什么需要：追问/视觉的 `候选值` 允许写原文片段，
    于是大量形如 `是（Saftrille 汁槽）`、`是，耐热高达175°C`、`带汁槽(带导流槽)`
    的写法 —— 它们**语义上就是受控值本身**，附加说明应进明细列，
    却因为整串不匹配词表而被判"待归类"（实测占待归类的一大块）。

    只做"取第一段"这一种切分，不猜、不模糊匹配：
    切出来的第一段仍须**精确命中**受控值或同义表才采用。
    """
    out = []
    s = normalize_form(v)
    m = _PREFIX_SEP.search(s)
    if m and m.start() > 0:
        out.append(s[:m.start()].strip())
    # 括号里的内容本身就是受控值的情况（如 `(带导流槽)`）
    for grp in re.findall(r'[（(]([^）)]+)[）)]', s):
        if grp.strip():
            out.append(grp.strip())
    return [x for x in out if x]


# ================================================================ 收敛
@dataclass
class ConvergenceResult:
    attr: str
    cells: dict[str, dict[str, Any]] = field(default_factory=dict)
    """{格键: {候选:[...], 收敛后:{候选:受控值}, 状态:'一致'|'已收敛'|'待归类',
              主值, 未命中:[...]}}"""
    unmatched: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """{属性: [{原值, 归一后, 出现行数, 出现运行数, 样例行}]} —— 词表外、需大模型归类"""
    stats: dict[str, int] = field(default_factory=dict)


def _conv_multi(v: str, vals: Sequence[str], order: Sequence[str],
                seps: Sequence[str],
                aliases: Optional[Mapping[str, str]] = None,
                sep_out: str = '、') -> tuple[str, list[str]]:
    """multi 属性的收敛：**按基础词**逐个查词表，再按规范序重组。

    这是 multi 属性唯一正确的做法 —— 整串比对永远收敛不了：
      `厨房、露营、烧烤`      与
      `厨房、露营、户外、烧烤`  整串不同，但按词收敛后前者是后者的子集，
      只差一个 `户外`，人一眼就能判断"是否遗漏"。

    查词顺序：**受控值 → 同义表(aliases) → 未命中**。
    `aliases` 不能省 —— 否则"观察到的写法"（如 `竹木`、`防滑脚垫`）全部落进未命中，
      multi 属性会 100% 变成待归类（这是本函数早期版本的真实缺陷）。

    返回 (收敛后的串, 词表外的词)
    """
    words: list[str] = []
    for part in re.split('|'.join(re.escape(s) for s in seps), v):
        p = normalize_form(part)
        if p and p not in words:
            words.append(p)
    decl_k = {_key(x): x for x in vals}
    alias_k = {_key(k): str(v2) for k, v2 in (aliases or {}).items()}    # 无词表（开放型）时，**词本身就是结果** —— 不能当"未命中"丢掉。
    # 踩过的坑：早期版本在这种情况下把所有词都塞进 `unk`，`out` 恒为空，
    # 于是 open 属性的收敛结果全是空串（实测把 尺寸规格/图片OCR文字/雕刻文字
    # 共 ~5,500 格的值清空了，而"越界校验"因为跳过 open 型而完全没发现）。
    no_vocab = not vals
    out, unk = [], []
    for w in words:
        k = _key(w)
        if k in decl_k:
            out.append(decl_k[k])
        elif w in vals or k in decl_k:
            out.append(w)
        elif k in alias_k:
            tgt = alias_k[k]
            if tgt and tgt != '待人工':
                out.append(tgt)
            else:
                unk.append(w)
        elif no_vocab:
            out.append(w)
        else:
            unk.append(w)
    idx = {w: i for i, w in enumerate(order or vals)}
    known = [w for w in dict.fromkeys(out) if w in idx]
    known.sort(key=lambda w: idx[w])
    rest = [w for w in dict.fromkeys(out) if w not in idx]
    # 输出分隔符由类目配置决定：材质用 `+`（`材质A+材质B`），其余用 `、`
    return sep_out.join(known + rest), unk


def converge_mechanical(
    cells: Mapping[str, Mapping[str, Any]],
    declared: Mapping[str, Sequence[str]],
    aliases: Optional[Mapping[str, Mapping[str, str]]] = None,
    attrs_spec: Optional[Mapping[str, Mapping[str, Any]]] = None,
    row_ids: Optional[Sequence[int]] = None,
) -> dict[str, ConvergenceResult]:
    """第一遍：**机械可判的部分**，**按属性类型分别收敛**。

    · `enum`/single ：词表命中（含审定后的同类名表 aliases）
    · `enum`/multi  ：按基础词命中 + 按 `value_order` 规范序重组
    · `numeric`     ：数值+单位**格式归一**（词表为空，不查词表）
                      —— 归一规则本身由已有引擎负责（d64/d76/d98），此处只做形状统一
    · `open`        ：清洗（空白/全半角/去重），**不查词表**

    状态含义：
      · `一致`   所有提名归一后**同一个**值
      · `已收敛` 多个提名，收敛后**同一个**值
      · `待归类` 有提名落在词表外，或收敛后仍不同（语义分歧）→ 交大模型

    `attrs_spec`：{属性名: {'type':…, 'value_shape':…, 'value_order':…,
                            'value_separators':…}}；缺省时按 single/enum 处理。
    """
    aliases = aliases or {}
    attrs_spec = attrs_spec or {}
    out: dict[str, ConvergenceResult] = {}
    seen: dict[str, dict[str, Any]] = {}
    bad_measure: dict[str, dict[str, str]] = {}
    assumed: dict[str, dict[str, str]] = {}

    for key, d in cells.items():
        if d.get("kind") != "value":
            continue                      # 只收敛交付值格(attr.<属性>); 来源格已被归 free
        rid, _, path = key.partition("|")
        if not path.startswith("attr."):
            continue
        attr = path[len("attr."):]
        spec = attrs_spec.get(attr) or {}
        atype = str(spec.get('type') or 'enum')
        shape = str(spec.get('value_shape') or 'single')
        vals = [g["value"] for g in d["values"]
                if str(g["value"]).strip() not in ('', '未明确', 'None')]
        if not vals:
            continue
        declared_set = list(declared.get(attr) or [])
        decl_norm = {_key(v): v for v in declared_set}
        # ★ 2026-10-28 用户口径: **人工确认过的别名优先**。
        #   原来是 `{**spec.aliases, **runtime}`（LLM 归类覆盖人工映射），
        #   导致"词表面板里定好的 A→B"在标准化时被 LLM 的临时判断改掉。
        #   现在反过来：runtime 归类只作补充，rules.json 里的 aliases 说了算。
        alias = {**(aliases.get(attr) or {}), **(spec.get('aliases') or {})}
        # ★ 词表里**本身就有组合值**（含连接符，如 `不锈钢+塑料`）→ 那么行内的组合值
        #   必须**整体**落到某个词表项上，不能"部分分量命中就算过"。
        #   否则 `不锈钢+小麦秸秆PP`（分量 不锈钢✓ + 小麦秸秆PP✗）会以 multi 的宽松
        #   通道溜过去，永远不进化简 —— 实测 2 格漏网，通用自检 ② 报"8 > 建议 5"。
        _seps = spec.get('value_separators') or ['、', ',', ';', '+']
        _join = str(spec.get('join_sep') or '、')
        vocab_has_combo = any(str(s).strip() and str(s) in str(v)
                              for v in declared_set for s in _seps)
        # **同义表的键必须先做 `_key` 归一**再比。
        # 踩过的坑：单值分支里 `k = _key(v)` 归一了**值**，却拿它去 `k in alias`
        # 比**原始键** —— 于是凡是键里带大写/空格/标点的别名永远匹配不上
        # （`不含BPA`、`BPA free`、`100% Food-Grade`、`L型折边防滑` …），
        # 静默落进待归类。多值分支 `_conv_multi` 本来就归一了，所以只有单值中招。
        alias_k = {_key(k): str(v2) for k, v2 in alias.items()}
        # 值里的"温度/etc."模式规则（人工口径），未命中词表后按其判定
        pats = spec.get('if_pattern') or []

        # 模式规则（人工口径）：如「耐热性出现具体温度 → 是」。
        # **必须放在"待人工"分支之前试** —— 否则别名指向 `待人工` 的值会提前
        # 判为未命中，模式规则永远够不到（实测 `260°C热处理` 就是这样漏掉的）。
        def _by_pattern(s: str) -> str:
            for pr in pats:
                try:
                    if re.search(pr['pattern'], s):
                        return str(pr['then'])
                except Exception:
                    pass
            return ''

        conv: dict[str, str] = {}
        miss: list[str] = []
        for v in vals:
            rule_config = spec.get('_rule_config')
            declared_rule = spec.get('rule')
            execute_rule = declared_rule in ('color', 'noise', 'material', 'layer_material', 'count_modes', 'ocr')
            execute_rule |= declared_rule in ('enum', 'multi') and any(
                rule_config and rule_config['params'].get(k) for k in ('recheck', 'contains', 'special'))
            if rule_config and execute_rule:
                from .rule_pipeline import primary_value
                from .std_rules import ST_REVIEW, apply_rule
                projected, rule_status, _, _ = apply_rule(rule_config, v, {})
                primary = primary_value(rule_config, projected, v)
                if rule_status == ST_REVIEW and str(primary or '').strip() not in ('未明确', ''):
                    miss.append(v)
                else:
                    conv[v] = str(primary or '')
                continue
            if atype == 'numeric':
                # 数值型：词表为空，收敛 = **计量归一**（统一单位 + 数字+单位无空格）
                tgt = str(spec.get('unit') or ('' if spec.get('_unit_unresolved') else 'cm'))
                nv, st = normalize_measure(v, tgt, unit_factors=spec.get('unit_factors'),
                                           quantity=spec.get('quantity') or spec.get('_物理量'))
                if st == 'failed':
                    # 人工口径：解析不出数值 → 填类目指定的值（厚度 → `未明确`）。
                    # 例 `加厚` —— 它不是数值，也不该原样留在数字列里。
                    ov = str(spec.get('on_parse_fail') or '未明确')
                    if ov:
                        conv[v] = ov
                        bad_measure.setdefault(attr, {})[str(v)] = 'failed→' + ov
                        continue
                    bad_measure.setdefault(attr, {})[str(v)] = st
                conv[v] = nv
                if st == 'assumed':
                    assumed.setdefault(attr, {})[str(v)] = nv
                continue
            if atype == 'open' or not declared_set:
                # 开放型 / 无词表：清洗去重，**不查词表**
                if spec.get('size_format'):
                    # 尺寸：统一成 `长x宽x高 单位`（缺的写 `缺`）+ 单位换算
                    nv, st = normalize_size(v, str(spec.get('unit') or 'cm'))
                    conv[v] = nv
                    if st in ('failed', 'suspicious'):
                        bad_measure.setdefault(attr, {})[str(v)] = st
                    elif st == 'assumed':
                        assumed.setdefault(attr, {})[str(v)] = nv
                    continue
                s = normalize_form(v)
                if atype == 'open' and (spec.get('value_separators')):
                    # 开放型也做"多值拆词 + 去重 + 规范分隔符"，但**不校验词表** ——
                    # 开放型本就没有词表，拆出来的词不是"未命中"，不能进待归类。
                    # （早期版本把 unk 也收进 miss，导致 尺寸规格/图片OCR文字
                    #   每个取值都变成"待归类"，`原有·开放` 永远是 0。）
                    s, _unk = _conv_multi(s, [], [], spec['value_separators'])
                conv[v] = s
                continue
            if shape == 'multi':
                # ★ 连接符：显式声明优先；没声明时**跟着词表走** ——
                #   词表里若有组合值（`不锈钢+塑料`），就用它自己的连接符 `+`。
                #   否则默认 `、`，会把 `+` 组合重组成 `不锈钢、塑料` →
                #   与词表不一致 → 闸门判成词表外值（实测第二趟收敛踩到）。
                _join = str(spec.get('join_sep') or '')
                if not _join:
                    _join = '+' if any('+' in str(x) for x in declared_set) else '、'
                # ★★ 修复（审核中2，2026-10-10）：交付值的分隔符与词表常不一致
                #   （词表 `、`、交付 `,`）→ 复合词表（如「适用场景」这类**不拆**的 multi）
                #   整串永远匹配不上、被拆成"词表外"。这里**先把 `,`/`，`/`;` 归一成 `_join`**，
                #   再做一次**整串匹配**；命中就直接用（不必拆）。不命中则原逻辑照旧。
                _v_norm = (v.replace(',', _join).replace('，', _join)
                           .replace(';', _join).replace('；', _join)).strip()
                # 审核（低5）：交付值可能带**尾随/前导分隔符**（实测 `'…cm,'`）→ 去掉再比，
                # 否则归一后仍匹配不上（`'a、b、'` ≠ `'a、b'`）。
                _v_norm = _v_norm.strip(_join).strip()
                if _v_norm in declared_set:
                    conv[v] = _v_norm
                    continue
                s, unk = _conv_multi(v, declared_set,
                                     spec.get('value_order') or declared_set,
                                     spec.get('value_separators')
                                     or ['、', ',', ';', '+'],
                                     alias,
                                     _join)
                if unk and vocab_has_combo:
                    # 词表里有组合值 → 组合值必须**整体**成立，不能"部分成分命中就算过"。
                    # ★ 但送归类的必须是**未命中的成分**，不是整串（2026-10-09 修正）：
                    #   整串送归类 → LLM 只能答 `不锈钢+小麦秸秆PP`→`不锈钢+塑料`，
                    #   学到的映射是**整串**的，换个组合（`钛金属+小麦秸秆PP`）又不认。
                    #   成分送归类 → LLM 答 `小麦秸秆PP`→`塑料`，
                    #   配合第二趟收敛，**任意组合**都能重组出来（组合值靠枚举追不上）。
                    conv[v] = ''
                    miss.extend(unk)
                    continue
                conv[v] = s
                miss.extend(unk)
                continue
            k = _key(v)
            if k in decl_norm:
                conv[v] = decl_norm[k]
            elif k in alias_k and alias_k[k] not in _ALIAS_PENDING:
                conv[v] = alias_k[k]
            elif k in alias_k:
                # 同义表把它标成"待人工/待归类" —— 那是**标记不是值**。
                # 但在放弃之前，先让**模式规则**（人工口径）有机会判它，
                # 例如 `260°C热处理` 虽被标待人工，按"有温度即 是"应归 是。
                hit = _by_pattern(v)
                if hit:
                    conv[v] = hit
                else:
                    miss.append(v)
            elif v in declared_set:
                conv[v] = v
            else:
                # 降级：`是（汁槽）` / `是，耐热175°C` 这类"受控值+附加说明"，
                # 取第一段再精确匹配一次；命中则采用，附加说明进明细。
                hit = ''
                for cand in _variants(v):
                    ck = _key(cand)
                    if ck in decl_norm:
                        hit = decl_norm[ck]
                        break
                    if ck in alias_k and alias_k[ck] not in _ALIAS_PENDING:
                        hit = alias_k[ck]
                        break
                    if cand in declared_set:
                        hit = cand
                        break
                # 模式规则（人工口径）：如「耐热性出现具体温度 → 是」
                if not hit:
                    hit = _by_pattern(v)
                if hit:
                    conv[v] = hit
                else:
                    miss.append(v)

        uniq_out = {conv[v] for v in vals if v in conv}
        if miss:
            status = '待归类'
        elif len(uniq_out) == 1:
            status = '一致' if len({_key(v) for v in vals}) == 1 else '已收敛'
        else:
            status = '待归类'           # 收敛后仍不同 → 语义分歧，交大模型
        main = (sorted(uniq_out)[0] if len(uniq_out) == 1 else '')
        cr = out.setdefault(attr, ConvergenceResult(attr=attr))
        cr.cells[key] = {
            '行号': rid, '候选': vals, '收敛后': conv, '状态': status,
            '主值': main, '未命中': sorted(set(miss)),
        }
        for v in sorted(set(miss)):
            b = seen.setdefault('%s\x00%s' % (attr, normalize_form(v)), {
                '属性': attr, '原值': v, '归一后': normalize_form(v),
                '出现行数': set(), '出现运行数': set(), '样例行': rid})
            b['出现行数'].add(rid)
        if not miss and len(uniq_out) > 1:
            # 收敛后仍不同（如同属性两个都是合法受控值）→ 语义分歧，也要人/模型裁定
            b = seen.setdefault('%s\x00冲突' % attr, {
                '属性': attr, '原值': ' / '.join(sorted(uniq_out)[:4]),
                '归一后': '收敛后仍不同', '出现行数': set(), '出现运行数': set(),
                '样例行': rid, '冲突': True})
            b['出现行数'].add(rid)

    for b in seen.values():
        b['出现行数'] = len(b['出现行数'])
        b['出现运行数'] = len(b['出现运行数'])
    by_attr: dict[str, list] = {}
    for b in seen.values():
        by_attr.setdefault(b['属性'], []).append(b)
    for a, lst in by_attr.items():
        lst.sort(key=lambda x: -x['出现行数'])
        out.setdefault(a, ConvergenceResult(attr=a)).unmatched = lst

    st = {'一致': 0, '已收敛': 0, '待归类': 0}
    for cr in out.values():
        for c in cr.cells.values():
            st[c['状态']] = st.get(c['状态'], 0) + 1
    out.setdefault('_', ConvergenceResult(attr='_')).stats = st
    # 计量归一的体检结果（解析失败的写法 + 无单位按目标单位处理的写法）
    conv_store['bad_measure'] = bad_measure
    conv_store['assumed_unit'] = assumed
    return out


conv_store: dict[str, Any] = {}
"""收敛过程的**体检输出**（不放进 ConvergenceResult，避免污染其结构）。

  · `bad_measure`  ：数值型解析不了的写法（如 `加厚`、`大号2.5cm,小号1.1cm`）→ 原值保留、需人工
  · `assumed_unit` ：原文没有单位、按目标单位处理了的写法 → 需人工确认单位
"""


def convergence_report(results: Mapping[str, ConvergenceResult]) -> dict[str, Any]:
    """汇总结论：多少格"多提名 → 收敛为同一个值"。"""
    st = {'一致': 0, '已收敛': 0, '待归类': 0}
    total = 0
    for a, cr in results.items():
        for _k, v in cr.cells.items():
            total += 1
            st[v['状态']] = st.get(v['状态'], 0) + 1
    n_settled = st.get('一致', 0) + st.get('已收敛', 0)
    return {
        '总格数': total,
        '一致（提名本就同一个受控值）': st.get('一致', 0),
        '已收敛（多提名→同一受控值）': st.get('已收敛', 0),
        '待归类（词表外或有语义分歧）': st.get('待归类', 0),
        '收敛率': round(100.0 * n_settled / total, 2) if total else None,
        '未命中属性数': len([a for a, cr in results.items() if cr.unmatched]),
    }


# ================================================================ 交大模型的载荷
def build_classify_payload(
    results: Mapping[str, ConvergenceResult],
    attrs_spec: Optional[Mapping[str, Any]] = None,
    limit: int = 200,
) -> dict[str, Any]:
    """把「词表外的值」打包成大模型的**语义归类任务**载荷。

    每条给出：原值、归一后、出现行数/次数、以及该属性的 include/exclude/词表。
    大模型须返回 `{属性: {原值: 受控值}}`；**目标值必须来自词表**，
    确实该新增受控值的填 `"新增: <建议受控值>"` —— 由代码登记、人工审定，
    **不允许模型直接把词表外的值当结果输出**。

    ★ 2026-10-07 加**空词表守卫**（改动清单 §13 方案②）：
    实测 `steps/step17_attr_annotation.py::_converge_standardize` 构造 `attrs_spec` 时
    漏传了 `values`，而本函数读的正是 `spec['values']` → **25 个属性的「受控值词表」全空**，
    模型无法把任何值归入词表（只能答 `新增: X`，而"新增"按设计不落地）→
    **所有词表外的值全部掉落**（含全部视觉补充：5947 格里只有 4 格活到交付）。
    现在：词表为空的属性**不静默** —— 跳过并记进返回值 `_跳过(词表为空)`，调用方须告警。
    """
    tasks = []
    skipped: list[dict[str, Any]] = []
    for a, cr in sorted(results.items()):
        if not cr.unmatched:
            continue
        spec = (attrs_spec or {}).get(a) or {}
        vocab = [str(x) for x in (spec.get('values') or [])]
        if not vocab:
            skipped.append({
                '属性': a,
                '待归类原值数': len(cr.unmatched),
                '原因': "attrs_spec 未提供该属性的受控值词表(`values`) → 无法归类",
            })
            continue
        tasks.append({
            '属性': a,
            '受控值词表': vocab,
            'include': spec.get('include') or '',
            'exclude': spec.get('exclude') or '',
            'type_note': spec.get('type_note') or '',
            '待归类原值': [
                {'原值': b['原值'], '出现行数': b['出现行数']}
                for b in cr.unmatched[:limit]
            ],
        })
    return {
        '_任务': '把「待归类原值」逐个映射到「受控值词表」中的值；'
                 '确实不属于任何现有受控值时返回 "新增: <建议词>"。',
        '_硬性要求': [
            '目标值必须逐字符来自该属性的受控值词表，或形如 "新增: X"。',
            '不得输出词表外的其它写法。',
            '拿不准的填 "未明确"（不要猜）。',
        ],
        '_跳过(词表为空)': skipped,
        '待归类': tasks,
    }


def apply_classification(
    results: Mapping[str, ConvergenceResult],
    mapping: Mapping[str, Mapping[str, str]],
    declared: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """把大模型的归类结果落回来，并**校验**（代码侧闸门）。

    返回统计及「合法映射」；调用方只能使用合法映射落值、复收敛和学习。
      · `新增建议`：模型答 "新增: X" 的 → 登记交人工审定，**不直接进词表**
      · `非法输出`：模型给了词表外的值 → 报错（不静默通过）
    """
    new_suggest: dict[str, dict[str, Any]] = {}
    illegal = []
    applied = 0
    valid: dict[str, dict[str, str]] = {}
    for a, mp in (mapping or {}).items():
        if a not in declared or not isinstance(mp, Mapping):
            illegal.append({'属性': a, '模型输出': mp, '原因': '未知属性或映射不是对象'})
            continue
        decl = list(declared.get(a) or [])
        decl_k = {_key(v): v for v in decl}
        sugg = new_suggest.setdefault(a, {})
        for raw, target in (mp or {}).items():
            if not isinstance(raw, str) or not isinstance(target, str):
                illegal.append({'属性': a, '原值': str(raw), '模型输出': target,
                                '原因': '原值与目标必须是字符串'})
                continue
            t = target.strip()
            if t.startswith('新增:') or t.startswith('新增：'):
                w = t.split(':', 1)[-1].split('：', 1)[-1].strip()
                if w:
                    sugg[w] = sugg.get(w, 0) + 1
                continue
            if t in ('未明确', '', 'None'):
                continue
            if _key(t) in decl_k or t in decl:
                applied += 1
                valid.setdefault(a, {})[raw] = decl_k.get(_key(t), t)
            else:
                illegal.append({'属性': a, '原值': raw, '模型输出': t,
                                '原因': '不在受控值词表内，且未标为「新增」'})
    return {'新增建议': new_suggest, '非法输出': illegal, '已应用': applied,
            '合法映射': valid}


def controlled_value_valid(value: Any, values: Sequence[str], *, multi: bool = False,
                           separators: Optional[Sequence[str]] = None) -> bool:
    """校验最终枚举值；多值只允许由合法基础值构成。"""
    keys = {_key(v) for v in values}
    if _key(value) in keys:
        return True
    if not multi:
        return False
    parts = [str(value)]
    for sep in separators or ('、', ',', ';', '+'):
        parts = [piece for part in parts for piece in part.split(str(sep))] if sep else parts
    return bool(parts) and all(_key(part) in keys for part in parts)


def main() -> None:
    """离线验证：对某轮的并集结果跑一遍机械收敛，看"多提名 → 同一受控值"的比例。"""
    import argparse
    import os
    import sys as _sys
    if hasattr(_sys.stdout, 'buffer'):
        import io as _io
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8',
                                        errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='r3')
    ap.add_argument('--cat-dir', default='categories')
    ap.add_argument('--cat', default='salt_pepper_mill')
    a = ap.parse_args()
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from attr_stability import RuleSource, load_run_files, union_cells

    rules = RuleSource.load(a.cat_dir, a.cat)
    runs = load_run_files('results/llm_only', a.prefix)
    cells = union_cells(runs, sorted(runs))
    declared = {n: s.values for n, s in rules.attrs.items()}
    # 类型感知收敛需要属性的 type / value_shape / value_order / 分隔符
    try:
        cf = __import__('attr_stability.catrules', fromlist=['load_category']) \
            .load_category(a.cat_dir, a.cat)
    except Exception:
        cf = None
    specs: dict[str, dict[str, Any]] = {}
    if cf is not None:
        for at in cf.attributes:
            if isinstance(at, dict) and at.get('name'):
                specs[at['name']] = {
                    'type': at.get('type', 'enum'),
                    'value_shape': at.get('value_shape', 'single'),
                    'value_order': at.get('value_order'),
                    'value_separators': at.get('value_separators'),
                }
    res = converge_mechanical(cells, declared, attrs_spec=specs)
    rep = convergence_report(res)
    print('=' * 92)
    print('归类与标准化 —— 机械可判部分（轮次 %s）' % a.prefix)
    print('=' * 92)
    for k, v in rep.items():
        print('   %-30s %s' % (k, v))
    print()
    print('注意：`待归类` 中含两种情况 —— ① 词表外原值；② 收敛后仍不同的语义分歧。')
    print('      两者都要交大模型按 rules.md 的 include/exclude/语义规则归类。')
    print()
    print('【词表外原值清单（需大模型语义归类）】')
    for attr, cr in sorted(res.items()):
        if not cr.unmatched:
            continue
        print('   %s（词表 %d 个值）' % (attr, len(declared.get(attr) or [])))
        for b in cr.unmatched[:10]:
            print('      %-34s 出现 %d 行  归一后=%s'
                  % (str(b['原值'])[:34], b['出现行数'], b['归一后'][:30]))


if __name__ == '__main__':
    main()
