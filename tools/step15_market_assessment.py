"""Step 15 market-demand and competition assessment, based on the supplied workbook.

All measures describe the collected sample. Missing inputs are never treated as zero.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
from core.step15_display import format_text


# section, display name, formula, default threshold, threshold expression, reserved legacy slot
SPECS = [
 ("市场需求", "核心词搜索量", "从步骤8生效核心词中筛选搜索量>阈值且CPC<阈值的词，逐词列出搜索量。", 30000, "> {limit:g} 次/月", ""),
 ("市场需求", "核心词数量", "满足搜索量及CPC条件的生效核心词个数。", 2, "> {limit:g} 个", ""),
 ("市场需求", "CPC竞价", "已筛选核心词的最高CPC；筛选条件本身含CPC阈值。", 3, "< {limit:g} 美元", ""),
 ("市场需求", "平均客单价", "步骤14样本变体价格算术平均，不是订单客单价。", 35, "> {limit:g} 美元", ""),
 ("市场需求", "月需求量估算", "步骤14样本变体月销量合计 ÷ 样本变体数。", 500, "≥ {limit:g} 件/变体/月", ""),
 ("市场需求", "类目GMV估算", "步骤14样本变体月销额之和，并非全市场GMV。", 100000, "≥ {limit:g} 美元/月", ""),
 ("市场需求", "趋势方向（站内）", "各核心词最近连续3个月逐词同比；同月相加后计算同比，再对3个月取均值。", -20, "> {limit:g}%", ""),
 ("市场需求", "趋势方向（Google）", "各核心词Google指数同月相加后计算近3个月同比，再求均值；指数之和不代表搜索量。", -20, "> {limit:g}%", ""),
 ("市场需求", "旺季月份", "仅当有季节性时，按近24个月核心词搜索量合计取最高3个月。", None, "仅展示", ""),
 ("市场需求", "淡季月份", "仅当有季节性时，按近24个月核心词搜索量合计取最低3个月。", None, "仅展示", ""),
 ("市场需求", "季节波动系数", "达标核心词每月搜索量相加，近24个月拆为两个连续12个月，分别计算合计搜索量最高月÷最低月，以两年中较大值判定；单词峰谷比仅供参考。", 2.5, "各词合计每年峰谷比 ≤ {limit:g}", ""),
 ("竞争格局", "头部链接销售额占比", "样本中销额前5个变体ASIN销额 ÷ 样本总销额。", 60, "< {limit:g}%", ""),
 ("竞争格局", "头部品牌集中度", "按品牌汇总样本销额，前5品牌销额 ÷ 样本总销额。", 60, "< {limit:g}%", ""),
 ("竞争格局", "卖家集中度", "按BuyBox卖家汇总样本销额，前5卖家销额 ÷ 样本总销额。", 60, "< {limit:g}%", ""),
 ("竞争格局", "新卖家占比", "上架≤180天商品的BuyBox卖家去重数及样本销额占比；满足任一条件。", 10, "新品卖家数 > {limit:g} 或新品销额占比 > {limit2:g}%", ""),
 ("竞争格局", "大卖占比", "评分数>1000的样本变体销额 ÷ 样本总销额。", 50, "< {limit:g}%", ""),
 ("竞争格局", "VC卖家占比", "配送方式为AMZ的样本变体销额 ÷ 样本总销额，作为自营近似口径。", 20, "< {limit:g}%", ""),
 ("竞争格局", "本土卖家占比", "卖家所属地与站点国家一致的有效样本销额 ÷ 所属地已知的样本总销额；与配送方式无关。", 20, "< {limit:g}%", ""),
 ("竞争格局", "评论壁垒", "按父ASIN汇总变体月销量，前10个父体各取最大评分数，再求算术平均。", 5000, "< {limit:g} 条评分", ""),
 ("竞争格局", "评分", "样本有效变体评分算术平均；0视为无有效评分。", 4.2, "≥ {limit:g} 分", ""),
 ("竞争格局", "老品占比", "上架天数超过1095天的样本变体销额 ÷ 上架天数及销额有效的样本变体销额，不代表全部老品。", 70, "< {limit:g}%", ""),
]

COLUMNS = ["优先级", "指标", "原表标准", "原表备注及口径", "实际值", "判定", "证据与限制", "板块", "单位"]
ASSESSMENT_VERSION = "2026-11-season-total-v1"


def key(section, name):
    return f"{section}·{name}"


def defaults():
    result = {}
    for section, name, _, limit, template, _ in SPECS:
        rule = {"enabled": True, "standard": template}
        if limit is not None:
            rule["limit"] = float(limit)
        if name == "新卖家占比":
            rule["limit2"] = 20.0
        rule["standard"] = standard(template, rule)
        result[key(section, name)] = rule
    return result


def standard(template, rule):
    return template.format(limit=rule.get("limit", 0), limit2=rule.get("limit2", 0))


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(str(value).replace(",", "").replace("$", "").strip())
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def _col(df, names):
    lookup = {str(c).strip().casefold(): c for c in df.columns}
    for name in names:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    return None


def _numbers(df, names, upper=None):
    col = _col(df, names)
    if col is None:
        return None
    values = df[col].map(_num)
    if upper is not None:
        values = values.where(values <= upper)
    return values


def _labels(df, names):
    col = _col(df, names)
    if col is None:
        return None
    out = df[col].fillna("").astype(str).str.strip()
    return out.mask(out.str.casefold().isin(("", "未知", "nan", "none", "null", "-", "--", "n/a", "na")))


def _complete(series):
    return series is not None and len(series) > 0 and series.notna().all()


def _month_shift(month, offset):
    year, mon = map(int, month.split("-"))
    count = year * 12 + mon - 1 + offset
    return f"{count // 12:04d}-{count % 12 + 1:02d}"


def _series(value, google=False):
    if google and isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = ((x.get("month"), x.get("search_volume")) for x in value if isinstance(x, dict))
    else:
        return {}
    out = {}
    for month, value in entries:
        month = str(month or "")[:7]
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            number = _num(value)
            if number is not None:
                out[month] = number
    return out


def _json(path, fallback):
    try:
        with open(path, encoding="utf-8-sig") as f:
            value = json.load(f)
        return value if isinstance(value, type(fallback)) else fallback
    except (OSError, ValueError, TypeError):
        return fallback


def assess(split_df, keywords, root, cat, currency, site, settings, trend_month=""):
    rows = []
    frame = split_df.reset_index(drop=True)
    asin = _labels(frame, ["变体ASIN", "ASIN"])
    if asin is None:
        base_mask = pd.Series(False, index=frame.index)
    else:
        base_mask = asin.notna() & ~asin.str.upper().duplicated(keep=False)
    sales = _numbers(frame, ["变体月销量"])
    revenue = _numbers(frame, ["变体月销额"])
    rating = _numbers(frame, ["评分", "rating"], 5)
    counts = _numbers(frame, ["评分数", "评分数量", "rating_count", "ratings_count"])
    if counts is not None:
        counts = counts.where(counts.mod(1) == 0)
    ages = _numbers(frame, ["上架天数"])
    price_cols = [c for c in frame.columns if re.match(r"^(?:价格|price)(?:\([^)]*\))?$", str(c).strip(), re.I)]
    prices_col = next((c for c in price_cols if "(" in str(c)), price_cols[0] if price_cols else None)
    prices = _numbers(frame, [str(prices_col)]) if prices_col is not None else None
    if prices is not None:
        prices = prices.where(prices > 0)
    parent = _labels(frame, ["父ASIN", "parent_asin"])
    seller = _labels(frame, ["BuyBox卖家", "buybox seller", "购物车卖家"])
    brand = _labels(frame, ["品牌", "brand"])
    shipping = _labels(frame, ["配送方式"])
    location = _labels(frame, ["卖家所属地"])
    def mask_for(*fields):
        mask = base_mask.copy()
        for field in fields:
            mask &= field.notna() if field is not None else False
        return mask

    def coverage(mask):
        return f"有效样本 {int(mask.sum())}/{len(frame)} 个变体；缺失、无效及重复ASIN行不纳入该指标。"
    dataset = _json(Path(root) / f"{cat}-amazon-keyword-data.json", [])
    monthly = _json(Path(root) / f"{cat}-amazon-monthly.json", {})
    records = {str(x.get("keyword", "")).strip().casefold(): x for x in dataset if isinstance(x, dict) and x.get("keyword")}
    monthly = {str(k).strip().casefold(): v for k, v in monthly.items()}
    kw = keywords if isinstance(keywords, pd.DataFrame) else pd.DataFrame()
    kw_name = _col(kw, ["关键词"])
    vol_col = _col(kw, ["站内月搜索量"])
    cpc_col = _col(kw, ["CPC"])
    volume_limit = settings[key("市场需求", "核心词搜索量")]["limit"]
    cpc_limit = settings[key("市场需求", "CPC竞价")]["limit"]
    candidates, incomplete, seen = [], [], set()
    if kw_name is not None:
        for _, row in kw.iterrows():
            word = str(row[kw_name] or "").strip()
            if not word or word.casefold() in seen or word.casefold() in ("nan", "搜索配置", "搜索口径", "口径", "方法", "备注"):
                continue
            seen.add(word.casefold())
            rec = records.get(word.casefold(), {})
            volume = _num(row[vol_col]) if vol_col is not None else _num(rec.get("monthly_search_volume"))
            cpc = _num(row[cpc_col]) if cpc_col is not None else _num(rec.get("cpc"))
            if volume is None or cpc is None:
                incomplete.append(word)
            elif volume > volume_limit and cpc < cpc_limit:
                candidates.append((word, volume, cpc, rec))
    words = [x[0] for x in candidates]
    station = {w: _series(monthly.get(w.casefold())) for w in words}
    google = {w: _series(rec.get("google_monthly_index"), True) for w, _, _, rec in candidates}
    target = str(trend_month or "")
    if re.fullmatch(r"\d{6}", target):
        target = target[:4] + "-" + target[4:]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", target):
        target = "9999-12"

    def latest_month(series):
        return max((m for values in series.values() for m in values if m <= target), default=None)

    def trend(series):
        end = latest_month(series)
        if not end:
            return None, "缺少核心词逐月数据"
        months = [_month_shift(end, i) for i in (-2, -1, 0)]
        usable = {w: values for w, values in series.items()
                  if all(m in values and _month_shift(m, -12) in values and values[_month_shift(m, -12)] > 0
                         for m in months)}
        if not usable:
            return None, f"截至{end}，无核心词具备连续3个月及去年同期数据"
        detail, totals = [], []
        for w, values in usable.items():
            yoy = []
            for m in months:
                old = _month_shift(m, -12)
                yoy.append((values[m] / values[old] - 1) * 100)
            detail.append(w + "：" + "、".join(f"{m} {format_text(f'{v:+.12f}%')}" for m, v in zip(months, yoy)))
        for m in months:
            prev = sum(v[_month_shift(m, -12)] for v in usable.values())
            totals.append((sum(v[m] for v in usable.values()) / prev - 1) * 100)
        detail.insert(0, f"有效核心词 {len(usable)}/{len(series)}，截至{end}；数据不完整的词已排除")
        detail.append("合计：" + "、".join(f"{m} {format_text(f'{v:+.12f}%')}" for m, v in zip(months, totals)))
        return sum(totals) / 3, "；".join(detail)

    def season():
        end = latest_month(station)
        if not end:
            return None, "缺少核心词逐月数据", None, None
        months = [_month_shift(end, i) for i in range(-23, 1)]
        usable = {word: values for word, values in station.items() if all(m in values for m in months)}
        if not usable:
            return None, "所有核心词的近24个月数据不完整", None, None
        details = [f"有效核心词 {len(usable)}/{len(station)}，截至{end}；数据不完整的词已排除；各词同月搜索量相加，并非独立搜索用户数"]
        for word, values in usable.items():
            parts = []
            for year in (months[:12], months[12:]):
                hi, lo = max(year, key=values.get), min(year, key=values.get)
                if values[lo] == 0:
                    parts.append(f"谷值为0，单词峰谷比不可计算（峰{hi} / 谷{lo}）")
                else:
                    parts.append(f"{values[hi] / values[lo]:.2f}（峰{hi} / 谷{lo}）")
            details.append(f"{word}（参考）：" + "、".join(parts))
        combined = {m: sum(v[m] for v in usable.values()) for m in months}
        combined_parts, combined_ratios = [], []
        for year in (months[:12], months[12:]):
            peak, valley = max(year, key=combined.get), min(year, key=combined.get)
            if combined[valley] <= 0:
                return None, f"合计搜索量在{valley}为0，无法计算峰谷比；" + "；".join(details), None, None
            ratio = combined[peak] / combined[valley]
            combined_ratios.append(ratio)
            combined_parts.append(f"{ratio:.2f}（峰{peak} / 谷{valley}）")
        details.append("各词合计（以两年中较大值判定）：" + "、".join(combined_parts))
        high = sorted(months, key=lambda m: (-combined[m], m))[:3]
        low = sorted(months, key=lambda m: (combined[m], m))[:3]
        return max(combined_ratios), "；".join(details), high, low

    season_ratio, season_evidence, high, low = season()
    station_trend, station_detail = trend(station)
    google_trend, google_detail = trend(google)
    for section, name, formula, _, template, _ in SPECS:
        rule = settings[key(section, name)]
        limit = rule.get("limit")
        value, status, evidence = "", "数据不足", formula
        ok = None
        if name in ("核心词搜索量", "核心词数量", "CPC竞价"):
            evidence += "；候选范围为步骤8生效核心词；仅搜索量严格大于阈值且CPC严格低于阈值才计入。"
            if incomplete:
                evidence += "缺少搜索量或CPC、已排除：" + "、".join(incomplete)
            evidence += f"；可评估 {len(seen) - len(incomplete)}/{len(seen)} 个核心词。"
            if candidates:
                if name == "核心词搜索量":
                    value = "；".join(f"{w}: {v:g}" for w, v, _, _ in candidates)
                    ok = True
                elif name == "核心词数量":
                    value = len(candidates)
                    ok = len(candidates) > limit
                else:
                    value = "；".join(f"{w}: ${c:g}" for w, _, c, _ in candidates) + f"；最高 ${max(x[2] for x in candidates):g}"
                    evidence += " 已按CPC阈值预筛选，该项达标不代表未入选词CPC达标。"
                    ok = True
            elif kw_name is not None and len(seen) > len(incomplete):
                value = 0 if name == "核心词数量" else "无符合筛选条件的核心词"
                ok = False if name != "CPC竞价" else None
            if currency != "USD":
                ok = None
                evidence += "；CPC为站点币种，未换算美元，无法应用美元阈值。"
        elif name in ("平均客单价", "月需求量估算", "类目GMV估算"):
            values = prices if name == "平均客单价" else sales if name == "月需求量估算" else revenue
            mask = mask_for(values)
            evidence += "；" + coverage(mask)
            if mask.any():
                if name == "平均客单价":
                    measure = float(values[mask].mean())
                    value, ok = round(measure, 2), measure > limit if currency == "USD" else None
                    evidence += f"；币种{currency or '未知'}；不等同成交客单价。"
                elif name == "月需求量估算":
                    measure = float(values[mask].mean())
                    value, ok = measure, measure >= limit
                    evidence += f"；有效样本销量合计{values[mask].sum():g}；显示值取整，按未取整均值判定。"
                else:
                    measure = float(values[mask].sum())
                    value, ok = measure, measure >= limit if currency == "USD" else None
                    evidence += f"；币种{currency or '未知'}；显示值以百万为单位，按未取整数值判定；仅代表有效样本，不代表全市场。"
        elif name in ("趋势方向（站内）", "趋势方向（Google）"):
            number, detail = (station_trend, station_detail) if "站内" in name else (google_trend, google_detail)
            evidence += "；" + detail
            if number is not None and currency == "USD":
                value = number
                if "Google" in name:
                    evidence += "；按选定口径将各词指数相加后判断；各词可能独立归一化，指数之和不等于实际搜索量。"
                ok = number > limit
        elif name == "季节波动系数" and season_ratio is not None and currency == "USD":
            value = season_ratio
            evidence += "；按未取整值判定；" + season_evidence
            ok = season_ratio <= limit
        elif name in ("旺季月份", "淡季月份") and season_ratio is not None and currency == "USD":
            value = "、".join(high if name == "旺季月份" else low) if season_ratio > settings[key("市场需求", "季节波动系数")]["limit"] else "无（未判定为季节性）"
            status = "仅展示"
            evidence += "；月份为搜索量近似，不是销量；" + season_evidence
        elif name in ("头部链接销售额占比", "头部品牌集中度", "卖家集中度"):
            labels = brand if name == "头部品牌集中度" else seller
            mask = mask_for(revenue) if name == "头部链接销售额占比" else mask_for(revenue, labels)
            evidence += "；" + coverage(mask)
            if name == "头部链接销售额占比":
                share = float(revenue[mask].nlargest(5).sum() / revenue[mask].sum() * 100) if mask.any() and revenue[mask].sum() > 0 else None
            elif mask.any() and revenue[mask].sum() > 0:
                groups = pd.DataFrame({"label": labels[mask].str.casefold(), "revenue": revenue[mask]}).groupby("label")["revenue"].sum()
                share = float(groups.nlargest(5).sum() / revenue[mask].sum() * 100)
            else:
                share = None
            if share is not None:
                value, ok = share, share < limit
        elif name == "新卖家占比":
            mask = mask_for(revenue, ages, seller)
            evidence += "；" + coverage(mask)
            if mask.any() and revenue[mask].sum() > 0:
                new = mask & (ages <= 180)
                cnt = int(seller[new].str.casefold().nunique())
                share = float(revenue[new].sum() / revenue[mask].sum() * 100)
                value = f"{cnt} 个卖家；新品销额占比 {format_text(f'{share:.12f}%')}"
                ok = cnt > limit or share > rule["limit2"]
            evidence += "；这是新品对应卖家，不能证明卖家本身为新卖家。"
        elif name == "大卖占比":
            mask = mask_for(revenue, counts)
            evidence += "；" + coverage(mask)
            if mask.any() and revenue[mask].sum() > 0:
                share = float(revenue[mask & (counts > 1000)].sum() / revenue[mask].sum() * 100)
                value, ok = share, share < limit
            evidence += "；评分数不等于文字评论数。"
        elif name == "VC卖家占比":
            mask = mask_for(revenue, shipping)
            evidence += "；" + coverage(mask)
            if mask.any() and revenue[mask].sum() > 0:
                share = float(revenue[mask & (shipping.str.upper() == "AMZ")].sum() / revenue[mask].sum() * 100)
                value, ok = share, share < limit
            evidence += "；AMZ并不等同已核实VC身份。"
        elif name == "本土卖家占比":
            mask = mask_for(revenue, location)
            evidence += "；" + coverage(mask)
            site_country = {"美国":"US", "US":"US", "英国":"GB", "德国":"DE", "法国":"FR", "意大利":"IT", "西班牙":"ES", "加拿大":"CA", "日本":"JP", "墨西哥":"MX", "巴西":"BR", "澳大利亚":"AU", "印度":"IN", "阿联酋":"AE", "荷兰":"NL", "瑞典":"SE", "新加坡":"SG"}.get(site)
            aliases = {"美国":"US", "USA":"US", "UNITED STATES":"US", "英国":"GB", "UK":"GB", "UNITED KINGDOM":"GB", "德国":"DE", "法国":"FR", "加拿大":"CA", "日本":"JP"}
            if site_country and mask.any() and revenue[mask].sum() > 0:
                countries = location[mask].str.upper().map(lambda x: aliases.get(x, x))
                share = float(revenue[mask][countries == site_country].sum() / revenue[mask].sum() * 100)
                value, ok = share, share < limit
                evidence += f"；当前站点国家 {site_country}；不根据AMZ配送方式推断卖家国别。"
            elif not site_country:
                evidence += "；未识别站点国家，无法判断本土。"
        elif name == "评论壁垒":
            mask = mask_for(parent, sales)
            evidence += "；" + coverage(mask)
            if mask.any():
                subset = pd.DataFrame({"parent": parent[mask].str.upper(), "sales": sales[mask], "count": counts[mask] if counts is not None else float("nan")})
                grouped = subset.groupby("parent", sort=True).agg({"sales": "sum", "count": "max"})
                top = grouped.sort_values(["sales"], ascending=False, kind="stable").head(10)
                valid_top = top["count"].dropna()
                evidence += f"；前{len(top)}个父体中，有效评分数 {len(valid_top)}/{len(top)}，缺失父体不计均值。"
            else:
                valid_top = pd.Series(dtype=float)
            if len(valid_top):
                mean = sum((Decimal(str(v)) for v in valid_top), Decimal(0)) / Decimal(len(valid_top))
                value = int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                ok = mean < Decimal(str(limit))
                evidence += "；显示值四舍五入为整数，按未取整均值判定；评分数不等同文字评论数。"
        elif name == "评分":
            valid_ratings = rating.where(rating > 0) if rating is not None else None
            mask = mask_for(valid_ratings)
            evidence += "；" + coverage(mask) + " 0不纳入评分均值。"
            if mask.any():
                mean = float(valid_ratings[mask].mean())
                value, ok = mean, mean >= limit
                evidence += "；按未取整均值判定。"
        elif name == "老品占比":
            mask = mask_for(revenue, ages)
            evidence += "；" + coverage(mask)
            if mask.any() and revenue[mask].sum() > 0:
                share = float(revenue[mask & (ages > 1095)].sum() / revenue[mask].sum() * 100)
                value, ok = share, share < limit
        if ok is None and status == "数据不足":
            required = {
                "平均客单价": [("价格", prices)],
                "月需求量估算": [("变体月销量", sales)],
                "类目GMV估算": [("变体月销额", revenue)],
                "头部链接销售额占比": [("变体月销额", revenue)],
                "头部品牌集中度": [("变体月销额", revenue), ("品牌", brand)],
                "卖家集中度": [("变体月销额", revenue), ("BuyBox卖家", seller)],
                "新卖家占比": [("变体月销额", revenue), ("上架天数", ages), ("BuyBox卖家", seller)],
                "大卖占比": [("变体月销额", revenue), ("评分数", counts)],
                "VC卖家占比": [("变体月销额", revenue), ("配送方式", shipping)],
                "本土卖家占比": [("变体月销额", revenue), ("配送方式", shipping), ("卖家所属地", location)],
                "评论壁垒": [("变体月销量", sales), ("父ASIN", parent), ("评分数", counts)],
                "评分": [("评分", rating)],
                "老品占比": [("变体月销额", revenue), ("上架天数", ages)],
            }.get(name, [])
            problems = [f"{label}有效 {0 if values is None else int(values.notna().sum())}/{len(frame)}"
                        for label, values in required if not _complete(values)]
            if required and not base_mask.all():
                problems.append("变体ASIN缺失或重复的行已排除")
            if problems:
                evidence += "；数据检查：" + "，".join(problems)
            if name in ("旺季月份", "淡季月份", "季节波动系数") and season_ratio is None:
                evidence += "；" + season_evidence
        if ok is not None:
            status = "达标" if ok else "不达标"
        if not rule.get("enabled", True):
            status = "未启用"
            evidence += "；已在工作台停用，不参与初判。"
        unit = {"核心词数量":"个", "平均客单价":currency, "月需求量估算":"件/变体/月", "类目GMV估算":f"{currency}/月" if currency else "",
                "趋势方向（站内）":"%", "趋势方向（Google）":"%",
                "季节波动系数":"倍", "头部链接销售额占比":"%", "头部品牌集中度":"%", "卖家集中度":"%",
                "大卖占比":"%", "VC卖家占比":"%", "本土卖家占比":"%", "评论壁垒":"条评分", "评分":"分", "老品占比":"%"}.get(name, "")
        rows.append(dict(zip(COLUMNS, ["", name, standard(template, rule), formula, value, status, evidence, section, unit])))
    return rows
