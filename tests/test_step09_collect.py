# 单元测试: 步骤9 搜索收集正确性(2026-09-08 增强)
# 1) 只解析搜索结果主列表结果卡(data-component-type="s-search-result"),
#    广告卡(sp-sponsored-result)与页尾"相关推荐/看了又看"等推荐模块一律不收;
# 2) 每条留标题快照(结果卡 h2 文本);
# 3) 标题×关键词规则审计(全内容词命中=相关, 缺失=待人工, 不自动剔除)。
import os

from steps.step09_search_collect import (
    _extract_result_items,
    _verify_relevance,
    _write_verify_csv,
)


def _organic(asin: str, title: str, comp_first: bool = False) -> str:
    attrs = f'data-component-type="s-search-result" data-asin="{asin}"' if comp_first \
        else f'data-asin="{asin}" data-component-type="s-search-result"'
    return (
        f'<div {attrs}>'
        f'<h2><a class="a-link-normal" href="/{asin}/dp/{asin}?ref=xx">'
        f'<span>{title}</span></a></h2>'
        f'<span class="a-size-base">4.5 stars</span>'
        f'</div>'
    )


def _sponsored(asin: str, title: str) -> str:
    return (
        f'<div data-component-type="sp-sponsored-result" data-asin="{asin}">'
        f'<h2><span>Sponsored ad {title}</span></h2>'
        f'</div>'
    )


def _related_module(asin: str) -> str:
    """页尾"与此商品相关"等推荐模块: 有 /dp/ 链接但不是 s-search-result 容器。"""
    return (
        f'<div data-component-type="p13n-sc-css-full-carousel">'
        f'<a href="/dp/{asin}?th=1">Related item {asin}</a>'
        f'</div>'
    )


def test_extract_organic_only_excludes_sponsored_and_related():
    """只收主列表有机结果卡: 广告卡(sp-sponsored-result)与推荐模块 /dp/ 链接排除。"""
    html = (
        _organic("B032DF7BEA", "Cat Water Fountain Stainless Steel 2.5L") +
        _sponsored("B03B9D2014", "Cat Fountain Pump") +
        _organic("B02A69D429", "Dog Water Fountain Pet Dispenser") +
        _related_module("B03259DB7A") +
        _related_module("B06FA26165")
    )
    items = _extract_result_items(html)
    asins = [it["asin"] for it in items]
    assert asins == ["B032DF7BEA", "B02A69D429"]          # 广告/推荐模块不收
    assert items[0]["title"] == "Cat Water Fountain Stainless Steel 2.5L"
    assert items[1]["title"] == "Dog Water Fountain Pet Dispenser"


def test_extract_attribute_order_variants():
    """data-asin 与 data-component-type 两种属性顺序都能解析。"""
    html = (
        _organic("B0BD918FED", "Title A", comp_first=False) +
        _organic("B01A14ECE7", "Title B", comp_first=True)
    )
    items = _extract_result_items(html)
    assert [it["asin"] for it in items] == ["B0BD918FED", "B01A14ECE7"]
    assert [it["title"] for it in items] == ["Title A", "Title B"]


def test_extract_title_fallback_dp_anchor():
    """无 h2 时(真实 DOM 结构漂移): 从卡内 /dp/ 标题锚点取文本; 主图锚点(内部空)跳过。"""
    html = (
        '<div data-component-type="s-search-result" data-asin="B02AA39EB4">'
        '<a href="/dp/B02AA39EB4"><img alt="img"></a>'
        '<a class="a-link-normal" href="/B02AA39EB4/dp/B02AA39EB4?ref=x">'
        '<span class="a-size-medium">Cat Water Fountain 2L Auto</span></a>'
        '<span class="a-size-base">4.6</span></div>'
    )
    items = _extract_result_items(html)
    assert items[0]["title"] == "Cat Water Fountain 2L Auto"


def test_extract_title_fallback_h2_aria_label():
    """h2 内无文本节点时: 用 h2 的 aria-label 作为标题。"""
    html = (
        '<div data-asin="B01A753246" data-component-type="s-search-result">'
        '<h2 aria-label="Pet Fountain with Pump 1.8L"><span class="a-text-normal"></span></h2>'
        '<span>other</span></div>'
    )
    items = _extract_result_items(html)
    assert items[0]["title"] == "Pet Fountain with Pump 1.8L"


