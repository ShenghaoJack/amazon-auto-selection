# -*- coding: utf-8 -*-
"""数值类属性的**异常值校验与修正**（标准化环节）。

**为什么需要它**
----------------
`rule_numeric` 只做"数值+单位换算+分档"，**不判断结果是否可信**。实测三类真异常
（Dog Dryer 标准化交付表，`scripts/_plan_pilot/_num_anomaly_scan.txt`）：

| 现象 | 实测 |
|---|---|
| 标准化数值在**原文里找不到对应**（`容量=55`、`线长=2`、`最大风速=72`） | **5 + 10 + 18 行** |
| 同属性内**量级离群**（单位识别错，10^n 级并存） | 属性列未出现（输入列的长尾属真实分布，**不该当异常**） |
| **档位与数值不符** | 需按 bands 重算 |

**设计原则（类目无关）**
------------------------
· **不写死属性的物理范围** —— 范围随类目变，写死就失效（见改动清单 §12）
· 判据来自两类通用事实：
  ① **物理量常识**：长度/体积/重量/功率/速度… **不可能为 0 或负**（`units` 的物理量表）
  ② **该属性自身的分布**：量级离群用中位数倍数（类目自适应，不用绝对阈值）
  ③ **与证据一致**：标准化数值应能在该行原文（标题/五点/详细参数）里找到对应
· **能确定性修的当场修**（档位重算）；**不能确定的不许留假数**（清空/标待复核），
  并产出**重跑指令**（行号+属性+问题）供"重新修改"

**修正档位**
------------
  · `清空`      值不可信且无证据可依 → 置空（等价于没判出来），登记
  · `重算档位`  数值可信但档位与 bands 不符 → 按数值重算
  · `待重跑`    值可能来自上游误判 → 保留原值 + 列入重跑指令（不清空，避免丢信息）
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .units import convert, parse_measure, quantity_of_unit
from .unknowns import is_unknown
from .numeric_values import interval_bounds

__all__ = ["check_attribute", "check_cells", "build_retry_items", "apply_fixes",
           "evidence_numbers", "band_units_consistent",
           "CHECK_ZERO_NEG", "CHECK_MAGNITUDE", "CHECK_BAND", "CHECK_NO_EVIDENCE"]

CHECK_ZERO_NEG = "非正数值(物理量不可能为0或负)"
CHECK_MAGNITUDE = "量级离群(疑似单位识别错)"
CHECK_BAND = "档位与数值不符"
CHECK_NO_EVIDENCE = "原文无对应数值(疑似上游误判)"

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_MAG_FACTOR = 100.0        # 与中位数差 ≥100 倍视为量级离群（跨 2 个数量级）
_EV_TOL = 0.05             # 与原文数值的一致性容差（5%）
_EV_COLS = ("标题", "亮点", "五点描述", "详细参数", "SKU")


def _nums(text: str) -> list[float]:
    return [float(m.group(0)) for m in _NUM_RE.finditer(text or "")]


def _as_number(v: Any) -> float | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    pm = parse_measure(s)
    return pm[0] if pm else None


# 文本里的「数值 + 单位」（如 `3.9 oz` / `110ml` / `8 inches`）
_NUM_UNIT_RE = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*"
    r"([A-Za-z%µ°³/]{1,12}|[\u4e00-\u9fff]{1,4})?")


def evidence_numbers(text: str, quantity: str | None = None,
                     target_unit: str = "") -> list[float]:
    """从原文里抽出数值，并**换算到目标单位**（单位感知）。

    为什么必须单位感知：实测 `容量` 标准化为 `110`(ml)，而原文写 `3.9 oz`
    —— 不做换算就会当成"原文无对应"，实测误报 **1000 格**。
    这里把文本里的「数值+单位」按 `parse_measure` 解析；能解析且物理量相符的
    换算到 `target_unit`，解析不出的按原数字保留（宽松兜底）。
    """
    out: list[float] = []
    if target_unit:
        for interval in re.finditer(
            r"[-+]?\d+(?:[.,]\d+)?\s*(?:[A-Za-z%µ°³/]{1,12}|[\u4e00-\u9fff]{1,4})?"
            r"\s*[-~–—至]\s*[-+]?\d+(?:[.,]\d+)?\s*(?:[A-Za-z%µ°³/]{1,12}|[\u4e00-\u9fff]{1,4})?",
            text or "",
        ):
            endpoints = interval_bounds(interval.group(), target_unit)
            if endpoints:
                out.extend(endpoints)
    for m in _NUM_UNIT_RE.finditer(text or ""):
        n = float(m.group(1))
        u = (m.group(2) or "").strip()
        if u:
            pm = parse_measure(f"{n} {u}")
            if pm is not None:
                _v, q, su = pm
                if quantity and q == quantity:
                    if target_unit:
                        c = convert(_v, su, target_unit)
                        out.append(c if c is not None else _v)
                    else:
                        out.append(_v)
                    continue
                if quantity and q not in (quantity, "ambiguous"):
                    continue          # 物理量不符 → 不是这个属性的证据
        out.append(n)
    return out


def _evidence_text(row: Mapping[str, Any]) -> str:
    return " ".join(str(row.get(c) or "") for c in _EV_COLS)


def band_units_consistent(bands: Sequence[Sequence[Any]] | None,
                          quantity: str | None,
                          unit: str = "") -> tuple[bool, str]:
    """**分档单位一致性守卫**：档位标签里的单位必须与属性的物理量/单位一致。

    为什么必需（实测事故 2026-10-07）：`吸力大小` 的**属性单位是 Pa**，
    而它的 `bands` 是从别处带来的**风速**档位（`≤50m/s / 50-100m/s / ≥100m/s`）。
    不做守卫时"档位与数值不符"检查会把 `11000Pa` 判成 `≥100m/s` —— **荒谬修正**。

    判据：从每个档位标签里抽出单位，凡能识别且**物理量与属性不同** → 判不一致。
    **以属性的「声明目标单位」为准**（`unit` 优先，其次 `quantity`）—— 实测踩过：
    `吸力大小` 是混量属性（pressure/speed 都有），其"主口径"被算成 **speed**，
    而属性声明单位是 `Pa`；若只用主口径判定就会认为档位口径一致，
    照样把 `11000Pa` 判成 `≥100m/s`。
    返回 `(是否一致, 原因)`。
    """
    if not bands:
        return True, ""
    want = (quantity_of_unit(unit) if unit else None) or quantity
    if not want or want == "ambiguous":
        return True, ""
    for b in bands:
        if not isinstance(b, (list, tuple)) or len(b) < 3:
            continue
        label = band_label_of(b)
        for m in re.finditer(r"[A-Za-zµ°³/]{1,12}|[\u4e00-\u9fff]{1,4}", label):
            tok = m.group(0)
            q = quantity_of_unit(tok)
            if q and q != "ambiguous" and q != want:
                return False, ("档位标签 %r 里的单位 %r 属 %s，而属性是 %s(%s) → 分档口径不符"
                               % (label, tok, q, want, unit or "?"))
    return True, ""


def check_attribute(
    attr: str,
    values: Mapping[Any, Any],
    rows_by_id: Mapping[Any, Mapping[str, Any]] | None = None,
    *,
    bands: Sequence[Sequence[Any]] | None = None,
    quantity: str | None = None,
    unit: str = "",
    mag_factor: float = _MAG_FACTOR,
    ev_tol: float = _EV_TOL,
) -> dict[str, Any]:
    """校验单个数值属性的所有格。返回 `{问题: [...], 统计: {...}}`。

    `values`：`{行号: 值}`；`bands`：`[[lo,hi,label], …]`（有则查档位一致性）。
    """
    problems: list[dict[str, Any]] = []
    nums: dict[Any, float] = {}
    intervals = {}
    for rid, v in values.items():
        # 哨兵（未明确/未标注/不适用…）不是"非数值异常"，它们是合法的"没信息"
        if is_unknown(v):
            continue
        bounds = interval_bounds(v, unit)
        if bounds is not None:
            intervals[rid] = bounds
            if bounds[0] < 0 or (bounds[0] == 0 and quantity not in (None, "count")):
                problems.append({"行": rid, "值": str(v), "类型": CHECK_ZERO_NEG,
                                 "说明": "数值区间包含不可信的非正物理量"})
            continue
        n = _as_number(v)
        if n is None:
            problems.append({"行": rid, "值": str(v), "类型": "非数值",
                             "说明": "该属性声明为数值型但值解析不出数值"})
            continue
        nums[rid] = n
    if not nums:
        return {"属性": attr, "问题": problems, "统计": {
            "格数": len(values), "可解析": len(intervals), "区间格数": len(intervals)}}

    # 中位数**只用正值** —— 否则 0/负值（正是我们要检出的异常）会把基准拉偏，
    # 进而污染"量级离群"的判据（实测：含 0 时中位从 55 掉到 27.5）。
    pos = [n for n in nums.values() if n > 0]
    med = statistics.median(pos) if pos else 0.0
    # ★ 分档单位守卫：口径不符时**不做档位检查**（否则会出现 `11000Pa → ≥100m/s` 这种荒谬修正）
    bands_ok, bands_why = band_units_consistent(bands, quantity, unit)
    if not bands_ok:
        problems.append({"行": None, "值": None, "类型": "分档口径不符",
                         "说明": bands_why})
        bands = None
    for rid, n in nums.items():
        # ① 物理量不可能为 0 或负；**计数值（无物理单位）允许 0**，但负数仍不可能。
        #    实测误报：`配件数量=0` 被判成"物理量不可能为0" —— 其实"0 件配件"是合法业务值。
        allow_zero = not quantity or quantity == "count"
        if n < 0 or (n == 0 and not allow_zero):
            problems.append({"行": rid, "值": n, "类型": CHECK_ZERO_NEG,
                             "说明": ("物理量为 %s，%s不可信"
                                      % (quantity, "0/负值" if not allow_zero else "负值"))})
            continue
        # ② 量级离群（单位识别错 → 差 10^n 倍）。注意 `n` 可能为 0（计数值）→ 必须先判 `n > 0`
        if n > 0 and med > 0 and (n / med >= mag_factor or med / n >= mag_factor):
            problems.append({"行": rid, "值": n, "类型": CHECK_MAGNITUDE,
                             "说明": "与同属性中位 %.6g 差 %.0f 倍" % (med, max(n / med, med / n))})
        # ③ 档位与数值不符
        if bands:
            want = None
            for b in bands:
                # ★ 用 `band_range_of` 而不是原地解包 —— 原地解包假设 `[lo,hi,档名]`，
                #   而项目里是 `[档名,lo,hi]`，会让 `n > lo` 拿字符串比 →
                #   TypeError → 整个数值异常检查被静默跳过（实测）。
                lo, hi, label = band_range_of(b)
                if (lo is None or n > lo) and (hi is None or n <= hi):
                    want = label
                    break
            if want is not None:
                band_key = "%s_档位" % attr
                got = str((rows_by_id or {}).get(rid, {}).get(band_key) or "") \
                    if rows_by_id else ""
                if got and got != want:
                    problems.append({"行": rid, "值": n, "类型": CHECK_BAND,
                                     "说明": "档位应为 %r，实为 %r" % (want, got),
                                     "修正": {"列": band_key, "新值": want}})
    return {
        "属性": attr,
        "问题": problems,
        "统计": {"格数": len(values), "可解析": len(nums) + len(intervals),
                 "区间格数": len(intervals), "中位": med,
                 "最小": min(nums.values()), "最大": max(nums.values()),
                 "类型计数": dict(Counter(p["类型"] for p in problems)),
                 "单位": unit, "物理量": quantity},
    }


def check_no_evidence(
    attr: str,
    values: Mapping[Any, Any],
    rows_by_id: Mapping[Any, Mapping[str, Any]],
    *,
    quantity: str | None = None,
    target_unit: str = "",
    ev_tol: float = _EV_TOL,
) -> list[dict[str, Any]]:
    """④ **与证据一致**：标准化数值应能在该行原文里找到对应（容差 `ev_tol`）。

    **单位感知**（2026-10-07 修）：原文数值先按 `parse_measure` 解析并换算到
    `target_unit` 再比 —— 否则 `容量` 标准化 `110`(ml) 与原文 `3.9 oz` 对不上，
    实测误报 1000 格。

    实测 Dog Dryer：`容量=55` 5 行、`线长=2` 10 行、`最大风速=72` 18 行
    在原文里**找不到任何对应数值** → 这些值缺乏依据（多半是上游把枚举成员判错了，
    或视觉/归类猜的）。这类**不清空**（保留信息）但必须列入重跑指令。
    """
    out: list[dict[str, Any]] = []
    for rid, v in values.items():
        if is_unknown(v):
            continue
        bounds = interval_bounds(v, target_unit)
        n = _as_number(v) if bounds is None else None
        if n is None and bounds is None:
            continue
        # ★ 计数值的**单品默认值**不算异常（用户口径 2026-10-09）。
        #   为什么：`套装数量=1件` 在原文没写"1 Pack/Set of 1"时，是**合理的默认推断**
        #   （单品本来就 1 件），不是"上游误判"。实测 100 行里 16 处告警有 15 处是
        #   `套装数量=1` —— 属噪音，会把真异常（如 `=10`）淹掉。
        #   判据：**无物理量**（计数值，如"件/个"）+ 值为 1 → 放过。
        #   ⚠ 只放过 1：`=10` 之类仍要报（那才是真误读）。
        if bounds is None and quantity in (None, "count") and abs(n - 1.0) <= 1e-9:
            continue
        row = rows_by_id.get(rid)
        if not row:
            continue
        txt = _evidence_text(row)
        if not txt.strip():
            continue
        cands = evidence_numbers(txt, quantity=quantity, target_unit=target_unit)
        if not cands:
            continue
        required = bounds or (n,)
        if not all(any(abs(c - endpoint) <= max(1e-9, abs(endpoint) * ev_tol) for c in cands)
                   for endpoint in required):
            out.append({"行": rid, "值": str(v) if bounds else n, "类型": CHECK_NO_EVIDENCE,
                        "说明": "原文数值样例 %s 中无对应数值 %s"
                                % (sorted(set(round(c, 3) for c in cands))[:6], required)})
    return out


def band_label_of(b: Any) -> str:
    """从一个档位三元组里取**档名**，兼容两种格式。

    ★ 2026-10-09 实测发现的格式分歧（这是 `CHECK_BAND` 从来没生效的**第二个**原因）：
      · `rules.json` 里实际写的是 **`[档名, lo, hi]`** ——
        `厚度 bands = [['超薄(≤3mm)', 0, 3], ['标准(3-20mm)', 3, 20], …]`
      · 而 `check_attribute` 假设的是 **`[lo, hi, 档名]`**
        （`lo, hi, label = (list(b) + [None]*3)[:3]` → 它拿到的是 `lo='超薄(≤3mm)'`）
      所以**取"那个不是数字的元素"当档名**，两种格式都对。
    """
    for x in list(b or []):
        if isinstance(x, str) and x.strip():
            return x.strip()
    return ""


def band_range_of(b: Any) -> tuple[Any, Any, str]:
    """从一个档位三元组里取出 `(lo, hi, 档名)`，**兼容两种顺序**。

    ★ 2026-10-09 实测（真实数据 `厚度`）：项目里写的是 `[档名, lo, hi]`，
    而原实现假设 `[lo, hi, 档名]`。两者相遇时 `check_attribute` 会
    **抛 `TypeError: '>' not supported between 'float' and 'str'`** ——
    而它的调用点是 `except Exception: 忽略` 包着的 → **整个数值异常检查被静默跳过**。

    做法：先定位**档名**（那个非空字符串）的位置，剩下的两个按原顺序即 `(lo, hi)`。
    这样 `[None,3,档名]` → `(None,3)`、`[档名,0,3]` → `(0,3)` 都对。
    """
    row = list(b or [])
    li = next((i for i, x in enumerate(row)
               if isinstance(x, str) and x.strip()), None)
    rest = [x for i, x in enumerate(row) if i != li]
    lo = rest[0] if len(rest) > 0 else None
    hi = rest[1] if len(rest) > 1 else None
    return lo, hi, band_label_of(b)


def check_cells(
    cells: Mapping[tuple[Any, str], Any],
    attrs_spec: Mapping[str, Mapping[str, Any]],
    rows_by_id: Mapping[Any, Mapping[str, Any]] | None = None,
    *,
    numeric_attrs: Iterable[str] | None = None,
    with_evidence: bool = True,
    raw_values: Mapping[tuple[Any, str], Any] | None = None,
) -> dict[str, Any]:
    """对声明为数值型的属性批量校验。

    `attrs_spec[attr]` 可含 `unit` / `bands` / `_物理量` / `values` 等。
    `raw_values`：`{(行号, 属性): 原值}` —— 用于**口径一致性**检查（见下）。

    ★ **声明口径与取值不符**（2026-10-07 实测事故）：`吸力大小` 被声明为
    `unit=m/s`（速度）且档位也是 m/s，但它的受控值是 **Pa**（`11000Pa`/`15000Pa`）
    —— 即 0-9 混量警告的那个属性。此时若照常做"档位与数值不符"检查，
    会把 `11000Pa` **修正**成 `≥100m/s`（荒谬）。所以：一旦发现**取值的单位与声明物理量不符**，
    登记该问题并**跳过该属性的档位检查**。

    返回 `{"属性": {attr: 结果}, "可修": [...], "待重跑": [...], "统计": {...}}`。
    """
    numeric_attrs = list(numeric_attrs if numeric_attrs is not None else
                         [a for a, s in attrs_spec.items()
                          if str((s or {}).get("type") or "").lower() == "numeric"])
    by_attr: dict[str, dict[Any, Any]] = defaultdict(dict)
    for (rid, attr), v in cells.items():
        if attr in numeric_attrs:
            by_attr[attr][rid] = v
    out_attr: dict[str, Any] = {}
    fixable: list[dict[str, Any]] = []
    retry: list[dict[str, Any]] = []
    stat: Counter = Counter()
    for attr in numeric_attrs:
        spec = dict(attrs_spec.get(attr) or {})
        vals = by_attr.get(attr) or {}
        if not vals:
            continue
        want_q = spec.get("_物理量") or quantity_of_unit(str(spec.get("unit") or ""))
        # ---- 口径一致性：**逐行**取值的单位 vs 声明的物理量 ----
        # 只把**口径不符的那些行**排除出档位检查，而不是整个属性跳过 ——
        # 实测 S&P `容量` 混了 volume(`3oz`)/weight(`200g`)：整属性跳过会让
        # 231 个**本可正确修正**的档位全部漏掉。
        bad_rows: dict[Any, Any] = {}
        if raw_values and want_q:
            for rid in vals:
                pm = parse_measure(raw_values.get((rid, attr), ""))
                if pm and pm[1] not in (want_q, "ambiguous"):
                    bad_rows[rid] = raw_values[(rid, attr)]
        vals_ok = {rid: v for rid, v in vals.items() if rid not in bad_rows}
        # 档位格也参与（bands 校验用）
        rows2 = dict(rows_by_id or {})
        if rows2:
            bk = "%s_档位" % attr
            for rid in list(vals):
                r = dict(rows2.get(rid) or {})
                if bk not in r:
                    r[bk] = cells.get((rid, bk), "")
                rows2[rid] = r
        res = check_attribute(attr, vals_ok, rows2,
                              bands=spec.get("bands"),
                              quantity=spec.get("_物理量"),
                              unit=str(spec.get("unit") or ""))
        if bad_rows:
            why = ("声明物理量=%s(单位 %s)，但这些格的取值是别的物理量：%s"
                   " → 档位口径不可信，**这些格不做档位检查**"
                   % (want_q, spec.get("unit") or "?",
                      [(r, str(v)) for r, v in list(bad_rows.items())[:4]]))
            res["问题"].append(
                {"行": list(bad_rows)[0], "值": str(list(bad_rows.values())[0]),
                 "类型": "声明口径与取值不符", "说明": why})
            stat["声明口径与取值不符"] += 1
            retry.append({"属性": attr, "行": list(bad_rows)[0],
                          "值": str(list(bad_rows.values())[0]),
                          "类型": "声明口径与取值不符", "说明": why})
        if with_evidence and rows_by_id:
            res["问题"].extend(check_no_evidence(
                attr, vals, rows_by_id,
                quantity=spec.get("_物理量"), target_unit=str(spec.get("unit") or "")))
        for p in res["问题"]:
            if p.get("类型") != "声明口径与取值不符":
                stat[p["类型"]] += 1
            if p["类型"] in (CHECK_ZERO_NEG, CHECK_MAGNITUDE):
                fixable.append({"属性": attr, **p})
            elif p["类型"] in (CHECK_NO_EVIDENCE, "非数值"):
                retry.append({"属性": attr, **p})
            elif p["类型"] == CHECK_BAND and p.get("修正"):
                fixable.append({"属性": attr, **p})
        res["统计"]["类型计数"] = dict(Counter(p["类型"] for p in res["问题"]))
        out_attr[attr] = res
    # ★ 2026-10-09：原先这里有一段「带 `bands` 但非数值型 → 查档名合法性」的循环。
    #   用户口径「**bands 没必要存在**」后已删除 —— 档位声明取消，就没有档名可查。
    #   档名的合法性本来就由**结构闸门**保证（值必须在 `params.values` 内），
    #   所以删掉它并没有丢覆盖。
    return {"属性": out_attr, "可修": fixable, "待重跑": retry,
            "统计": {"数值属性数": len([a for a in numeric_attrs if by_attr.get(a)]),
                     "问题总数": sum(stat.values()), "按类型": dict(stat)}}


def apply_fixes(cells: dict, report: Mapping[str, Any],
                clear_types: Sequence[str] = (CHECK_ZERO_NEG,),
                ) -> tuple[dict, list[dict[str, Any]]]:
    """执行**确定性修正**：档位重算 + 指定类型的清空。

    `clear_types` 默认只清 `非正数值`（物理量不可能为 0/负 → 留着就是假数）。
    `量级离群`/`原文无对应` **不清空**（信息可能有用），只登记 + 进重跑指令。
    """
    applied: list[dict[str, Any]] = []
    for p in (report.get("可修") or []):
        attr = p.get("属性")
        rid = p.get("行")
        if p["类型"] == CHECK_BAND and p.get("修正"):
            k = (rid, p["修正"]["列"])
            old = cells.get(k, "")
            if str(old) != str(p["修正"]["新值"]):
                cells[k] = p["修正"]["新值"]
                applied.append({"行": rid, "属性": attr, "动作": "重算档位",
                                "原值": old, "新值": p["修正"]["新值"]})
        elif p["类型"] in clear_types:
            k = (rid, attr)
            old = cells.get(k, "")
            if str(old) != "":
                cells[k] = ""
                applied.append({"行": rid, "属性": attr, "动作": "清空(不可信)",
                                "原值": old, "新值": ""})
    return cells, applied


def build_retry_items(report: Mapping[str, Any], limit: int = 200) -> str:
    """把"待重跑"整理成**指名到行的重跑指令**（供"发现问题→重新修改"）。

    形式与 `attr_stability.build_retry_brief` 一致：只列问题点，要求其余字段不要重做。
    """
    items = report.get("待重跑") or []
    if not items:
        return ""
    by_attr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        by_attr[it["属性"]].append(it)
    lines = ["# 数值类属性异常值重跑指令", "",
             "以下格子的标准化数值**缺乏证据或不可信**，请**只重做这些格子**，",
             "其余字段保持不变；并对每个格子给出：`值` + `来源`。", ""]
    for attr, arr in sorted(by_attr.items(), key=lambda kv: -len(kv[1])):
        lines.append("## %s（%d 格）" % (attr, len(arr)))
        for it in arr[:limit]:
            lines.append("- 行 %s：%s —— %s"
                         % (it.get("行"), it.get("类型"), it.get("说明")))
        lines.append("")
    return "\n".join(lines)
