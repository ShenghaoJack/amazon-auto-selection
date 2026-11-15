# -*- coding: utf-8 -*-
"""规则类型引擎：把「归类 + 标准化」拆成可复用的规则类型，参数全部来自 rules.json。

设计原则（与整个项目一致）
============================================================================
· **机制通用、规则声明化**：每个类目只写一份 rules.json，声明"每个属性用哪个规则类型
  + 传什么参数/词表"；引擎代码任何类目都不改。
· **兜底链固定**：机械归并 → 判不出 → 待人工复核（绝不硬猜）。
· 每个规则类型返回 (子列值 dict, 归类状态, 归类方式, 问题清单)。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from .unknowns import is_unknown as common_is_unknown

UNKNOWN = {"未明确", "未知", "待确认", "N/A", "na", "无", "空白",
           "未发现明确证据", "未发现明确运行模式"}

ST_DIRECT = "已直接归类"
ST_SYN = "已同义词归类"
ST_SPLIT = "已复合值拆分"
ST_UNIT = "已单位转换"
ST_UNKNOWN = "未明确—无证据"
ST_REVIEW = "待人工复核—语义无法判断"
ST_VISION = "已图片视觉推断"
ST_VISION_CHECK = "已图片视觉复核"
ST_RECHECK = "已复核改判"
MT_DIRECT = "直接归类"
MT_SYN = "同义词归类"
MT_SPLIT = "复合值拆分"
MT_UNIT = "数值提取/单位转换"
MT_UNKNOWN = "原值为空或未明确"
MT_REVIEW = "无法安全归类"
MT_VISION = "图片视觉推断"
MT_VISION_CHECK = "图片视觉复核"
MT_RECHECK = "语义改判"


def norm(v: Any) -> str:
    return "" if v is None else str(v).strip()


def is_unknown(v: Any) -> bool:
    s = norm(v)
    return common_is_unknown(v) or s in (UNKNOWN - {"无"})


def _split(s: str, seps: list[str]) -> list[str]:
    """按分隔符拆词（保序去空白）。"""
    pat = "|".join(re.escape(x) for x in seps)
    return [x.strip() for x in re.split(pat, s) if x.strip()]


def _parse_num_unit(s: str) -> tuple[Optional[float], str]:
    m = re.match(r"^([\d.]+)\s*([A-Za-z%μ°Ω]+)$", s)
    if m:
        return float(m.group(1)), m.group(2)
    return None, ""


# ================================================================ 规则类型
def rule_enum(v, params, ctx) -> tuple[dict, str, str, list]:
    """受控值/别名单值收敛。params: values, aliases, recheck, contains"""
    subcol = params["subcol"]
    if is_unknown(v):
        return {subcol: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    name = params.get("name", "属性")
    recheck = params.get("recheck") or {}
    aliases = params.get("aliases") or {}
    values = params.get("values") or []
    contains = params.get("contains") or {}
    if s in recheck:
        m = recheck[s]
        note = (params.get("recheck_note") or "%(name)s原值「%(v)s」已复核改判为%(m)s") \
            % {"name": name, "v": s, "m": m}
        return {subcol: m}, ST_RECHECK, MT_RECHECK, [note]
    if s in aliases:
        m = aliases[s]
        st, mt = (ST_DIRECT, MT_DIRECT) if m == s else (ST_SYN, MT_SYN)
        return {subcol: m}, st, mt, []
    if s in values:
        return {subcol: s}, ST_DIRECT, MT_DIRECT, []
    for pat, then in contains.items():
        if pat in s:
            return {subcol: then}, ST_DIRECT, MT_DIRECT, []
    if params.get("vocab_policy") == "open":
        return {subcol: s}, ST_DIRECT, MT_DIRECT, []
    return {subcol: s}, ST_REVIEW, MT_REVIEW, ["%s=%s" % (name, s[:20])]


def rule_multi(v, params, ctx) -> tuple[dict, str, str, list]:
    """多值按词收敛。params: subcol, values, aliases, split_seps, join_sep"""
    subcol = params["subcol"]
    if is_unknown(v):
        return {subcol: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    special = params.get("special") or {}
    if s in special:
        return {subcol: special[s]}, ST_DIRECT, MT_DIRECT, []
    aliases = params.get("aliases") or {}
    toks = _split(s, params.get("split_seps", ["+", "、", ",", "，", ";", "；", "/"]))
    mapped = [aliases.get(t, t) for t in toks]
    mapped = list(dict.fromkeys(mapped))
    if not mapped:
        return {subcol: s}, ST_REVIEW, MT_REVIEW, [params.get("name", "属性") + "=" + s[:20]]
    return {subcol: params.get("join_sep", "+").join(mapped)}, ST_SPLIT, MT_SPLIT, []


def rule_color(v, params, ctx) -> tuple[dict, str, str, list]:
    """颜色：单色→主色(别名)；多色(+)→主色=多色 + 明细。params: subcols[主色,明细], color_map, special"""
    sc_main, sc_detail = params["subcols"]
    if is_unknown(v):
        return {sc_main: "未明确", sc_detail: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    cmap = params.get("color_map") or {}
    special = params.get("special") or {}
    if s in special:
        return {sc_main: special[s], sc_detail: s}, ST_SYN, MT_SYN, []
    toks = _split(s, params.get("split_seps") or ["+", "＋", "、", ",", "，", ";", "；", "/"])
    if len(toks) > 1:
        unknown = [t for t in toks if t not in cmap and t not in cmap.values() and t not in (params.get("values") or [])]
        if unknown:
            return {sc_main: "未明确", sc_detail: s}, ST_REVIEW, MT_REVIEW, ["未识别颜色: " + "+".join(unknown)]
        mapped = list(dict.fromkeys([cmap.get(t, t) for t in toks]))
        return {sc_main: "多色", sc_detail: "+".join(mapped)}, ST_SPLIT, MT_SPLIT, []
    main = cmap.get(s)
    if main is None and (s in cmap.values() or s in (params.get("values") or [])):
        main = s
    if main is None:
        return {sc_main: s, sc_detail: s}, ST_REVIEW, MT_REVIEW, ["颜色=" + s[:20]]
    if main == s:
        return {sc_main: main, sc_detail: s}, ST_DIRECT, MT_DIRECT, []
    return {sc_main: main, sc_detail: s}, ST_SYN, MT_SYN, []


def rule_numeric(v, params, ctx) -> tuple[dict, str, str, list]:
    """数字+单位换算 + 可选分档。params: subcols[数值,分档?], unit_factors, bands, unit"""
    subcols = params["subcols"]
    if is_unknown(v):
        return {c: "未明确" for c in subcols}, ST_UNKNOWN, MT_UNKNOWN, []
    from .numeric_values import band_for, normalize_numeric, numeric_target
    text, status = normalize_numeric(v, params)
    if status == "failed":
        fallback = params.get("on_parse_fail") or "未明确"
        return {c: fallback for c in subcols}, ST_REVIEW, MT_REVIEW, [params.get("name", "属性") + "=" + norm(v)[:40]]
    unit, quantity = numeric_target(params)
    number_text = text[:-len(unit)]
    try:
        number = float(number_text)
    except ValueError:  # An interval cannot be assigned a single band safely.
        from .numeric_values import interval_bounds
        bounds = interval_bounds(number_text)
        if bounds is None or (params.get("zero_issue") and bounds[0] <= 0 <= bounds[1]):
            return {c: "未明确" for c in subcols}, ST_REVIEW, MT_REVIEW, ["数值区间不可信"]
        issues = ["保留数值区间，不指定单一档位"]
        if status == "assumed":
            issues.append("原值无单位，按声明目标单位处理")
        return {c: number_text if i == 0 else "未明确" for i, c in enumerate(subcols)}, ST_UNIT, MT_UNIT, issues
    if params.get("zero_issue") and number == 0:
        return {c: "未明确" for c in subcols}, ST_REVIEW, MT_REVIEW, [params.get("name", "属性") + "=0 异常"]
    number = int(number) if number.is_integer() else number
    out = {c: "未明确" for c in subcols}
    out[subcols[0]] = number
    if params.get("bands") and len(subcols) > 1:
        from .num_check import band_units_consistent
        ok, why = band_units_consistent(params["bands"], quantity, unit)
        if not ok:
            return out, ST_REVIEW, MT_REVIEW, [why]
        out[subcols[1]] = band_for(number, params["bands"])
    issues = ["原值无单位，按声明目标单位处理"] if status == "assumed" else []
    return out, ST_UNIT, MT_UNIT, issues


def rule_noise(v, params, ctx) -> tuple[dict, str, str, list]:
    """噪音：dB数值 + 静音等级。

    params: subcols[dB,等级], special, level_threshold,
            vision_override{原值: {db, level, note}}, min_plausible_db

    物理下限闸门（2026-09-20，用户报「噪音_dB_标准 出现异常值 0」）:
      实测 B0AF13FD56 卖点写「operating volume of 0 dB」、B09D6EDDE1 写
      「remains at zero decibels in the standby mode」，机械「数值提取/单位转换」
      一律产出 0。0 dB 是**人耳听阈**，整机（含水泵）不可能是 0 dB；且 B09D6EDDE1
      自述的是 **standby mode 待机**噪音，并非运行噪音——两者混为一谈会污染整个
      噪音列。故：低于 `min_plausible_db` 的数值不产出 dB，降级为「未明确」+「静音」
      并转待人工/视觉复核，绝不把 0 当噪音写进交付表。
      图片视觉复核结论（见 vision_override）优先于机械提取。
    """
    sc_db, sc_lv = params["subcols"]
    if is_unknown(v):
        return {sc_db: "未明确", sc_lv: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    vo = params.get("vision_override") or {}
    if s in vo:
        m = vo[s]
        return {sc_db: m.get("db", "未明确"), sc_lv: m.get("level", "未明确")}, \
            ST_VISION_CHECK, MT_VISION_CHECK, \
            ["噪音原值「%s」→图片视觉复核: %s" % (s[:20], m.get("note", ""))]
    special = params.get("special") or {}
    if s in special:
        return {sc_db: "未明确", sc_lv: special[s]}, ST_SYN, MT_SYN, []
    from .numeric_values import normalize_numeric
    unit = params.get("unit") or "dB"
    normalized, parsed_status = normalize_numeric(s, {"unit": unit})
    if parsed_status != "ok":
        return {sc_db: "未明确", sc_lv: "未明确"}, ST_REVIEW, MT_REVIEW, ["噪音=" + s[:20]]
    db = float(normalized[:-len(unit)])
    db = int(db) if db.is_integer() else db
    floor = params.get("min_plausible_db")
    if floor is not None and db < float(floor):
        return {sc_db: "未明确", sc_lv: "静音"}, ST_REVIEW, MT_REVIEW, \
            ["噪音=%s 低于物理下限 %s dB, 疑营销宣称/待机噪音, 待图片视觉复核"
             % (s[:20], floor)]
    lv = "静音" if db <= params.get("level_threshold", 30) else "普通"
    return {sc_db: db, sc_lv: lv}, ST_UNIT, MT_UNIT, []


def rule_material(v, params, ctx) -> tuple[dict, str, str, list]:
    """复合材质 → 主材质 + 牌号 + 明细。params: subcols[主,牌号,明细], main_map, token_map, grade_hints, vision_override"""
    sc_main, sc_grade, sc_detail = params["subcols"]
    if is_unknown(v):
        return {sc_main: "未明确", sc_grade: "未明确", sc_detail: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    vo = params.get("vision_override") or {}
    if s in vo:
        m = vo[s]
        return {sc_main: m["main"], sc_grade: "未明确", sc_detail: m["detail"]}, \
            ST_VISION, MT_VISION, ["材质原值「%s」→图片视觉推断为%s" % (s, m["main"])]
    main_map = params.get("main_map") or {}
    token_map = params.get("token_map") or {}
    toks = _split(s, ["+", "＋", "、", ",", "，", ";", "；", "/"])
    if not toks:
        return {c: "未明确" for c in params["subcols"]}, ST_REVIEW, MT_REVIEW, ["未识别材质"]
    normed = [token_map.get(t, t) for t in toks]
    grade = "未明确"
    for t in toks:
        for hint, gv in (params.get("grade_hints") or []):
            if hint in t:
                grade = gv
                break
        if grade != "未明确":
            break
    main = main_map.get(toks[0], toks[0])
    if len(toks) == 1 and normed[0] == toks[0] and main == toks[0]:
        st, mt = ST_DIRECT, MT_DIRECT
    elif len(toks) == 1 and normed[0] != toks[0]:
        st, mt = ST_SYN, MT_SYN
    else:
        st, mt = ST_SPLIT, MT_SPLIT
    return {sc_main: main, sc_grade: grade, sc_detail: "+".join(normed)}, st, mt, []


def rule_layer_material(v, params, ctx) -> tuple[dict, str, str, list]:
    """层数(营销宣称) + 材料集 + 材料种类数。

    **层数与材料种类数是两个不同概念** —— 一个滤芯可以 8 层但只用 5 种材料
    （同种材料叠多层是行业常见做法），故两者不交叉校验。
    params: subcols[层数,材料,材料种类数,类型], material_map, special, prefix_res, suffix_res
    """
    subcols = params["subcols"]
    sc_layer, sc_mat, sc_cnt, sc_type = subcols
    if is_unknown(v):
        return {c: "未明确" for c in subcols}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    special = params.get("special") or {}
    if s in special:
        return dict(zip(subcols, special[s])), ST_DIRECT, MT_DIRECT, []
    layer = "未明确"
    m = re.search(params.get("layer_re", r"(\d+)\s*层"), s)
    if m:
        layer = int(m.group(1))
    elif "多层" in s or "多重" in s:
        layer = "多层"
    body = s
    for pr in params.get("prefix_res", []):
        body = re.sub(pr, "", body)
    for sr in params.get("suffix_res", []):
        body = re.sub(sr, "", body)
    # 括号归一（2026-09-21 新增，声明式 `strip_parens`）: 原值形态 `2层过滤（活性炭+海绵）`
    # 在前缀正则剥掉「2层过滤」后剩下 `（活性炭+海绵）`，全角括号被当成材料名的一部分
    # → 拆出 `（活性炭` / `海绵）` 两个词表外材料。实测 猫饮水机 4 行受影响。
    if params.get("strip_parens"):
        body = body.strip("（）()［］[] \t")
    mmap = params.get("material_map") or {}
    toks = [t for t in _split(body, ["+", "、", ",", "，", ";", "；", "/"]) if t not in ("过滤", "层")]
    mats = list(dict.fromkeys([mmap.get(t, t) for t in toks]))
    unrecognized = [t for t in toks if mmap and t not in mmap and t not in mmap.values()]
    if unrecognized:
        return {sc_layer: layer, sc_mat: "未明确", sc_cnt: "未明确",
                sc_type: "有过滤系统（材料未明确）"}, ST_REVIEW, MT_REVIEW, ["未识别过滤材料: " + "+".join(unrecognized)]
    if not mats:
        mats = ["未明确"]
    # 取值内顺序归一（2026-09-21 声明式 `material_order`）: 源表枚举顺序随机，
    # 同一材料集合会碎成多种写法（实测 过滤材料 有 24 组同集合不同顺序、涉 360 行），
    # 直接 group by 会把 1 个类目算成 7 个。按声明的物理流经顺序排列。
    mo = params.get("material_order")
    if mo and mats != ["未明确"]:
        idx = {m: i for i, m in enumerate(mo)}
        mats = sorted(mats, key=lambda m: (idx.get(m, len(mo)), m))
    mat_str = "+".join(mats)
    mat_cnt = len(mats) if mats != ["未明确"] else "未明确"
    ftype = "有过滤系统（材料已明确）" if mats != ["未明确"] else "有过滤系统（材料未明确）"
    return {sc_layer: layer, sc_mat: mat_str, sc_cnt: mat_cnt, sc_type: ftype}, ST_SPLIT, MT_SPLIT, []


def rule_count_modes(v, params, ctx) -> tuple[dict, str, str, list]:
    """模式数 + 模式集。params: subcols[数,模式集], mode_map, special, count_re, prefix_res"""
    sc_cnt, sc_modes = params["subcols"]
    if is_unknown(v):
        return {sc_cnt: "未明确", sc_modes: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    for sp in (params.get("special") or []):
        if sp.get("contains") in s:
            st = {"DIRECT": (ST_DIRECT, MT_DIRECT), "SYN": (ST_SYN, MT_SYN),
                  "REVIEW": (ST_REVIEW, MT_REVIEW)}.get(sp.get("status", "DIRECT"), (ST_DIRECT, MT_DIRECT))
            iss = [params.get("name", "属性") + "=" + s[:20]] if sp.get("status") == "REVIEW" else []
            return {sc_cnt: sp.get("cnt", "未明确"), sc_modes: sp.get("modes", "未明确")}, st[0], st[1], iss
    cnt = "未明确"
    m = re.search(params.get("count_re", r"(\d+)\s*种"), s)
    if m:
        cnt = int(m.group(1))
    body = s
    for pr in params.get("prefix_res", []):
        body = re.sub(pr, "", body)
    # 括号归一（2026-09-21，声明式 `strip_parens`，与 rule_layer_material 同口径）:
    # 原值 `2种水流（已明确：喷泉模式）` 在前缀正则处理后可能剩下 `喷泉模式）`
    # → 尾括号被当成模式名的一部分。实测 水流模式 13 行受影响。
    if params.get("strip_parens"):
        body = body.strip("（）()［］[] \t")
    body = body.rstrip("）)")
    mmap = params.get("mode_map") or {}
    toks = _split(body, ["+", "、", ",", "，", ";", "；", "/"])
    modes = []
    unrecognized = []
    for t in toks:
        t2 = mmap.get(t)
        if t2 is None and params.get("strip_suffix"):
            t3 = re.sub(params["strip_suffix"], "", t)
            t2 = mmap.get(t3, t3 if t3 in mmap.values() else None)
        if t2 is None and t in mmap.values():
            t2 = t
        if t2 is None:
            unrecognized.append(t)
        modes.append(t2)
    modes = list(dict.fromkeys([m for m in modes if m]))
    if not modes:
        modes = ["未明确"]
    # `count_from_modes`（2026-09-21 新增，声明式）: 原值没有写「N种」但**枚举了模式名**时，
    # 用枚举出的模式个数作为模式数。理由: 既然这个原值可信到能拿来填「模式集」子列，
    # 就该同样可信地推出「模式数」——否则同一行会出现「模式=X+Y 但 数量=未明确」的自相矛盾。
    # 实测 猫饮水机 水流模式: `水龙头模式、气泡模式` 这类枚举原值有 72 行被判成未明确。
    if cnt == "未明确" and params.get("count_from_modes") and modes != ["未明确"] and not unrecognized:
        cnt = len(modes)
    if unrecognized:
        return {sc_cnt: cnt, sc_modes: "+".join(modes)}, ST_REVIEW, MT_REVIEW, ["未识别模式: " + "+".join(unrecognized)]
    if isinstance(cnt, int) and modes != ["未明确"] and cnt < len(modes):
        return {sc_cnt: "未明确", sc_modes: "+".join(modes)}, ST_REVIEW, MT_REVIEW, ["声明模式数小于已识别模式数量"]
    return {sc_cnt: cnt, sc_modes: "+".join(modes)}, ST_SPLIT, MT_SPLIT, []


def rule_open(v, params, ctx) -> tuple[dict, str, str, list]:
    """开放型：清洗归一。params: subcol"""
    subcol = params["subcol"]
    if is_unknown(v):
        return {subcol: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    return {subcol: re.sub(r"\s+", " ", norm(v))}, ST_DIRECT, MT_DIRECT, []


def rule_ocr(v, params, ctx) -> tuple[dict, str, str, list]:
    """图片OCR：统一分隔符 + 信息类型识别。params: subcols[文字,类型], ocr_keys"""
    sc_txt, sc_type = params["subcols"]
    if is_unknown(v):
        return {sc_txt: "未明确", sc_type: "未明确"}, ST_UNKNOWN, MT_UNKNOWN, []
    s = norm(v)
    s2 = re.sub(r"\s*\|\s*", " | ", s)
    s2 = re.sub(r"\s*[；;]\s*", " | ", s2)
    s2 = re.sub(r"[ \t]+", " ", s2)
    types = []
    for label, pat in (params.get("ocr_keys") or []):
        if re.search(pat, s2):
            types.append(label)
    tstr = "、".join(dict.fromkeys(types)) if types else "未识别"
    return {sc_txt: s2, sc_type: tstr}, ST_DIRECT, MT_DIRECT, []


# ================================================================ 分发
RULES = {
    "enum": rule_enum,
    "multi": rule_multi,
    "color": rule_color,
    "numeric": rule_numeric,
    "noise": rule_noise,
    "material": rule_material,
    "layer_material": rule_layer_material,
    "count_modes": rule_count_modes,
    "open": rule_open,
    "ocr": rule_ocr,
}


def apply_rule(attr_cfg: Mapping, value: Any, ctx: Mapping) -> tuple[dict, str, str, list]:
    """按属性配置调用对应规则类型。attr_cfg 来自 rules.json 的 attributes[]。
    返回 (子列值 dict, 状态, 方式, 问题清单)。ctx 含 row（用于 ASIN 级改判）。"""
    rule = attr_cfg.get("rule", "enum")
    params = dict(attr_cfg.get("params") or {})
    params.setdefault("name", attr_cfg.get("name", "属性"))
    subcols = attr_cfg.get("subcols") or params.get("subcols") or [attr_cfg.get("name", "属性") + "_标准"]
    params.setdefault("subcols", subcols)
    params.setdefault("subcol", subcols[0])
    declared_unknowns = {str(v).strip().lower() for v in params.get("unknown_values", [])} - {"无", "否"}
    if common_is_unknown(value) or norm(value).lower() in declared_unknowns:
        return {c: "未明确" for c in subcols}, ST_UNKNOWN, MT_UNKNOWN, []
    fn = RULES.get(rule)
    if fn is None:
        return {}, ST_REVIEW, MT_REVIEW, ["未知规则类型 %s" % rule]
    try:
        return fn(value, params, ctx)
    except Exception as e:
        return {}, ST_REVIEW, MT_REVIEW, ["异常:%s" % str(e)[:30]]
