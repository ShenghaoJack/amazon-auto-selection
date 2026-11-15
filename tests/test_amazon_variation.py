# 单元测试: core/amazon_variation 解析与分组审计(2026-09-08 对齐 codex skill)
from core.amazon_variation import (
    attempts_for,
    auto_resolve_cross_parent,
    build_groups_skill,
    choose_parent,
    normalize_asin,
    parse_variations,
    unique_in_order,
    worker_count,
)


def _html(parent="B076215605", current="B0EFD58058", extra_color=None,
          title="Cat Water Fountain - Amazon.com", body="page body" * 4000) -> str:
    dim = '{"Black":"B03C537AFE","Silver":"B092B9EB32"}'
    color = extra_color or '{"Red":"B00756426F","Black":"B03C537AFE","Silver":"B092B9EB32"}'
    parts = []
    if parent:
        parts.append(f'"parentAsin":"{parent}"')
    parts.append(f'"currentAsin":"{current}"')
    parts.append(f'"dimensionToAsinMap":{dim},"num_total_variations":2')
    data = "{" + ",".join(parts) + "}"
    return (
        "<html><head><title>" + title + "</title></head><body>" +
        f'<script>P.register("twister-js-init-dpx-data", function(){{ var dataToReturn = '
        f'{data}; }});</script>' +
        f'<script>jQuery.parseJSON(\'{{"colorToAsin":{color}}}\');</script>' +
        body + "</body></html>"
    )


def test_normalize_and_unique():
    assert normalize_asin(" b0abc12345 ") == "B04D5C8E59"
    assert normalize_asin("not-an-asin") is None
    assert unique_in_order(["A", "B", "A", "C"]) == ["A", "B", "C"]


def test_parse_variants_max_union_and_extended():
    """受限字段取最大并集: dimensionToAsinMap(2) + colorToAsin 额外 1 个 + 输入兜底;
    超出的标记为扩展变体并记来源。"""
    html = _html(extra_color='{"Red":"B00756426F","Black":"B03C537AFE","Silver":"B092B9EB32"}')
    p = parse_variations(html, "B0EFD58058")
    assert p["status"] == "ok"
    assert p["parent_asin"] == "B076215605"
    # dimensionToAsinMap 2 个 + colorToAsin 多出的 Red + currentAsin 兜底 B0EFD58058
    assert "B03C537AFE" in p["variant_asins"]
    assert "B092B9EB32" in p["variant_asins"]
    assert "B00756426F" in p["variant_asins"]
    assert "B0EFD58058" in p["variant_asins"]
    assert p["variant_asins"] == unique_in_order(p["variant_asins"])
    assert p["map_counts"]["dimensionToAsinMap"] == 2
    assert p["map_counts"]["colorToAsin"] == 3
    assert p["expected_variation_count"] == 2
    # 超出主映射的变体被标为扩展变体, 来源=colorToAsin/selectedAsinFallback/requestedAsinFallback
    assert "B00756426F" in p["extended_variant_asins"]
    assert "colorToAsin" in p["extended_variant_sources"]["B00756426F"]
    assert p["parent_method"] in ("linked_landing_asin", "ranked_page_parent_candidate")


def test_parse_parent_fallback_and_page_not_found():
    """无父体证据 -> fallback_input_asin; Page Not Found 标题 -> page_not_found。"""
    html = _html(parent=None, title="Pet Supplies - Amazon.com", body="x" * 60000)
    p = parse_variations(html, "B0EFD58058")
    assert p["parent_method"] == "fallback_input_asin"
    assert p["parent_asin"] == "B0EFD58058"
    pnf = parse_variations(_html(parent=None, title="Page Not Found", body="x" * 60000),
                           "B0EFD58058")
    assert pnf["status"] == "page_not_found"


