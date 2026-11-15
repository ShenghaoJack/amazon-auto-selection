"""Amazon 变体页解析与审计(2026-09-08, 对齐 codex skill scrape-amazon-variations)。

纯逻辑、无网络/无浏览器。供 steps/step10_variants.py 直接请求后调用:

- `parse_variations(html, asin)` → 从受限字段取**最大变体并集**并返回 skill 口径字段
  (parent/method/candidates、map_counts/map_asins、fallback、extended+来源、
  expected_variation_count、status 判定、html_length), 供审计与 Excel。
- `choose_parent(...)`: 页面父体证据评分(linked +100 / parentAsin 每次 +10 /
  parent_asin 每次 +8 / URL parentAsin= 每次 +3 / 不在变体集 +5 /
  输入被当父体但有多个变体 -20); 无证据回退输入 ASIN 并标 fallback_input_asin。
- `build_groups_skill(asins, records)` → 按父 ASIN 聚合(删多变体组里的父体、
  扩展变体标注、声明数 vs 提取数检查、跨父重复、需复核标记)。
- `url_candidates(market, asin)`: 与 skill 一致的四种商品页形态。

对齐口径说明:
- 保留最大并集(不因页面声明数删变体), 超出的标"扩展变体"并记来源;
- 404/Page Not Found 跳过为终态(skipped_page_not_found), 不再重试;
- blocked(Robot/CAPTCHA)即使 HTTP200 也判无效, 可重试;
- 父 ASIN 格式/变体数(1..100)/HTML 长度(>=50000) 无效即拒收。
"""
from __future__ import annotations

import html as _html
import json
import re
from collections import Counter, defaultdict

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

_ALLOWED_MAP_LABELS = [
    ("dimensionToAsinMap", False),   # 主变体映射(页面声明/主图维度)
    ("colorToAsin", False),          # 颜色维度
    ("asinVariationValues", True),   # 变体维度值(键含 ASIN)
    ("variationAsins", True),        # 变体 ASIN 列表
]


