# 回归测试(2026-09-10): step09 标题快照多级兜底 + 相关长尾的"配件/非主营"过滤。
#
# 背景:
#   ① Amazon 新搜索结果页部分卡片由 JS 客户端渲染, 标题可能不在 h2, 而在
#      a.a-link-normal / span.a-size-base-plus / span.a-color-base / img alt 等处,
#      原三级兜底(h2→/dp/锚点→h2 aria-label)会漏; 现拓宽到 9 级。
#   ② 长尾词会混入配件词(如 air ionizers 4 pack ...), 选"最相关长尾"时需按
#      FR-02 的 配件/耗材/硬排除 词 + 与核心产品共名词头 做剔除。
from steps.step09_search_collect import (
    _extract_title,
    _extract_result_items,
    _accessory_terms_from,
    _top_related_longtail,
    _contains_core_product,
    _core_product_heads,
)


# ---------------- ① 标题 9 级兜底 ----------------

def test_title_from_h2():
    assert _extract_title("<div><h2>H2 Title Here</h2></div>") == "H2 Title Here"


def test_title_from_dp_anchor_text():
    seg = '<div><a href="/dp/B000C91625">Anchor Title Here</a></div>'
    assert _extract_title(seg) == "Anchor Title Here"


def test_title_from_dp_anchor_aria_label():
    seg = '<div><a href="/dp/B000C91625?th=1" aria-label="Anchor Aria Title"></a></div>'
    assert _extract_title(seg) == "Anchor Aria Title"


def test_title_from_h2_aria_label():
    seg = '<div><h2 aria-label="H2 Aria Title" class="x"></h2></div>'
    assert _extract_title(seg) == "H2 Aria Title"


def test_title_from_link_normal():
    seg = '<div><a class="a-link-normal s-line-clamp-2" href="/x">Link Normal Title</a></div>'
    assert _extract_title(seg) == "Link Normal Title"


def test_title_from_size_base_plus_span():
    seg = ('<div><a class="a-link-normal" href="/dp/B04A8EAA04"></a>'
           '<span class="a-size-base-plus a-color-base a-text-normal">Span Base Plus Title</span></div>')
    assert _extract_title(seg) == "Span Base Plus Title"


def test_title_from_color_base_span():
    seg = '<div><span class="a-color-base">Color Base Only Title</span></div>'
    assert _extract_title(seg) == "Color Base Only Title"


def test_title_from_text_normal_span():
    seg = '<div><span class="custom a-text-normal">Text Normal Only Title</span></div>'
    assert _extract_title(seg) == "Text Normal Only Title"


def test_title_from_img_alt():
    seg = '<div><img alt="Image Alt Title" src="//x.jpg"></div>'
    assert _extract_title(seg) == "Image Alt Title"


def test_title_empty_when_nothing():
    assert _extract_title("<div><p>no title anywhere</p></div>") == ""


def test_old_logic_would_miss_span_only_card():
    """证明拓宽有效: span-only 卡片的标题不在 h2 / 不在 /dp/ 锚点文本里。"""
    seg = ('<div><a class="a-link-normal" href="/dp/B04A8EAA04"></a>'
           '<span class="a-size-base-plus a-color-base a-text-normal">Only In Span Title</span></div>')
    assert "<h2" not in seg
    assert _extract_title(seg) == "Only In Span Title"


# ---------------- ② 结果卡解析(含广告排除 + 标题快照) ----------------

_PAGE = """
<div data-component-type="s-search-result" data-asin="B000C91625" class="s-result-item">
  <h2>Organic One Title</h2>
  <a href="/dp/B000C91625">Organic One Title</a>
</div>
<div data-component-type="s-search-result" data-asin="B04A8EAA04" class="s-result-item">
  <a class="a-link-normal" href="/dp/B04A8EAA04"></a>
  <span class="a-size-base-plus a-color-base a-text-normal">Organic Two Span Title</span>
</div>
<div data-component-type="s-search-result" data-asin="B08B0176A6" class="s-result-item AdHolder">
  <h2>Sponsored Thing Title</h2>
  <a href="/dp/B08B0176A6">Sponsored Thing Title</a>
</div>
"""


def test_result_items_capture_all_organic_and_drop_ads():
    items = _extract_result_items(_PAGE)
    asins = {it["asin"] for it in items}
    assert "B08B0176A6" not in asins, "带 AdHolder 的广告卡必须排除"
    assert {"B000C91625", "B04A8EAA04"} <= asins, f"有机结果漏收: {asins}"
    for it in items:
        assert it["title"], f"{it['asin']} 标题快照缺失(应被兜底取到)"


# ---------------- ③ 相关长尾: 配件/非主营过滤 ----------------

def test_accessory_terms_from_prompt():
    terms = _accessory_terms_from({
        "accessories": ["Replacement Filter", "ionizer plate"],
        "consumables": ["Filter Refill"],
        "hard_exclude": ["car air purifier"],
    })
    assert {"replacement filter", "ionizer plate", "filter refill", "car air purifier"} <= terms


def test_core_product_head_matching():
    heads = _core_product_heads(["air purifier", "air purifiers for home"])
    assert _contains_core_product("air purifier for bedroom", heads)
    assert not _contains_core_product("air ionizers 4 pack for bedroom", heads)
    assert not _contains_core_product("ionizers for home", heads)


def test_top_related_longtail_drops_accessory_and_ranks_core_head_first():
    """2026-09-13(③): "含核心产品名词头"由硬门槛改为**排序权重** ——
    硬门槛实战会把长尾全剔(Food Processors 根词与达标词无共同词头 → 相关长尾 0),
    改为权重后: ①配件/耗材词仍硬剔除; ②不含词头的候选排在后面但不再消失。"""
    prompt = {"accessories": ["replacement filter"], "consumables": [], "hard_exclude": []}
    terms = _accessory_terms_from(prompt)
    core = ["air purifier", "air purifiers for home"]
    longs = [
        "air purifier for kitchen",
        "air purifier for bedroom",
        "air ionizers 4 pack for bedroom",
        "ionizers for home",
        "air purifier replacement filter",   # 命中配件词 → 硬剔除
        "air purifier for pets",
    ]
    picked = _top_related_longtail(core, longs, 5, terms)
    assert "air purifier replacement filter" not in picked, "命中配件词必须剔除"
    # 含词头的 3 个排在最前(相关度相同 -> 保持原序)
    assert picked[:3] == ["air purifier for kitchen", "air purifier for bedroom",
                          "air purifier for pets"]
    # 不含词头的 2 个不再被硬剔除, 只沉到末尾
    assert picked[3:] == ["air ionizers 4 pack for bedroom", "ionizers for home"]


def test_top_related_longtail_warns_when_nobody_has_core_head():
    """一条都不含核心产品名词头时打 WARN(提示 FR-04 根词可能与达标词脱节), 但仍返回结果。"""
    logs: list[str] = []
    core = ["air purifier", "air purifiers for home"]
    longs = ["ionizers for home", "ionizer 4 pack"]
    picked = _top_related_longtail(core, longs, 5, set(), log=logs.append)
    assert picked == ["ionizers for home", "ionizer 4 pack"]     # 不再返回空
    assert logs and "产品名词头" in logs[0]


def test_top_related_longtail_no_warn_when_head_hit_exists():
    logs: list[str] = []
    core = ["air purifier"]
    _top_related_longtail(core, ["air purifier for kitchen"], 5, set(), log=logs.append)
    assert logs == []