def test_non_us_site_page_not_misjudged_as_unexpected():
    """非 US 站详情页标题是 `Amazon.co.uk`/`Amazon.de`, 不能被误判成 unexpected_page。

    2026-09-15 修复: 原实现写死 `"amazon.com" not in title` —— 英国/德国/法国站的 title
    永远不含 amazon.com, 于是页面里只要没直接出现该 ASIN 字符串就会被丢掉(变体抓取受损)。
    """
    # 英国站: 标题 Amazon.co.uk + 页面里没有目标 ASIN 字面量 → 必须仍是 ok
    uk = ('<html><head><title>Chopping Board Set - Amazon.co.uk</title></head><body>'
          + ("filler body " * 4000) + "</body></html>")
    p = parse_variations(uk, "B0EFD58058", market="amazon.co.uk")
    assert p["status"] == "ok", "英国站页面不得被判 unexpected_page"

    # 德国站同理
    de = ('<html><head><title>Schneidebrett - Amazon.de</title></head><body>'
          + ("inhalt " * 4000) + "</body></html>")
    assert parse_variations(de, "B0EFD58058", market="amazon.de")["status"] == "ok"

    # 美国站老行为不变(不传 market 时按 amazon.com 判断)
    assert parse_variations(uk, "B0EFD58058")["status"] == "unexpected_page"
    us = ('<html><head><title>Board - Amazon.com</title></head><body>'
          + ("body " * 4000) + "</body></html>")
    assert parse_variations(us, "B0EFD58058")["status"] == "ok"


def test_choose_parent_scores_linked_over_input():
    """父体证据评分: linked parentAsin/landingAsin +100 胜过输入 ASIN 当父(-20)。"""
    text = '{"parentAsin":"B076215605","landingAsin":"B0EFD58058"} ' \
           '{"dimensionToAsinMap":{"Black":"B03C537AFE","Silver":"B092B9EB32"}}'
    variants = ["B03C537AFE", "B092B9EB32"]
    parent, method, ranked = choose_parent(text, "B0EFD58058", variants)
    assert parent == "B076215605"
    assert ranked[0][1] >= 100


def _rec(asin, parent, variants, extra=None):
    rec = {"request_asin": asin, "status": "ok", "ok": True,
           "parent_asin": parent, "variant_asins": variants,
           "extended_variant_asins": [], "extended_variant_sources": {},
           "source_url": f"https://www.amazon.com/dp/{asin}"}
    if extra:
        rec.update(extra)
    return rec


def test_auto_resolve_cross_parent_own_record_wins():
    """规则①: 变体有自身记录时, 归到自身页面父体所在组, 从其它组移除。"""
    groups = build_groups_skill(
        ["B0EFD58058", "B0A5A928ED", "B03C537AFE"],
        [
            _rec("B0EFD58058", "B076215605", ["B03C537AFE", "B092B9EB32", "B0EFD58058"]),
            _rec("B0A5A928ED", "B08BBC7F8C", ["B03C537AFE", "B0A5A928ED"]),
            _rec("B03C537AFE", "B076215605", ["B03C537AFE"]),   # 自身记录: 父=P01
        ],
    )
    notes = auto_resolve_cross_parent(groups, [
        _rec("B0EFD58058", "B076215605", ["B03C537AFE", "B092B9EB32", "B0EFD58058"]),
        _rec("B0A5A928ED", "B08BBC7F8C", ["B03C537AFE", "B0A5A928ED"]),
        _rec("B03C537AFE", "B076215605", ["B03C537AFE"]),
    ])
    assert notes, "应有自动归属记录"
    g1 = next(g for g in groups if g["parent_asin"] == "B076215605")
    g2 = next(g for g in groups if g["parent_asin"] == "B08BBC7F8C")
    assert "B03C537AFE" in g1["variant_asins"]
    assert "B03C537AFE" not in g2["variant_asins"]
    assert any("自动归属父 B076215605" in n and "从父 B08BBC7F8C 移除" in n for n in notes)


