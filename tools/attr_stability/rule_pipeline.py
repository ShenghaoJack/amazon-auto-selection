"""Execute declared rule types and project their columns into the delivery."""
from __future__ import annotations

import copy
import json
import math
from typing import Mapping

from .numeric_values import interval_bounds, numeric_target
from .std_rules import RULES, ST_REVIEW, ST_UNKNOWN, apply_rule, is_unknown

SPECIAL_RULES = {"color", "noise", "material", "layer_material", "count_modes", "ocr"}
_SUFFIXES = {
    "color": ["标准", "明细"],
    "noise": ["dB", "等级"], "material": ["主材质", "牌号", "明细"],
    "layer_material": ["层数", "材料", "材料种类数", "类型"],
    "count_modes": ["模式数", "模式集"], "ocr": ["文字", "信息类型"],
}


def delivery_columns(config):
    """Exclude rule-derived columns containing the retired numeric suffixes."""
    return [col for col in config["subcols"] if "_数值" not in col and "_档位" not in col]


def rule_configs(definitions: Mapping, *, cells=None) -> list[dict]:
    configs = []
    owners = {}
    names = {a.get("name") for a in definitions.get("attributes", []) if isinstance(a, Mapping)}
    for attr in definitions.get("attributes", []):
        if not isinstance(attr, Mapping) or not isinstance(attr.get("name"), str):
            raise ValueError("规则属性必须有字符串名称")
        cfg = copy.deepcopy(dict(attr))
        name = cfg["name"]
        if "rule" not in cfg and (cfg.get("value_shape") or cfg.get("shape")) == "multi":
            cfg["rule"] = "multi"
        legacy = "rule" not in cfg
        rule = cfg.setdefault("rule", cfg.get("type") or "enum")
        if rule in ("bool", "boolean"):
            cfg["rule"] = rule = "enum"
        if rule not in RULES:
            raise ValueError(f"{name}: 未知规则类型 {rule}")
        params = cfg.setdefault("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"{name}: params 必须是对象")
        params.setdefault("vocab_policy", cfg.get("vocab_policy", "closed"))
        params["unknown_values"] = list(dict.fromkeys(
            list(definitions.get("unknown_values") or []) + list(params.get("unknown_values") or [])
        ))
        if "values" not in params and "values" in cfg:
            params["values"] = cfg["values"]
        if "values" in params and not isinstance(params["values"], list):
            raise ValueError(f"{name}: values 必须是列表")
        for key in ("unit", "quantity", "_物理量", "on_parse_fail", "size_format", "if_pattern", "aliases"):
            if key not in params and key in cfg:
                params[key] = cfg[key]
        top_cols, param_cols = cfg.get("subcols"), params.get("subcols")
        if top_cols and param_cols and top_cols != param_cols:
            raise ValueError(f"{name}: 两处 subcols 声明不一致")
        cols = top_cols or param_cols
        if cols is None:
            cols = [f"{name}_{s}" for s in _SUFFIXES[rule]] if rule in _SUFFIXES else [name]
        if not isinstance(cols, list) or not cols or any(not isinstance(c, str) or not c.strip() for c in cols):
            raise ValueError(f"{name}: subcols 必须是非空列名列表")
        if len(set(cols)) != len(cols):
            raise ValueError(f"{name}: subcols 列名重复")
        required = len(_SUFFIXES.get(rule, []))
        if rule == "numeric":
            required = 1
        maximum = 2 if rule == "numeric" else required or 1
        if len(cols) < (required or 1) or len(cols) > maximum:
            raise ValueError(f"{name}: {rule} 输出列数不符，要求 {required or 1}–{maximum} 列")
        cfg["subcols"] = params["subcols"] = cols
        params.setdefault("subcol", cols[0])
        for col in cols:
            if col in names and col != name:
                raise ValueError(f"输出列 {col} 与其他属性名称冲突")
            if col in owners and owners[col] != name:
                raise ValueError(f"输出列 {col} 被多个属性声明")
            owners[col] = name
        order = definitions.get("value_order") or {}
        if isinstance(order, Mapping):
            if rule == "layer_material" and len(cols) > 1 and order.get(cols[1]):
                params.setdefault("material_order", order[cols[1]])
        if rule == "numeric":
            unit, quantity = numeric_target(params)
            if not unit:
                if not legacy:
                    raise ValueError(f"{name}: 数值规则缺少明确的目标单位或系数表")
                from .units import QUANTITY_BASE, parse_measure
                quantities = set()
                for key, cell in (cells or {}).items():
                    if key.partition("|")[2] != "attr." + name:
                        continue
                    for candidate in cell.get("values", []):
                        parsed = parse_measure(candidate.get("value"))
                        if parsed and parsed[1] != "ambiguous":
                            quantities.add(parsed[1])
                if len(quantities) == 1:
                    quantity = quantities.pop()
                    unit = QUANTITY_BASE[quantity]
                    cfg["unit_source"] = "旧格式：从本轮有单位的候选推断物理量，使用基准单位"
                else:
                    params["_unit_unresolved"] = True
                    cfg["unit_source"] = "旧格式缺少单位，且本轮无法确定单一物理量"
            params["unit"] = unit
            params["quantity"] = params["_物理量"] = quantity
        configs.append(cfg)
    return configs


def primary_value(cfg, output, raw):
    cols = cfg["subcols"]
    rule = cfg["rule"]
    if rule == "numeric":
        from .numeric_values import normalize_numeric
        normalized, status = normalize_numeric(raw, cfg["params"])
        return normalized if status != "failed" else "未明确"
    if rule == "noise":
        number = output.get(cols[0])
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            return f"{number:g}{cfg['params'].get('unit') or 'dB'}"
        return output.get(cols[1], "未明确")
    if rule == "layer_material":
        return output.get(cols[1], "未明确")
    if rule == "count_modes":
        modes = output.get(cols[1], "未明确")
        return raw if is_unknown(modes) and not is_unknown(output.get(cols[0])) else modes
    return output.get(cols[0], "未明确")


def _evidence(cells, rid, attr, raw, fallback):
    value_cell = cells.get(f"{rid}|attr.{attr}", {})
    runs = set()
    for nomination in value_cell.get("values", []):
        if str(nomination.get("value")) == str(raw):
            runs.update(nomination.get("by") or [])
    result = {}
    for suffix in ("来源", "依据"):
        nominations = cells.get(f"{rid}|attr.{attr}_{suffix}", {}).get("values", [])
        supported = [n.get("value") for n in nominations if runs.intersection(n.get("by") or []) and n.get("value")]
        result[suffix] = str(supported[0] if supported else fallback.get(f"{attr}_{suffix}") or "")
    return result


def project_columns(configs, res, rows, std):
    inputs = {int(r["xl_row"]): r for r in rows}
    report = []
    provenance = {}
    for row in res.deliverable:
        rid = int(row["xl_row"])
        attrs = row.get("attributes") or {}
        for cfg in configs:
            name = cfg["name"]
            final = std.get((rid, name), attrs.get(name, ""))
            raw = final
            supported = []
            # Preserve details (grade, layer count, etc.) from a nomination that
            # actually supports the standardized parent value.
            if not is_unknown(final):
                nominees = res.cells.get(f"{rid}|attr.{name}", {}).get("values", [])
                for nominee in nominees:
                    candidate = nominee.get("value")
                    out, status, _, _ = apply_rule(cfg, candidate, {"row": inputs.get(rid, {})})
                    if status not in (ST_REVIEW, ST_UNKNOWN) and str(primary_value(cfg, out, candidate)) == str(final):
                        supported.append((candidate, out, max(1, len(nominee.get("by") or []))))
                if supported:
                    raw = supported[0][0]
            output, status, method, issues = apply_rule(cfg, raw, {"row": inputs.get(rid, {})})
            execution_status = status
            output = {c: output.get(c, "未明确") for c in cfg["subcols"]}
            column_evidence = {}
            disagreements = {}
            for col in output:
                options = {}
                for candidate, candidate_out, votes in supported:
                    value = candidate_out.get(col, "未明确")
                    if is_unknown(value):
                        continue
                    key = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    option = options.setdefault(key, {"value": value, "votes": 0, "raw": candidate})
                    option["votes"] += votes
                if not options:
                    continue
                ordered = sorted(options.values(), key=lambda o: -o["votes"])
                if len(ordered) == 1 or ordered[0]["votes"] * 2 > sum(o["votes"] for o in ordered):
                    output[col] = ordered[0]["value"]
                    column_evidence[col] = _evidence(res.cells, rid, name, ordered[0]["raw"], attrs)
                else:
                    output[col] = "未明确"
                    status = ST_REVIEW
                    disagreements[col] = [{"值": o["value"], "支持数": o["votes"]} for o in ordered]
                    issues.append(f"{col}: 候选有分歧，未确定输出")
            if is_unknown(final):
                if cfg["rule"] == "numeric":
                    output = {c: "未明确" for c in output}
                if not is_unknown(attrs.get(name)):
                    status = ST_REVIEW
                    issues.append("原值未通过归并或前置校验，输出保持未明确")
            # Numeric output columns must never contain unparsed text or NaN.
            number_columns = []
            if cfg["rule"] in ("numeric", "noise", "count_modes"):
                number_columns = cfg["subcols"][:1]
            elif cfg["rule"] == "layer_material":
                number_columns = [cfg["subcols"][2]]
            for col in number_columns:
                v = output[col]
                if v == cfg['params'].get('on_parse_fail'):
                    continue
                if not is_unknown(v) and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                    # An explicit numeric interval is preserved by numeric rules.
                    if not (cfg["rule"] == "numeric" and interval_bounds(v) is not None and status != ST_REVIEW):
                        issues.append(f"{col}: 非法数值输出已改为未明确")
                        output[col] = "未明确"
                        status = ST_REVIEW
            if cfg["rule"] in ("enum", "multi"):
                from .converge import controlled_value_valid
                values = cfg["params"].get("values") or []
                if not values and cfg.get("vocab_policy", "closed") != "open":
                    values = list((cfg["params"].get("aliases") or {}).values())
                    values += list((cfg["params"].get("contains") or {}).values())
                    values += list((cfg["params"].get("recheck") or {}).values())
                    if not values and not is_unknown(final):
                        output = {c: "未明确" for c in output}
                        status = ST_REVIEW
                        issues.append("受控词表为空，无法产生已确认的标准值")
                for col, value in output.items():
                    if values and not is_unknown(value) and not controlled_value_valid(value, values, multi=cfg["rule"] == "multi"):
                        output[col] = "未明确"
                        status = ST_REVIEW
                        issues.append(f"{col}: 词表外输出已改为未明确")
            domains = {}
            if cfg["rule"] == "color":
                domains[cfg["subcols"][0]] = set((cfg["params"].get("color_map") or {}).values()) | set((cfg["params"].get("special") or {}).values()) | set(cfg["params"].get("values") or []) | {"多色"}
            elif cfg["rule"] == "material":
                domains[cfg["subcols"][0]] = set((cfg["params"].get("main_map") or {}).values())
            for col, allowed in domains.items():
                if allowed and not is_unknown(output[col]) and output[col] not in allowed:
                    output[col] = "未明确"
                    status = ST_REVIEW
                    issues.append(f"{col}: 未命中声明值域，已改为未明确")
            if (execution_status == ST_REVIEW or is_unknown(primary_value(cfg, output, raw))) and cfg["rule"] in SPECIAL_RULES | {"enum", "multi"}:
                std[(rid, name)] = "未明确"
            evidence = _evidence(res.cells, rid, name, raw, attrs)
            for col, value in output.items():
                # A numeric rule without explicit derived columns uses the
                # parent name internally; keep its approved value and unit.
                if not (cfg["rule"] == "numeric" and col == name):
                    std[(rid, col)] = value
                provenance[(rid, col)] = column_evidence.get(col, evidence)
            report.append({"行": rid, "属性": name, "规则": cfg["rule"], "原值": raw,
                           "标注原值": attrs.get(name), "目标单位": cfg["params"].get("unit"),
                           "单位声明来源": cfg.get("unit_source", "规则声明"),
                           "输出候选分歧": disagreements,
                           "状态": status, "方式": method, "输出": output, "问题": issues})
    return report, provenance
