# -*- coding: utf-8 -*-
"""确定性推导与收敛（2026-10-08）。**不调用 LLM**。

解决两个实测问题
===============================================================================
① `厚度` 与 `尺寸_拆列.厚` 互相矛盾（148 行里 147 行不一致）
   且分档本身有洞：`超薄≤3mm / 标准10-15 / 加厚20-45 / 超厚≥45`
   → **3–10mm 与 15–20mm 无档可归**（实测 70 个值里 22 个落洞里）。
   模型判档还系统性偏小（10mm→"超薄"、25mm→"标准"、61mm→"加厚"）。
   ⇒ 改为**从精确值确定性分档**，并把档位改成无缝分区。

② `主体材质` 20 个取值太碎（竹木/柚木/相思木/304不锈钢/PP塑料/钛金属+小麦秸秆PP…）
   ⇒ 按**分量**收敛：单材质归大类；组合值各分量分别归大类再拼回
      （同族去重后退化为单值）。**组合值必须保留** —— `不锈钢+PP塑料`
      这种双面异材质是有信息量的，不能压成"金属"。

声明写在 rules.json 里（不把类目知识藏进代码）::

    "params": {
      "values": [...],                         # 收敛后的受控值
      "converge": {"竹木": "竹制", ...},        # 分量 → 大类
      "derive_from": "尺寸_拆列.厚",            # 该属性由谁推导
      "bands": [["超薄(≤3mm)", 0, 3], ...]      # [名称, 下限mm, 上限mm]（闭区间）
    }
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

__all__ = ["parse_mm", "band_of", "converge_value", "derive_band",
           "apply_derive_and_converge"]

_MM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(cm|mm|m|in|inch|\"|英寸)?")


def parse_mm(v: Any) -> float | None:
    """把长度值解析成 mm。多值时取**第一个**（同一块板的长宽厚应一致）。
    识别不出单位按 cm（与 `_norm_size_dim` 的口径一致）。"""
    s = str(v or "").strip()
    if not s:
        return None
    m = _MM_RE.search(s)
    if not m:
        return None
    x = float(m.group(1))
    u = (m.group(2) or "cm").lower()
    if u == "cm":
        return x * 10.0
    if u == "mm":
        return x
    if u == "m":
        return x * 1000.0
    return x * 25.4                     # in / inch / "


def band_of(mm: float | None, bands: Sequence[Sequence[Any]]) -> str:
    """按 [名称, 下限, 上限] 的**闭区间**分档；落不进任何档返回 ""。"""
    if mm is None:
        return ""
    for b in bands or []:
        try:
            name, lo, hi = str(b[0]), float(b[1]), float(b[2])
        except (IndexError, TypeError, ValueError):
            continue
        if lo <= mm <= hi:
            return name
    return ""


def converge_value(raw: Any, converge: Mapping[str, str] | None) -> str:
    """按分量收敛。同族去重后退化为单值；组合保留 `A+B`（上限两个，金属在前）。"""
    s = str(raw or "").strip()
    if not s or s in ("未明确", "未标注", "不适用", "未知", "待确认", "None"):
        return s
    table = converge or {}
    parts = [p.strip() for p in s.split("+") if p.strip()]
    mapped: list[str] = []
    for p in parts:
        t = table.get(p, p)
        if t and t not in mapped:
            mapped.append(t)
    if not mapped:
        return s
    if len(mapped) == 1:
        return mapped[0]
    order = {"金属": 0, "不锈钢": 1, "钛金属": 1, "竹制": 2, "木制": 3,
             "塑料": 4, "玻璃": 5, "石材": 6, "硅胶": 7, "树脂": 8}
    mapped.sort(key=lambda x: order.get(x, 99))
    return "+".join(mapped[:2])


def derive_band(dim_value: Any, bands: Sequence[Sequence[Any]]) -> str:
    """由精确尺寸值推出分档名。"""
    return band_of(parse_mm(dim_value), bands)


def apply_derive_and_converge(
    std: dict,
    attrs_spec: Mapping[str, Mapping[str, Any]],
    cells: Mapping[str, Any] | None = None,
) -> tuple[dict, dict]:
    """就地修正 `std[(行号, 属性)]`。

      · `derive_from` 的属性 → 用来源属性的精确值确定性分档（覆盖模型判的档）
      · 有 `converge` 的属性 → 按分量收敛

    `cells`：`稳定性结果.json` 的原始 cells。**必须传** —— `std` 是
    `converge_mechanical(res.cells, declared, …)` 的产物，而 `declared` 只含
    **规格书属性**，像 `尺寸_拆列.厚` 这种过程字段**不在其中**，所以 `std` 里根本没有它，
    推导会全部落空（实测：44 处不一致纹丝不动，日志里"来源为空"爆表）。
    传进来后按 `"<行号>|<路径>"` 直接读。

    返回 `(新的 std, 统计)`；**不改值的语义**：推不出来就保留原值，绝不清空。
    """

    def _cell_value(rid, path):
        if not cells:
            return ""
        d = cells.get("%s|%s" % (rid, path))
        if not isinstance(d, Mapping):
            return ""
        v = str(d.get("mode") or "").strip()
        if v:
            return v
        for c in (d.get("values") or []):
            if isinstance(c, Mapping) and str(c.get("value") or "").strip():
                return str(c["value"]).strip()
        return ""

    stat = {"推导": 0, "推导跳过(来源空)": 0, "推导跳过(无档可归)": 0, "收敛": 0}

    def _params_of(spec) -> Mapping:
        """取声明所在的层。

        ★ 形状兼容（踩过的坑）：生产里 `attrs_spec[name]` 是**扁平**的
        （`derive_from` / `bands` / `values` 直接在该层），而单元测试/探针常传
        `{"params": {...}}` 的嵌套形状。原先只读 `spec["params"]` →
        生产里永远读到空 → 推导**从不执行**；而探针用嵌套形状却"跑通"，
        于是出现「探针绿、真跑不生效」这种最难查的偏差。两种都支持。
        """
        p = spec if isinstance(spec, Mapping) else {}
        inner = p.get("params")
        return inner if isinstance(inner, Mapping) else p

    # ---- 推导 ----
    for attr, spec in (attrs_spec or {}).items():
        params = _params_of(spec)
        src = str(params.get("derive_from") or "").strip()
        bands = params.get("bands") or []
        if not src or not bands:
            continue
        # ★ 行号取 **std ∪ cells 的并集**：只取 std 会漏掉"没有收敛条目"的行
        #   （实测：70 行有 `厚`，却只有 57 行被推导到，另 13 行因 std 无条目而跳过）。
        rids = {rid for (rid, a) in std if a == attr}
        for k in (cells or {}):
            rid_s, _, path = str(k).partition("|")
            if path == src and rid_s.isdigit():
                rids.add(int(rid_s))
        for rid in sorted(rids, key=lambda x: str(x)):
            sv = str(std.get((rid, src)) or "").strip() or _cell_value(rid, src)
            if not sv:
                stat["推导跳过(来源空)"] += 1
                continue
            band = derive_band(sv, bands)
            if not band:
                stat["推导跳过(无档可归)"] += 1
                continue
            if std.get((rid, attr)) != band:
                std[(rid, attr)] = band
                stat["推导"] += 1

    # ---- 「值→值」的收敛表**已按用户要求整段移除** ----
    # 2026-10-08 决定：**不做枚举**。手写 `params.converge`（原值→大类）是类目专用硬编码，
    # 换类目即失效，与「通用、自动」相悖。
    # 收敛改由**既有的 LLM 归类链路**完成，代码里不该有任何值→值的表：
    #     converge_mechanical()  →  build_classify_payload()  →  LLM  →  apply_classification()
    # 实测（Cutting Boards 100 行）：`主体材质` 16 个原值 → LLM 全部正确映射到 8 个受控值
    # （竹木→竹制、柚木→木制、304不锈钢+小麦秸秆PP→不锈钢+塑料 …），零枚举。
    # 类目只需在 rules.json 里声明**目标值域**（`params.values`）——那是业务决定，不是枚举。
    return std, stat
