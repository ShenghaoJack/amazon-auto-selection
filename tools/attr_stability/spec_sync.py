# -*- coding: utf-8 -*-
"""spec.hint 与 rules.values 的**口径同步**（2026-10-08）。

问题（通用自检 ③ 报出来的）
===============================================================================
同一件事写了两份、必然漂移：
  · `spec.hint` 里的「典型取值 A / B / C」—— 讲给模型听的**例子**
  · `rules.json` 的 `params.values` —— 闸门**强制**的受控值域

实测（Cutting Boards）：5 个属性两份不一致；Dog Dryer **22 个**。
最严重的一例：`是否含负离子` 的 `rules.values` 只有 1 个（`含负离子`），
而 hint 写了 5 个（含/带/不含/不带/未明确）→ 模型照着 hint 答 `不含负离子`，
闸门判越界 → **历史上 58 格全掉**。

不变量
===============================================================================
    **hint 里列出的「典型取值」必须 ⊆ rules.values**

否则提示词在诱导模型输出闸门不接受的值。本模块负责：
  · `check_caliber()`   —— 查不变量（供通用自检用）
  · `sync_typical_values()` —— 把 hint 的「典型取值」段改写为当前 `rules.values`
    （词表生成/演化后调用，让"例子"永远跟着"法条"走）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

__all__ = ["TYPICAL_RE", "extract_typical", "check_caliber", "rewrite_hint",
           "sync_typical_values", "find_coupled_attrs", "strip_coupling_contradiction",
           "contradiction_sources", "strip_all_contradictions", "value_pool"]

TYPICAL_RE = re.compile(r"典型取值\s*([^；;。\n]+)")
# 例子里最多列几个（太多会撑提示词；超出部分写"等"）
MAX_SHOWN = 12
# 「典型取值」后面这一段超过这么多字符就当它是散文、不是取值列表，不改
MAX_SEG = 200


def extract_typical(hint: Any) -> list[str]:
    """提取「典型取值」里的各项。

    ★ 只按 `/` 拆 —— **不能按 `、` 拆**：受控值本身就可能含 `、`
      （如 `风嘴、导梳` 是**一个**组合值）。实测按 `、` 拆会把一个合法值
      拆成两个不存在的值 → 报假越界（Dog Dryer 3 个属性就是这么被误报的）。
    """
    m = TYPICAL_RE.search(str(hint or ""))
    if not m:
        return []
    out = []
    for x in m.group(1).split("/"):
        x = re.sub(r"[\s　]*等$", "", x.strip())     # 去掉尾部「等」（改写时加的省略号标记）
        if x:
            out.append(x)
    return out


def check_caliber(hint: Any, values: Any) -> list[str]:
    """返回 hint 里**越界**的典型取值（空 = 合规）。"""
    vals = {str(v).strip() for v in (values or []) if str(v).strip()}
    if not vals:
        return []
    return [t for t in extract_typical(hint) if t not in vals]


# ================================================================ (a) 耦合 vs 自相矛盾断言
# 2026-10-09 用户口径：**在 hint 生成/同步时消除"自相矛盾的断言"**，让矛盾不被产出。
#
# 根因（实测，Cutting Boards 全新类目冷启动）：
#   `产品类型` 的 hint 里同时有
#     · 样例「竹制砧板 / 实木砧板 / 玻璃砧板」—— **名字里嵌着材质**
#     · 断言「与材质区分：本属性看用途形态，材质看主体材料」—— 说**与材质无关**
#   两句自相矛盾，模型信了后者 → 20 个纯钛砧板全填 `不锈钢砧板`，
#   而它自己写的 `依据` 里明明是 `Titanium`（**值与依据直接矛盾**）。
#   两轮提示词加固（"典型取值不是闭集"、"值要与依据自洽"）**都没能压过这句断言** ——
#   因为断言写在**属性自己那一行**，比表头的通用规则更具体。
#
# 所以改在**产生矛盾的地方**：hint 同步时，若某属性的样例确实嵌着另一属性的语义，
#   就把那句"与它无关"的断言**换掉**。判据全部由数据算出（看样例嵌了什么），
#   **不写死任何"某属性从某属性派生"的假设** —— 按材质命名、按功能命名都适用。
COUPLING_MIN = 2          # 样例里至少这么多个"嵌着"另一属性语义，才算耦合
INDEP_MARKERS = ("无关", "区分", "不同", "独立", "不是一回事", "另算", "不等于")
# 子句切分：中文分号 / 句号 / 换行（不含逗号 —— 取值列表和长句都用逗号）
CLAUSE_SPLIT_RE = re.compile(r"[；;。\n]")


def find_coupled_attrs(
    typical: Any,
    values_by_attr: Mapping[str, Any],
    *,
    exclude: str = "",
) -> dict[str, int]:
    """看 `typical`（本属性的样例）里**嵌着哪些别的属性的取值**。

    返回 `{另一属性名: 嵌了几个样例}`。判据是纯数据的：
    某样例里**包含**了另一属性的某个受控值（长度≥2，避免单字噪音）即算嵌着。

    例：样例 `竹制砧板` 含 `主体材质` 的受控值 `竹制` → 嵌着 1 个；
        样例里若 ≥`COUPLING_MIN` 个都这样，就认定本属性与它**语义耦合**。
    """
    out: dict[str, int] = {}
    ts = [str(t).strip() for t in (typical or []) if str(t).strip()]
    if not ts:
        return out
    for other, vals in (values_by_attr or {}).items():
        if other == exclude:
            continue
        cand = [str(v).strip() for v in (vals or [])
                if len(str(v).strip()) >= 2]
        if not cand:
            continue
        hit = sum(1 for t in ts if any(c in t for c in cand))
        if hit >= COUPLING_MIN:
            out[other] = hit
    return out


def _name_tokens(name: str) -> list[str]:
    """一个属性名的**可匹配写法**：全称 + 各后缀简称（长度≥2）。

    为什么需要（单测抓到的 bug）：hint 里常写**简称**而属性名是**全称** ——
    实测 `主体材质` 的 hint 只写「与**材质**区分：…」，
    精确匹配 `主体材质 in 子句` 直接失败 → 矛盾断言被漏掉、一个都没换。
    后缀取到就行：`主体材质` → `材质`；`产品类型` → `类型`。
    """
    n = str(name or "").strip()
    out = [n] if len(n) >= 2 else []
    for i in range(1, len(n) - 1):
        suf = n[i:]
        if len(suf) >= 2:
            out.append(suf)
    return out


def strip_coupling_contradiction(hint: Any, coupled: Mapping[str, Any]) -> tuple[str, list[str]]:
    """把 hint 里"与已耦合属性无关/要区分"的**自相矛盾子句**换掉。

    返回 `(新 hint, 被换掉的子句列表)`；没改动时第二项为空。
    **不删信息**：换掉的那句改成"要与它自洽"的建设性说法。

    匹配用全称**或**简称（见 `_name_tokens`），且子句必须同时含**独立性标记**
    （`无关/区分/不同/…`）才会被换 —— 只"提到"另一属性的正确说明不动。
    """
    s = str(hint or "")
    if not s or not coupled:
        return s, []
    parts = CLAUSE_SPLIT_RE.split(s)
    seps = CLAUSE_SPLIT_RE.findall(s)
    removed: list[str] = []
    for i, cl in enumerate(parts):
        names = [n for n in coupled
                 if n and any(tok in cl for tok in _name_tokens(n))]
        if not names:
            continue
        if not any(mk in cl for mk in INDEP_MARKERS):
            continue                      # 提到该属性但没下"无关"断言 → 不动
        removed.append(cl.strip())
        parts[i] = ("与 %s 保持自洽（本属性的样例里已嵌有它的语义，"
                    "证据指向的类别不在样例里时，照样例的构词方式补出来）"
                    % " / ".join(names))
    if not removed:
        return s, []
    out = parts[0]
    for i, sep in enumerate(seps):
        out += sep + (parts[i + 1] if i + 1 < len(parts) else "")
    return out, removed


# CJK 起头的候选取值：hint 里的 `/` 列表常混着英文说明，只留含中文的
_CJK = re.compile(r"[\u4e00-\u9fff]")
_POOL_TOK_RE = re.compile(r"[（()\[\]【】:：\s　]+")


def value_pool(hint: Any) -> list[str]:
    """从一个属性的 hint 里挖出**所有可能当"受控值"用的词**。

    ★ 为什么不能只用「典型取值」那一段（实测踩到的坑，2026-10-09）：
      冷启动时两个属性的样例**粒度不同**，子串匹配一个都对不上：
        `产品类型` 典型取值 = 实木砧板 / 竹制砧板 / 不锈钢砧板 / 玻璃砧板   ← 大类
        `主体材质` 典型取值 = 竹木 / 相思木 / 柚木 / PP塑料 / 304不锈钢     ← 具体树种/牌号
      `竹制砧板` 里并不含 `竹木`，于是**耦合检测为 0 → 矛盾断言一句都没换**。
      而 `主体材质` 的 hint **后半句**写着大类清单
      （「取值必须是收敛后的大类（竹制/木制/塑料/不锈钢/钛金属/…）」）——
      把 hint 全文里所有 `/` 分隔、含中文的片段**一起纳入判据池**，就能对上了。
    """
    out = list(extract_typical(hint))
    seen = {x for x in out}
    for seg in CLAUSE_SPLIT_RE.split(str(hint or "")):
        if "/" not in seg:
            continue
        for x in seg.split("/"):
            t = _POOL_TOK_RE.sub("", str(x)).strip()
            if len(t) >= 2 and _CJK.search(t) and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _looks_like_values(seg: str) -> bool:
    """判断「典型取值」后面那一段是**取值列表**还是**散文**。

    实测两种误判都要防：
      · 散文里偶然出现「典型取值」四个字 → 不能改（单测抓到）；
      · 取值很多、列表很长（如 25 个功率档）→ 必须改（放宽长度上限）。
    判据：取值列表用 `/` 分隔（本项目的约定）；散文用 `，`（中文逗号）。
    ⚠ 不用 `、` 判：受控值本身可能含 `、`（`风嘴、导梳`）。
    """
    s = str(seg or "").strip()
    if not s or len(s) > MAX_SEG:
        return False
    return "/" in s


def rewrite_hint(hint: Any, values: Any) -> str:
    """把 hint 的「典型取值 …」段改写为当前受控值；没有该段/不是列表则不动。"""
    s = str(hint or "")
    vals = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not s or not vals:
        return s
    m = TYPICAL_RE.search(s)
    if not m or not _looks_like_values(m.group(1)):
        return s
    shown = " / ".join(vals[:MAX_SHOWN]) + (" 等" if len(vals) > MAX_SHOWN else "")
    return TYPICAL_RE.sub("典型取值 " + shown, s, count=1)


def contradiction_sources(spec: Mapping[str, Any], values_by_attr: Mapping[str, Any]) -> dict[str, list[str]]:
    """算出判断耦合时**拿什么当"另一属性的取值"**。

    ★ 为什么不能只用 `rules.values`：**冷启动时词表是空的**，
      只靠词表 → 首次跑检测不出任何耦合 → 矛盾断言留到标注时才用 → 白跑一轮。
      而各属性 hint 里的「典型取值」**从一开始就有**（步骤16 生成），
      它本身就带语义（`竹制砧板` 里嵌着 `竹制`）→ 拿它当兜底判据，**首轮就能生效**。
    优先级：`rules.values`（法条，更权威） > hint 的「典型取值」（例子）。
    """
    out: dict[str, list[str]] = {}
    for a in (spec.get("attributes") or []):
        name = str(a.get("name") or "")
        if not name:
            continue
        vals = [str(v).strip() for v in (values_by_attr.get(name) or []) if str(v).strip()]
        if not vals:
            # 兜底：hint 的「典型取值」∪ hint 全文里的 `/` 列表（见 `value_pool` 说明）
            vals = value_pool(a.get("hint")) or value_pool(a.get("hint_short"))
        if vals:
            out[name] = vals
    return out


class SpecConcurrentChange(ValueError):
    """An attribute definition changed while annotation was in progress."""


def strip_all_contradictions(spec_path, values_by_attr=None, *, expected_revision=None):
    from .spec_io import spec_lock
    p = Path(spec_path)
    with spec_lock(p.parent):
        return _strip_all_contradictions_locked(p, values_by_attr, expected_revision=expected_revision)


def _strip_all_contradictions_locked(
    spec_path: str | Path,
    values_by_attr: Mapping[str, Any] | None = None,
    *, expected_revision=None,
) -> list[tuple[str, list[str]]]:
    """扫整份 spec，消掉所有「与已耦合属性无关」的自相矛盾断言。

    返回 `[(属性名, 被换掉的子句), …]`。**幂等**：再跑一次返回空。

    调用时机（★ 很重要）：**必须在标注之前**。实测 `sync_typical_values` 是步骤18
    调的（在标注之后），于是首轮标注仍然读到带矛盾的 hint → 20 个纯钛砧板全填
    `不锈钢砧板`。所以步骤17 起手就先跑这个。
    """
    p = Path(spec_path)
    if not p.is_file():
        return []
    try:
        spec = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if expected_revision is not None:
        from .spec_io import spec_revision
        if spec_revision(spec) != expected_revision:
            raise SpecConcurrentChange("属性集在准备标注期间发生变化，请重新运行步骤17")
    src = contradiction_sources(spec, values_by_attr or {})
    changed: list[tuple[str, list[str]]] = []
    for a in (spec.get("attributes") or []):
        name = str(a.get("name") or "")
        if not name:
            continue
        for key in ("hint", "hint_short"):
            if not a.get(key):
                continue
            typical = extract_typical(a[key])
            coupled = find_coupled_attrs(typical, src, exclude=name)
            if not coupled:
                continue
            new, removed = strip_coupling_contradiction(a[key], coupled)
            if removed:
                a[key] = new
                changed.append((name, removed))
    if changed:
        from .spec_io import write_json_atomic
        write_json_atomic(p, spec)
    return changed


def sync_typical_values(spec_path: str | Path, values_by_attr: Mapping[str, Any], *, expected_revision=None, revision_out=None) -> int:
    from .spec_io import spec_lock
    p = Path(spec_path)
    with spec_lock(p.parent):
        return _sync_typical_values_locked(p, values_by_attr, expected_revision=expected_revision, revision_out=revision_out)


def _sync_typical_values_locked(spec_path, values_by_attr, *, expected_revision=None, revision_out=None):
    """按 `{属性: 受控值列表}` 改写 spec 里各属性 hint 的「典型取值」段。

    返回改动的属性数。**只改「典型取值」那一段**，其余文字（判据、易混区分）一律不动。

    ★ 2026-10-09 追加 (a)：**同时消除"自相矛盾的断言"**。
      若某属性的样例确实嵌着另一属性的语义（`find_coupled_attrs`），
      就把 hint 里那句"与它无关/要区分"的断言**换掉**（见 `strip_coupling_contradiction`）。
      为什么放在这里：矛盾是在"生成 hint"时产生的，就该在**同一处**消掉 ——
      实测在两轮提示词里加固通用规则都**压不过**属性自己那一行的断言。
    """
    p = Path(spec_path)
    if not p.is_file():
        return 0
    try:
        spec = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0
    if expected_revision is not None:
        from .spec_io import spec_revision
        if spec_revision(spec) != expected_revision:
            raise SpecConcurrentChange("属性集在词表生成期间发生变化，请重新运行步骤17")
    n = 0
    for a in (spec.get("attributes") or []):
        name = str(a.get("name") or "")
        if name not in values_by_attr:
            continue
        for key in ("hint", "hint_short"):
            if not a.get(key):
                continue
            new = rewrite_hint(a[key], values_by_attr[name])
            # (a) 消除"与已耦合属性无关"的自相矛盾断言
            coupled = find_coupled_attrs(
                extract_typical(new) or extract_typical(a[key]),
                values_by_attr, exclude=name)
            if coupled:
                new2, removed = strip_coupling_contradiction(new, coupled)
                if removed:
                    new = new2
            if new != a[key]:
                a[key] = new
                n += 1
    if n:
        from .spec_io import write_json_atomic
        write_json_atomic(p, spec)
    if revision_out is not None:
        from .spec_io import spec_revision
        revision_out["revision"] = spec_revision(spec)
    return n