def normalize_asin(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text if ASIN_PATTERN.fullmatch(text) else None


def unique_in_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def extract_balanced_json_objects(text: str, label: str) -> list[object]:
    """取命名属性后最近的平衡 JSON 对象(含单/双引号字符串内的大括号处理)。"""
    results: list[object] = []
    label_re = re.compile(rf"[\"']{re.escape(label)}[\"']\s*:\s*", re.I)
    for match in label_re.finditer(text):
        start = text.find("{", match.end())
        if start < 0 or start - match.end() > 20:
            continue
        depth = 0
        in_string = False
        escape = False
        quote = ""
        end = None
        for index in range(start, min(len(text), start + 400_000)):
            ch = text[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            continue
        raw = _html.unescape(text[start:end])
        try:
            results.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return results


def collect_asins(value: object, *, include_dict_keys: bool = False) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        asin = normalize_asin(value)
        if asin:
            found.append(asin)
    elif isinstance(value, list):
        for item in value:
            found.extend(collect_asins(item, include_dict_keys=include_dict_keys))
    elif isinstance(value, dict):
        for key, item in value.items():
            if include_dict_keys:
                asin = normalize_asin(str(key))
                if asin:
                    found.append(asin)
            found.extend(collect_asins(item, include_dict_keys=include_dict_keys))
    return found


def choose_parent(text: str, asin: str, variants: list[str]) -> tuple[str, str, list[tuple[str, int]]]:
    """页面父体证据评分。返回 (父ASIN, 识别方式, 候选前8[(asin,得分)])。"""
    candidates: list[str] = []
    scored: defaultdict[str, int] = defaultdict(int)

    linked_patterns = [
        re.compile(rf"parentAsin=([A-Z0-9]{{10}})[^\"'<>]{{0,600}}landingAsin={re.escape(asin)}",
                   re.I | re.S),
        re.compile(rf"landingAsin={re.escape(asin)}[^\"'<>]{{0,600}}parentAsin=([A-Z0-9]{{10}})",
                   re.I | re.S),
        re.compile(rf"[\"']parentAsin[\"']\s*:\s*[\"']([A-Z0-9]{{10}})[\"'][\s\S]{{0,800}}?"
                   rf"[\"'](?:landingAsin|asin)[\"']\s*:\s*[\"']{re.escape(asin)}[\"']", re.I),
    ]
    for pattern in linked_patterns:
        for match in pattern.finditer(text):
            candidate = match.group(1).upper()
            candidates.append(candidate)
            scored[candidate] += 100

    generic_patterns = [
        (10, re.compile(r"[\"']parentAsin[\"']\s*:\s*[\"']([A-Z0-9]{10})[\"']", re.I)),
        (3, re.compile(r"parentAsin=([A-Z0-9]{10})", re.I)),
        (8, re.compile(r"[\"']parent_asin[\"']\s*:\s*[\"']([A-Z0-9]{10})[\"']", re.I)),
    ]
    for weight, pattern in generic_patterns:
        hits = [m.group(1).upper() for m in pattern.finditer(text)]
        counts = Counter(hits)
        for candidate, count in counts.items():
            candidates.extend([candidate] * count)
            scored[candidate] += weight * count

    variant_set = set(variants)
    for candidate in list(scored):
        if candidate not in variant_set:
            scored[candidate] += 5
        if candidate == asin and len(variant_set) > 1:
            scored[candidate] -= 20

    if not scored:
        return asin, "fallback_input_asin", []

    first_position = {value: candidates.index(value) for value in set(candidates)}
    ranked = sorted(scored.items(),
                    key=lambda item: (-item[1], first_position.get(item[0], 10**9), item[0]))
    parent = ranked[0][0]
    method = "linked_landing_asin" if ranked[0][1] >= 100 else "ranked_page_parent_candidate"
    return parent, method, ranked[:8]


def parse_variations(html: str, asin: str, market: str = "") -> dict:
    """解析单个商品页(对齐 skill parse_page/amazon_html)。

    market: 期望的亚马逊域名(如 `amazon.co.uk`), 用于判断"抓回来的到底是不是本站商品页"。
    ⚠️ 2026-09-15 修复: 原来这里把**美国站**写死了 ——
        `elif not blocked and ("amazon.com" not in title.lower() and asin not in decoded)`
    而英国/德国/法国站详情页的 `<title>` 是 `…: Amazon.co.uk` / `Amazon.de`, **永远不含
    `amazon.com`** → 只要页面里恰好没直接出现该 ASIN 字符串, 就会被误判 `unexpected_page`
    丢掉(非 US 站变体抓取受影响)。现改为按**传入的站点域名**判断(未传时退回 `amazon.com`,
    保持 US 站与老调用行为不变)。
    """
    decoded = _html.unescape(html or "")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    _domain = str(market or "amazon.com").strip().lower().lstrip(".")

    variants: list[str] = []
    map_counts: dict[str, int] = {}
    map_asins: dict[str, list[str]] = {}
    for label, include_keys in _ALLOWED_MAP_LABELS:
        values: list[str] = []
        for obj in extract_balanced_json_objects(decoded, label):
            values.extend(collect_asins(obj, include_dict_keys=include_keys))
        values = unique_in_order(values)
        map_counts[label] = len(values)
        map_asins[label] = values
        variants.extend(values)

    fallback_variant_asins: list[str] = []
    for pattern in [
        r"[\"']deletedLandingAsinInfo[\"']\s*:\s*\{[^{}]{0,1000}?[\"']asin[\"']\s*:\s*"
        r"[\"']([A-Z0-9]{10})[\"']",
        r"[\"']currentAsin[\"']\s*:\s*[\"']([A-Z0-9]{10})[\"']",
    ]:
        fallback_variant_asins.extend(
            m.group(1).upper() for m in re.finditer(pattern, decoded, re.I | re.S)
        )
    fallback_variant_asins = unique_in_order(fallback_variant_asins)
    variants.extend(fallback_variant_asins)
    variants.append(asin)
    variants = unique_in_order(variants)

    expected_counts = [int(v) for v in re.findall(
        r"[\"']num_total_variations[\"']\s*:\s*(\d+)", decoded, re.I)]
    expected = max(expected_counts) if expected_counts else None

    # 扩展变体标注: 保留最大并集, 但标注超出主变体映射(dimensionToAsinMap)的来源
    primary_variants = map_asins.get("dimensionToAsinMap", [])
    extended_variant_asins: list[str] = []
    extended_variant_sources: dict[str, list[str]] = {}
    if primary_variants and len(variants) > len(primary_variants):
        for variant in variants:
            if variant in primary_variants:
                continue
            sources = [
                label for label, values in map_asins.items()
                if label != "dimensionToAsinMap" and variant in values
            ]
            if variant in fallback_variant_asins:
                sources.append("selectedAsinFallback")
            if variant == asin and not sources:
                sources.append("requestedAsinFallback")
            if sources:
                extended_variant_asins.append(variant)
                extended_variant_sources[variant] = unique_in_order(sources)

    parent, parent_method, parent_candidates = choose_parent(decoded, asin, variants)

    lowered = decoded.lower()
    blocked = any(
        marker in lowered for marker in (
            "robot check", "validatecaptcha", "enter the characters you see below",
            "api-services-support@amazon.com",
        )
    )
    status = "blocked" if blocked else "ok"
    if not blocked and "page not found" in title.lower():
        status = "page_not_found"
    elif not blocked and (_domain not in title.lower() and asin not in decoded):
        status = "unexpected_page"

    return {
        "status": status,
        "title": title,
        "parent_asin": parent,
        "parent_method": parent_method,
        "parent_candidates": parent_candidates,
        "variant_asins": variants,
        "variant_count": len(variants),
        "expected_variation_count": expected,
        "map_counts": map_counts,
        "map_asins": map_asins,
        "fallback_variant_asins": fallback_variant_asins,
        "extended_variant_asins": extended_variant_asins,
        "extended_variant_sources": extended_variant_sources,
        "html_length": len(html or ""),
    }


def url_candidates(market: str, asin: str) -> list[str]:
    """与 skill 一致的商品页形态(依次尝试)。"""
    canonical = f"https://{market}/dp/{asin}"
    return [
        canonical,
        f"https://{market}/gp/product/{asin}?th=1&psc=1",
        f"https://{market}/dp/{asin}?th=1&psc=1",
        f"https://{market}/dp/{asin}?language=en_US",
    ]


def worker_count(remaining: int, maximum: int) -> int:
    """按剩余量动态并发(对齐 skill): >100→8 / >30→6 / >10→3 / 其余→2, 受 maximum 封顶。"""
    if remaining > 100:
        return max(1, min(maximum, 8))
    if remaining > 30:
        return max(1, min(maximum, 6))
    if remaining > 10:
        return max(1, min(maximum, 3))
    return max(1, min(maximum, 2))


def attempts_for(remaining: int, configured: int | None = None) -> int:
    """单 ASIN 尝试次数: 显式配置 `FETCH_ASIN_TRIES`(>=1)时固定按配置,
    否则对齐 skill 动态: 剩 >30 → 2 次, 否则 4 次。

    configured: 配置原值(可为 int/str/None); 非法(缺失/0/负/非数字)回落动态。
    """
    try:
        if configured is not None and int(configured) >= 1:
            return int(configured)
    except (TypeError, ValueError):
        pass
    return 2 if remaining > 30 else 4


def auto_resolve_cross_parent(groups: list[dict], records: list[dict]) -> list[str]:
    """自动消解"跨父重复变体"(2026-09-08): 同一变体出现在多个父组时, 用确定性
    规则选唯一父组并把它从其它组移除, 返回说明日志行。

    规则优先级(全部确定性, 可复现):
      ① 该变体**自身 ok 记录**的 parent_asin(页面 linked/评分证据)所在组;
      ② 该变体作为"输入 ASIN"被直接抓过的组(最可靠的归属来源);
      ③ 该变体被引用(出现在组内成员页 variant_asins)次数最多的组;
      ④ 仍未决 -> 输入顺序最靠前的组。
    从其它组移除时只动该组的 variant_asins/计数, 并记注释, 不编造任何 ASIN。
    """
    notes: list[str] = []
    # 变体 -> 出现它的父组列表(按 groups 顺序)
    appear: dict[str, list[int]] = {}
    for gi, group in enumerate(groups):
        for v in group["variant_asins"]:
            appear.setdefault(v, []).append(gi)

    own_parent = {
        str(r.get("request_asin") or "").upper(): str(r.get("parent_asin") or "").upper()
        for r in records if r.get("ok")
    }
    input_order = {str(g["parent_asin"]).upper(): i for i, g in enumerate(groups)}

    def ref_count(variant: str, gi: int) -> int:
        parent = str(groups[gi]["parent_asin"]).upper()
        return sum(
            1 for r in records
            if r.get("ok") and str(r.get("parent_asin") or "").upper() == parent
            and variant in [str(x).upper() for x in (r.get("variant_asins") or [])]
        )

    for variant, gis in appear.items():
        if len(gis) <= 1:
            continue
        primary: int | None = None
        reason = ""
        # ① 自身记录父体
        op = own_parent.get(variant)
        if op:
            for gi in gis:
                if str(groups[gi]["parent_asin"]).upper() == op:
                    primary = gi
                    reason = f"该变体自身页面父体={op}"
                    break
        # ② 作为输入被抓过的组(该组 input_asins 含 variant)
        if primary is None:
            for gi in gis:
                if variant in [str(a).upper() for a in groups[gi]["input_asins"]]:
                    primary = gi
                    reason = "该变体作为输入被抓过"
                    break
        # ③ 引用次数最多
        if primary is None:
            primary = max(gis, key=lambda gi: (ref_count(variant, gi), -gi))
            reason = "被组内成员引用最多"
        # ④ 输入顺序最靠前(上面 max 已隐含 tie -> 最小索引)
        keep = gis[0] if primary is None else primary
        for gi in gis:
            if gi == keep:
                continue
            group = groups[gi]
            if variant in group["variant_asins"]:
                group["variant_asins"].remove(variant)
                group["variant_count"] = len(group["variant_asins"])
                group["extended_variant_asins"] = [
                    v for v in group["extended_variant_asins"] if v != variant
                ]
                group["extended_variant_sources"].pop(variant, None)
                note = (f"跨父重复变体 {variant} 自动归属父 {groups[keep]['parent_asin']}"
                        f"({reason}), 已从父 {group['parent_asin']} 移除")
                group.setdefault("_auto_notes", []).append(note)
                notes.append(note)
    return notes


def build_groups_skill(asins: list[str], records: list[dict]) -> list[dict]:
    """按父 ASIN 聚合 ok 记录(对齐 skill build_groups/amazon_html)。

    规则: 合并输入变体并集(按来源顺序去重, 输入 ASIN 兜底补入); 多变体组里
    父体混在子列表则删除父体; 扩展变体标注与来源; 声明数检查; 需复核标记。
    """
    order = {asin: index for index, asin in enumerate(asins)}
    grouped: dict[str, dict] = {}
    for record in records:
        if record.get("status") != "ok":
            continue
        parent = str(record.get("parent_asin", "")).upper()
        group = grouped.setdefault(parent, {
            "parent_asin": parent, "input_asins": [], "variant_asins": [],
            "source_urls": [], "statuses": [], "expected_variation_count": 0,
            "extended_variant_asins": [], "extended_variant_sources": {},
        })
        group["input_asins"].append(str(record.get("request_asin", "")).upper())
        group["variant_asins"].extend(
            str(v).upper() for v in record.get("variant_asins", []))
        group["variant_asins"].append(str(record.get("request_asin", "")).upper())
        group["extended_variant_asins"].extend(record.get("extended_variant_asins", []))
        for variant, sources in (record.get("extended_variant_sources") or {}).items():
            group["extended_variant_sources"].setdefault(str(variant).upper(), []).extend(sources)
        src = str(record.get("source_url") or record.get("final_url") or "")
        if src:
            group["source_urls"].append(src)
        group["statuses"].append(str(record.get("status", "")))
        exp = record.get("expected_variation_count")
        if exp:
            group["expected_variation_count"] = max(group["expected_variation_count"], int(exp))

    result: list[dict] = []
    for group in grouped.values():
        group["input_asins"] = unique_in_order(group["input_asins"])
        variants = unique_in_order(group["variant_asins"])
        # 多变体组里父体混在子列表 → 删除(仅真正单品页允许父体=唯一变体)
        if len(variants) > 1 and group["parent_asin"] in variants:
            variants.remove(group["parent_asin"])
        group["variant_asins"] = variants
        group["extended_variant_asins"] = unique_in_order(
            v for v in group["extended_variant_asins"] if v in variants)
        group["extended_variant_sources"] = {
            v: unique_in_order(s)
            for v, s in group["extended_variant_sources"].items()
            if v in group["extended_variant_asins"]
        }
        group["source_urls"] = unique_in_order(group["source_urls"])
        group["statuses"] = unique_in_order(group["statuses"])
        group["input_count"] = len(group["input_asins"])
        group["variant_count"] = len(variants)
        expected = group["expected_variation_count"] or None
        group["expected_variation_count"] = expected
        group["variation_count_check"] = (
            "complete_or_exceeds_page_count"
            if expected is not None and len(variants) >= expected
            else "no_page_count" if expected is None else "below_page_count"
        )
        group["needs_review"] = (
            any(s != "ok" for s in group["statuses"])
            or group["variation_count_check"] == "below_page_count"
        )
        result.append(group)
    result.sort(key=lambda g: min(order.get(a, 10**9) for a in g["input_asins"]))
    return result
