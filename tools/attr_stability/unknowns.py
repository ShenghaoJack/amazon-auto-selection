# -*- coding: utf-8 -*-
"""哨兵（"没信息"标记）唯一权威源。

**来源**：本模块先在 `scripts/_plan_pilot/unknowns.py` 试点验证（见
`冲突与值合法性校验-改动清单-20261007.md` §4 组 0-1/0-2/0-7），验证通过后落生产。
试点侧改为 re-export 本模块，避免两份实现。

**为什么要有这个文件**
--------------------
改动前，"什么算没信息"在 5 个地方各写一份，且内容已发散：
  · `gapfill.py`      L180  EMPTY          6 项（缺 nan）
  · `attr_runner.py`  L682  _EMPTY         7 项
  · `attr_runner.py`  L811  _EMPTY         7 项（字面量又抄一遍）
  · `attr_stability.py` L153 _NEG_EMPTY    把 `无` 当**非法**空值写法
  · `step17_attr_annotation.py` L385 只用 RESERVED_VALUES + MISSING_KEY 过滤词表候选
**5 个集合里没有一个含 `未标注`** —— 而它在真实数据里是 208 格，还被写进了
`dog_dryer.rules.json` 当**合法受控值**（覆盖率因此虚高：`线长` 报 56.3%，实际 9.7%）。

**口径（声明只能"加"不能"减"）**
--------------------------------
  `resolve_unknowns(declared)` = 内置通用集 ∪ 类目 rules.json 的 `unknown_values`
  · **`无` / `否` / `无X` 绝不当作缺值**（踩过的坑：把「是否无线=无」判成没值；
    而 `无` 在部分 legacy rules.json 的 `unknown_values` 里被声明过 → 这里明确排除）

**注意**：`缺` **不在**哨兵集里 —— 它是"非法值/占位符"（见 `illegal_reason`），
不是"没信息"。两者要分开：哨兵是合法的"没有信息"表达，`缺` 是漏出来的残渣。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "UNIVERSAL_UNKNOWN", "MISSING_KEY", "ILLEGAL_EXACT", "SIZE_SPLIT_RE",
    "resolve_unknowns", "is_unknown", "is_missing", "filter_unknown_words",
    "looks_illegal", "illegal_reason", "blank_sentinels",
]

# `<缺键>` —— 与 attr_stability.MISSING_KEY 同值
MISSING_KEY = "<缺键>"

# 内置通用哨兵集（0 token，任何类目可用）
# 含 `未标注`（2026-10-07 修）：它是 LLM 对"原文没写"的另一种说法，与 `未明确` 等价
UNIVERSAL_UNKNOWN: frozenset[str] = frozenset({
    "未明确", "未标注", "不适用", "未知", "待确认", "未提供",
    "未提及", "未说明", "不详", "n/a", "na", "none", "nan", "null",
    "-", "—", "–", "无数据", "无相关信息",
    "", MISSING_KEY,
})

# 非法值：这些**不是**"没信息"，而是不该出现在交付值里的残渣/占位符
ILLEGAL_EXACT: frozenset[str] = frozenset({"缺", "<缺>", MISSING_KEY, "缺键", "None", "none"})

# 尺寸/复合串的切分符（与 converge.normalize_size 的分隔约定一致）
SIZE_SPLIT_RE = re.compile(r"[、,，;；]")
_TOKEN_SPLIT_RE = re.compile(r"[x×*]")
_NUM_RE = re.compile(r"\d")
# 计量单位结尾（用于判断"这串像不像尺寸/计量值"）—— 不要求单位前紧跟数字,
# 因为缺失占位符会把数字顶掉（`17x12x缺 inch` 的单位前就是 `缺`）
_UNIT_TAIL_RE = re.compile(
    r"(cm|mm|km|m|inch|inches|in|ft|feet|厘米|毫米|米|英寸|英尺|寸)\s*$", re.I)
_PREFIX_RE = re.compile(r"^[^:：]{0,8}[:：]")


def resolve_unknowns(declared: Iterable[str] | None = None) -> frozenset[str]:
    """内置通用集 ∪ 类目声明。声明只能加，不能减。"""
    out = set(UNIVERSAL_UNKNOWN)
    for v in (declared or []):
        s = str(v).strip().lower()
        if s:
            out.add(s)
    return frozenset(out)


def _key(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        return ""
    return str(v).strip().lower()


def is_unknown(v: Any, unknowns: Iterable[str] | None = None) -> bool:
    """是否"没信息"。空/None/哨兵 → True。`无`/`否` **不算**（它们是合法取值）。"""
    s = _key(v)
    if not s:
        return True
    return s in (unknowns if unknowns is not None else UNIVERSAL_UNKNOWN)


def is_missing(v: Any, unknowns: Iterable[str] | None = None) -> bool:
    """`is_unknown` 的反面（有真值）。"""
    return not is_unknown(v, unknowns)


def blank_sentinels(values: Mapping[Any, Any],
                    unknowns: Iterable[str] | None = None) -> dict[Any, str]:
    """把哨兵一律写成空串（"缺"另由 `illegal_reason` 处理，不在这里静默清）。"""
    return {k: ("" if is_unknown(v, unknowns) else str(v)) for k, v in values.items()}


def filter_unknown_words(words: Sequence[Any],
                         unknowns: Iterable[str] | None = None,
                         ) -> tuple[list[str], list[str]]:
    """**0-7 哨兵不入词表**：从聚类产出的"规范值"里滤掉哨兵与占位符。

    没有这道守卫，下一轮冷启动聚类会把 `未明确` 也聚类成一个"类别" ——
    `未标注` 就是这么进的 `dog_dryer.rules.json`。

    返回 `(保留, 丢弃)`。
    """
    unk = unknowns if unknowns is not None else UNIVERSAL_UNKNOWN
    kept: list[str] = []
    dropped: list[str] = []
    for w in words:
        s = str(w).strip()
        if not s or is_unknown(s, unk) or illegal_reason(s):
            dropped.append(s)
        elif s not in kept:
            kept.append(s)
    return kept, dropped


# ------------------------------------------------------------------ A-1 值合法性
def _token_has_missing(tok: str) -> bool:
    """单个维度 token 是否是缺失占位符。

    注意 token 常带单位（`17x12x缺 inch` 的第三维切出来是 `缺 inch`），
    所以先剥掉尾部单位再比 —— 否则精确匹配必然漏判。
    """
    t = str(tok).strip().lower()
    if not t:
        return True
    if t in ILLEGAL_EXACT:
        return True
    bare = _UNIT_TAIL_RE.sub("", t, count=1).strip()
    return bare in ILLEGAL_EXACT or bare == ""


def illegal_reason(v: Any) -> str | None:
    """该值为什么非法（合法则返回 None）。**类目无关**，纯形态判定。

    判据（保守，避免误伤正常文本）：
      ① 整体就是占位符（`缺` / `<缺键>` / `缺键`）
      ② 含 `<缺键>` 字样
      ③ **尺寸/计量型串**里某一维是缺失占位符（`17x12x缺 inch`、`缺x缺x缺 cm`、
         `大号:38.1x25.4x缺 cm`、`9x6x缺 inch,11x8.5x缺 inch`）
      ④ 多组尺寸串里某一组**整组就是**占位符
    只在 ③④ 前先要求"这串像尺寸/计量值"（有数字 或 以单位结尾），
    以免把 `缺少说明` / `缺角设计` 这类正常文本判成非法。
    """
    s = str(v or "").strip()
    if not s:
        return None
    if s.lower() in ILLEGAL_EXACT:
        return "值为占位符 %r" % s
    if MISSING_KEY in s or "<缺>" in s or "缺键" in s:
        return "值含缺键标记"

    looks_measure = bool(_NUM_RE.search(s)) or bool(_UNIT_TAIL_RE.search(s))
    if not looks_measure:
        return None

    groups = [g.strip() for g in SIZE_SPLIT_RE.split(s) if g.strip()]
    if len(groups) >= 2:
        for g in groups:
            tail = _PREFIX_RE.sub("", g).strip().lower()
            if tail in ILLEGAL_EXACT or tail == "缺":
                return "多组尺寸串中某组为占位符 %r" % g[:40]
    for g in groups:
        body = _PREFIX_RE.sub("", g).strip()
        toks = [t for t in _TOKEN_SPLIT_RE.split(body) if t.strip()]
        if len(toks) >= 2 and any(_token_has_missing(t) for t in toks):
            return "尺寸串含缺失占位符 %r" % g[:40]
    return None


def looks_illegal(v: Any) -> bool:
    return illegal_reason(v) is not None