def test_extract_asin_from_title_link_without_data_asin():
    """用户场景: 结果卡标签上无 data-asin, ASIN 只在标题链接 /dp/ 里 —— 也要收。"""
    html = (
        '<div data-component-type="s-search-result" class="s-card">'
        '<a href="/dp/B0FCBD752F"><img alt="img"></a>'
        '<h2><a href="/B0FCBD752F/dp/B0FCBD752F?ref=sr">'
        '<span>Cat Water Fountain Automatic 2.5L</span></a></h2>'
        '</div>'
        '<div data-component-type="s-search-result">'
        '<a href="/dp/B012F0FB40"><img alt="img"></a>'
        '<h2><a href="/B012F0FB40/dp/B012F0FB40"><span>Fountain Pump Kit</span></a></h2>'
        '</div>'
    )
    items = _extract_result_items(html)
    assert [it["asin"] for it in items] == ["B0FCBD752F", "B012F0FB40"]
    assert items[0]["title"] == "Cat Water Fountain Automatic 2.5L"


def test_exclude_adholder_cards():
    """新布局广告卡也带 s-search-result 容器: 类含 AdHolder 的卡必须排除。"""
    html = (
        _organic("B08CF79A42", "Cat Fountain Organic Result") +
        '<div data-component-type="s-search-result" class="sg-col AdHolder sg-col-4" '
        'data-asin="B0F2BDD8C6">'
        '<a href="/dp/B0F2BDD8C6"><h2><span>Sponsored Ad Product X</span></h2></a></div>'
    )
    items = _extract_result_items(html)
    assert [it["asin"] for it in items] == ["B08CF79A42"]


def test_exclude_sponsored_ads_link_card():
    """卡内出现 sponsored-ads.amazon.com 点击链接 = 广告, 排除。"""
    html = (
        '<div data-component-type="s-search-result" data-asin="B0870865B8">'
        '<a href="https://sponsored-ads.amazon.com/clk/?p=xyz">'
        '<span>Sponsored ad product</span></a></div>' +
        _organic("B019688DDB", "Real Product Title")
    )
    items = _extract_result_items(html)
    assert [it["asin"] for it in items] == ["B019688DDB"]


def test_script_json_noise_ignored_and_plain_sponsored_word_kept():
    """内嵌 JSON/脚本里的容器串不产生假卡; 商品标题正文含 Sponsored 字样不算广告。"""
    html = (
        '<script>{"data-component-type":"s-search-result","asin":"B0XXXXXX111"}</script>'
        '<style>.s-search-result{}</style>'
        '<!-- <div data-component-type="s-search-result" data-asin="B0XXXXXX222"> -->'
        + _organic("B0A0BA9C89", "Fountain Sponsored by Acme Brand")
    )
    items = _extract_result_items(html)
    assert [it["asin"] for it in items] == ["B0A0BA9C89"]
    assert items[0]["title"] == "Fountain Sponsored by Acme Brand"


def test_extract_empty_and_entities():
    """无结果卡返回空; 标题 html 实体还原 + 空白压缩。"""
    assert _extract_result_items('<div data-component-type="s-search-result"></div>') == []
    html = _organic("B026832417", "Cat &amp; Dog Fountain 2-in-1\n   Large")
    items = _extract_result_items(html)
    assert items[0]["title"] == "Cat & Dog Fountain 2-in-1 Large"


def test_verify_relevance_full_phrase_and_terms():
    """整词短语命中 / 全部内容词命中 -> 相关。"""
    assert _verify_relevance("cat water fountain",
                             "Cat Water Fountain Stainless Steel 2.5L")[0] == "相关"
    assert _verify_relevance("pet water dispenser", "Pet Water Dispenser 2L")[0] == "相关"


def test_verify_relevance_missing_term_flags_human():
    """缺任一内容词(复数/同义) -> 待人工(不自动剔除), 依据列出缺失词。"""
    verdict, reason = _verify_relevance("cat water fountain", "Cat Fountain Stainless")
    assert verdict.startswith("待人工")
    assert "water" in reason
    # 全虚词/过短关键词无法校验 -> 默认相关不误报
    assert _verify_relevance("for the pet", "Pet Fountain")[0] == "相关"


def test_verify_relevance_empty_title():
    """无标题快照(旧数据/回退解析) -> 单独判定。"""
    assert _verify_relevance("cat fountain", "")[0].startswith("无标题快照")


def test_write_verify_csv(tmp_path):
    """审计 CSV: 表头 + 每 词×ASIN 一行, utf-8-sig 可读。"""
    rows = [
        {"关键词": "cat fountain", "ASIN": "B032DF7BEA", "标题": "Cat Fountain",
         "判定": "相关", "依据": "标题含整词短语"},
        {"关键词": "cat fountain", "ASIN": "B03B9D2014", "标题": "",
         "判定": "无标题快照(旧数据/解析缺失)", "依据": "标题缺失, 人工确认"},
    ]
    path = os.path.join(tmp_path, "t-search-verify.csv")
    _write_verify_csv(path, rows)
    with open(path, encoding="utf-8-sig") as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 3                      # 表头 + 2 行
    assert lines[0].startswith("关键词,ASIN,标题,判定,依据")
    assert "B03B9D2014" in lines[2]