def test_auto_resolve_cross_parent_ref_count_wins():
    """规则③: 无自身记录时, 归到引用次数最多的父组。"""
    groups = build_groups_skill(
        ["B0EFD58058", "B078B0AE44", "B0A5A928ED"],
        [
            _rec("B0EFD58058", "B076215605", ["B068631542", "B0EFD58058"]),
            _rec("B078B0AE44", "B076215605", ["B068631542", "B078B0AE44"]),  # P01 引用 2 次
            _rec("B0A5A928ED", "B08BBC7F8C", ["B068631542", "B0A5A928ED"]),  # P02 引用 1 次
        ],
    )
    records = [
        _rec("B0EFD58058", "B076215605", ["B068631542", "B0EFD58058"]),
        _rec("B078B0AE44", "B076215605", ["B068631542", "B078B0AE44"]),
        _rec("B0A5A928ED", "B08BBC7F8C", ["B068631542", "B0A5A928ED"]),
    ]
    auto_resolve_cross_parent(groups, records)
    g1 = next(g for g in groups if g["parent_asin"] == "B076215605")
    g2 = next(g for g in groups if g["parent_asin"] == "B08BBC7F8C")
    assert "B068631542" in g1["variant_asins"]
    assert "B068631542" not in g2["variant_asins"]


def test_worker_count_schedule():
    assert worker_count(200, 8) == 8
    assert worker_count(50, 8) == 6
    assert worker_count(20, 8) == 3
    assert worker_count(5, 8) == 2
    assert worker_count(200, 2) == 2      # 封顶


def test_attempts_for_dynamic_and_override():
    """动态(对齐 skill): >30→2, ≤30→4; FETCH_ASIN_TRIES 显式覆盖为固定值。"""
    assert attempts_for(103) == 2          # >30 -> 2 次
    assert attempts_for(30) == 4           # ≤30 -> 4 次
    assert attempts_for(5) == 4
    # 显式配置覆盖(yaml 可为 int/str; None/0/负/非数字回落动态)
    assert attempts_for(103, 1) == 1
    assert attempts_for(103, "1") == 1
    assert attempts_for(103, 1.0) == 1
    assert attempts_for(103, None) == 2
    assert attempts_for(103, 0) == 2
    assert attempts_for(103, -1) == 2
    assert attempts_for(103, "abc") == 2
    assert attempts_for(5, 1) == 1


def test_build_groups_merge_remove_parent_and_cross():
    """按父聚合: 多变体组删父体; 同一变体出现在两个父组 -> 跨父重复。"""
    recs = [
        {"request_asin": "B0EFD58058", "status": "ok", "ok": True,
         "parent_asin": "B076215605", "variant_asins": ["B076215605", "B03C537AFE",
                                                         "B092B9EB32", "B0EFD58058"],
         "extended_variant_asins": ["B092B9EB32"],
         "extended_variant_sources": {"B092B9EB32": ["colorToAsin"]},
         "source_url": "https://www.amazon.com/dp/B0EFD58058"},
        {"request_asin": "B0A5A928ED", "status": "ok", "ok": True,
         "parent_asin": "B08BBC7F8C", "variant_asins": ["B03C537AFE", "B0A5A928ED"],
         "extended_variant_asins": [], "extended_variant_sources": {},
         "source_url": "https://www.amazon.com/dp/B0A5A928ED"},
    ]
    groups = build_groups_skill(["B0EFD58058", "B0A5A928ED"], recs)
    assert len(groups) == 2
    g1 = next(g for g in groups if g["parent_asin"] == "B076215605")
    assert "B076215605" not in g1["variant_asins"]      # 多变体组删父体
    assert g1["variant_count"] == 3
    # 同一变体 B03C537AFE 出现在两个父组 -> 跨父重复(在 build_outputs 层报出)
    parents_of_black = [g["parent_asin"] for g in groups if "B03C537AFE" in g["variant_asins"]]
    assert len(parents_of_black) == 2
