# -*- coding: utf-8 -*-
"""沉淀的「类目级共享层 + 市场标记」（方案甲第 1 步，2026-10-09 用户口径）。

要解决什么
===============================================================================
`CAT` 名里带了市场+年月（`Cutting Boards-US-202608`）→ 同一商品类目的
`Chopping Boards-UK-202608` 拿不到任何沉淀 → **跨市场/跨月全从零开始**。
用户口径：**共享沉淀，但每条标上来源市场** —— 既不重复劳动，又不互相污染。

第 1 步的两条铁律（本文件锁的就是它们）
===============================================================================
① **向后兼容**：没有 `_catalog/` → 退回运行目录；**没有标记 → 视为全球**
   → 对现有数据，过滤后结果**逐字不变**（否则一上线就会清空所有词表）。
② **不污染**：带市场标记的项，只对该市场可见。

⚠ 探针抓到的致命 bug（已修，这里立测试防回归）：
   第一版 `is_visible` 把 `None` 用 `str()` 变成了 `"None"` → 被当成"一个叫 None 的
   市场标记" → `is_visible(None, 'US')` 返回 False → 连带 `filter_values(值, None, 市场)`
   返回**空列表** → **整个词表会被清空**。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.attr_stability.catalog import (
    GLOBAL_MARKET, VALUES_MARKET_KEY, catalog_dir_of, catalog_meta, filter_items,
    filter_values, is_visible, market_of, parse_cat, resolve_definition_dir,
    resolve_rules_path, resolve_spec_path, tag_values, values_market_map,
)


# ---------------------------------------------------------------- ① 目录名解析
def test_parse_cat_real_names():
    """用**真实存在的**目录名验证（数据里抄的，不是我编的）。"""
    assert parse_cat("Cutting Boards-US-202608") == {
        "类目": "Cutting Boards", "市场": "US", "年月": "202608"}
    assert parse_cat("Chopping Boards-UK-202608") == {
        "类目": "Chopping Boards", "市场": "UK", "年月": "202608"}
    assert parse_cat("Air Purifiers for Home Pets-US-202608") == {
        "类目": "Air Purifiers for Home Pets", "市场": "US", "年月": "202608"}


def test_parse_cat_unrecognized_returns_whole_name():
    """**认不出就整串当类目，绝不猜** —— 猜错会把不相干的类目并到一起。"""
    for n in ("salt and pepper mill sets", "Cutting-Boards", "your-category-US-YYYYMM", ""):
        assert parse_cat(n)["类目"] == n
        assert parse_cat(n)["市场"] == "" and parse_cat(n)["年月"] == ""


# ---------------------------------------------------------------- ② 向后兼容
def test_no_mark_means_global():
    """★ 铁律：没标记 = 全球。**含 `None`**（探针抓到的坑就在这）。"""
    assert is_visible(None, "US") is True
    assert is_visible([], "US") is True
    assert is_visible("", "US") is True
    assert is_visible([None], "US") is True
    assert is_visible([GLOBAL_MARKET], "US") is True


def test_marked_item_only_visible_to_its_market():
    assert is_visible(["US"], "US") is True
    assert is_visible(["US"], "DE") is False
    assert is_visible(["US", "DE"], "DE") is True


def test_no_market_category_sees_everything():
    """类目没市场概念（如 `Cutting-Boards`）→ 不该因为有标记就把项挡掉。"""
    assert is_visible(["US"], "") is True


def test_filter_values_unchanged_when_no_tags():
    """★★ 最关键的一条：**无标记时逐字不变**。

    否则一上线，所有 `params.values` 都会被过滤成空 → 词表全丢。
    """
    vals = ["竹制", "钛金属", "304不锈钢"]
    for tags in (None, {}, {"别的值": ["US"]}):
        for mk in ("US", "DE", ""):
            assert filter_values(vals, tags, mk) == vals, (tags, mk)


def test_filter_values_by_market():
    vals = ["竹制", "钛金属", "304不锈钢"]
    tags = {"钛金属": ["US"]}
    assert filter_values(vals, tags, "US") == vals
    assert filter_values(vals, tags, "DE") == ["竹制", "304不锈钢"]
    # 该属性没有 tags 的项，任何市场都看得见（保留顺序）
    assert filter_values([], tags, "DE") == []


def test_filter_items_default_tag_key():
    items = [{"说明": "a", "_市场": ["US"]}, {"说明": "b"}, {"说明": "c", "_市场": ["DE"]}]
    assert [x["说明"] for x in filter_items(items, "US")] == ["a", "b"]
    assert [x["说明"] for x in filter_items(items, "DE")] == ["b", "c"]
    assert [x["说明"] for x in filter_items(items, "")] == ["a", "b", "c"]


# ---------------------------------------------------------------- ③ 定义层解析
def test_resolve_falls_back_to_run_dir(tmp_path):
    """**没有 `_catalog/` → 退回运行目录**（现有 20 个目录行为不变）。"""
    (tmp_path / "Cutting Boards-US-202608").mkdir()
    got = resolve_definition_dir(tmp_path, "Cutting Boards-US-202608")
    assert got == tmp_path / "Cutting Boards-US-202608"


def test_resolve_prefers_catalog_when_present(tmp_path):
    """有 `_catalog/<类目>/` → **同类目的不同市场/年月**都用它（跨市场共享）。"""
    (tmp_path / "_catalog" / "Cutting Boards").mkdir(parents=True)
    (tmp_path / "Cutting Boards-US-202608").mkdir()
    (tmp_path / "Cutting Boards-UK-202609").mkdir()
    want = catalog_dir_of(tmp_path, "Cutting Boards")
    assert resolve_definition_dir(tmp_path, "Cutting Boards-US-202608") == want
    assert resolve_definition_dir(tmp_path, "Cutting Boards-UK-202609") == want


def test_differently_named_categories_do_not_auto_share(tmp_path):
    """★ **类目名不同就不共享** —— 这是刻意的，不是漏了。

    数据里 `Cutting Boards-US-202608` 与 `Chopping Boards-UK-202608` 是
    **同一个商品类目**（Cutting = Chopping），但名字不同。解析器**认不出来**，
    于是各走各的（回退到运行目录）。

    **要跨这种"同物异名"共享，必须有显式的类目身份声明**（甲方案的
    `_catalog.json`/`_inherits`）—— **不能靠猜**：猜错会把不相干的类目并到一起，
    而那会静默污染值域、极难发现。
    """
    (tmp_path / "_catalog" / "Cutting Boards").mkdir(parents=True)
    (tmp_path / "Chopping Boards-UK-202608").mkdir()
    got = resolve_definition_dir(tmp_path, "Chopping Boards-UK-202608")
    assert got == tmp_path / "Chopping Boards-UK-202608", "不同名的类目不该自动共享"


def test_catalog_meta_missing_returns_skeleton(tmp_path):
    m = catalog_meta(tmp_path, "Cutting Boards")
    assert m["类目"] == "Cutting Boards" and m["市场"] == [] and m["运行"] == []


def test_catalog_meta_reads_file(tmp_path):
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "_catalog.json").write_text(json.dumps(
        {"类目": "Cutting Boards", "市场": ["US", "UK"], "_说明": "x"}),
        encoding="utf-8")
    m = catalog_meta(tmp_path, "Cutting Boards")
    assert m["市场"] == ["US", "UK"] and m["_说明"] == "x"


# ---------------------------------------------------------------- ⑤ 定义层解析
def test_resolve_spec_falls_back_to_run_dir(tmp_path):
    """**没有共享层 → 退回运行目录**（现有 60+ 个读点行为不变）。"""
    (tmp_path / "Cutting Boards-US-202608").mkdir()
    got = resolve_spec_path(tmp_path, "Cutting Boards-US-202608")
    assert got == tmp_path / "Cutting Boards-US-202608" / \
        "Cutting Boards-US-202608-attribute-spec.json"


def test_resolve_spec_own_dir_wins(tmp_path):
    """★★ **自己的目录第一优先**（用户口径 2026-10-10：「肯定是自己的目录」）。

    也最安全 —— 自己的选择永远压过别人，不会"UK 站悄悄用了 US 站的属性集"。
    """
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "Cutting Boards-US-202608-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    run = tmp_path / "Cutting Boards-US-202608"
    run.mkdir()
    (run / "Cutting Boards-US-202608-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    assert resolve_spec_path(tmp_path, "Cutting Boards-US-202608") == \
        run / "Cutting Boards-US-202608-attribute-spec.json", "自己目录里有就用自己那份"


def test_resolve_spec_inherits_when_own_dir_empty(tmp_path):
    """自己目录**没有**时 → 从共享层继承（这正是迁移的意义：定义层搬走后仍能找到）。"""
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "Cutting Boards-US-202608-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    (tmp_path / "Cutting Boards-UK-202609").mkdir()
    got = resolve_spec_path(tmp_path, "Cutting Boards-UK-202609")
    assert got == d / "Cutting Boards-US-202608-attribute-spec.json"


def test_resolve_spec_refuses_to_guess_when_ambiguous(tmp_path):
    """★ **多份候选时不猜** —— 多市场各一份时猜错就是静默污染，交人工在 `_catalog.json` 声明。"""
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "Cutting Boards-US-202608-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    (d / "Cutting Boards-DE-202608-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    run = tmp_path / "Cutting Boards-UK-202609"
    run.mkdir()
    got = resolve_spec_path(tmp_path, "Cutting Boards-UK-202609")
    assert got == run / "Cutting Boards-UK-202609-attribute-spec.json", \
        "有歧义 → 返回自己目录（不存在），逼人工去声明"
    # 声明之后就能用了
    (d / "_catalog.json").write_text(
        json.dumps({"属性集": "Cutting Boards-DE-202608-attribute-spec.json"}), encoding="utf-8")
    assert resolve_spec_path(tmp_path, "Cutting Boards-UK-202609").name == \
        "Cutting Boards-DE-202608-attribute-spec.json"


def test_resolve_spec_declared_wins(tmp_path):
    """`_catalog.json` 的 `属性集` 显式声明**优先于猜测**（自己目录没有时）。"""
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "a-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    (d / "b-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    (d / "_catalog.json").write_text(json.dumps({"属性集": "a-attribute-spec.json"}),
                                     encoding="utf-8")
    assert resolve_spec_path(tmp_path, "Cutting Boards-US-202608").name == "a-attribute-spec.json"


def test_resolve_rules_own_dir_first(tmp_path):
    """规则文件同样**自己的目录第一优先**。"""
    run = tmp_path / "Cutting Boards-US-202608"
    run.mkdir()
    (run / "cutting_boards.rules.json").write_text("{}", encoding="utf-8")
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "shared.rules.json").write_text("{}", encoding="utf-8")
    assert resolve_rules_path(tmp_path, "Cutting Boards-US-202608",
                              "cutting_boards.rules.json") == run / "cutting_boards.rules.json"


def test_resolve_rules_inherits_when_own_dir_empty(tmp_path):
    d = catalog_dir_of(tmp_path, "Cutting Boards")
    d.mkdir(parents=True)
    (d / "cutting_boards.rules.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Cutting Boards-UK-202609").mkdir()
    got = resolve_rules_path(tmp_path, "Cutting Boards-UK-202609", "chopping_boards.rules.json")
    assert got == d / "cutting_boards.rules.json"


# ---------------------------------------------------------------- ④ 市场标记读写
def test_values_market_map_and_tagging():
    """旁挂映射：**`values` 保持字符串列表**（十几个消费者按字符串读），标记放旁边。"""
    params = {"values": ["竹制", "钛金属"]}
    assert values_market_map(params) == {}
    tag_values(params, {"钛金属": ["US"]})
    assert params["values"] == ["竹制", "钛金属"], "values 类型/内容都不许动"
    assert params[VALUES_MARKET_KEY] == {"钛金属": ["US"]}
    assert values_market_map(params) == {"钛金属": ["US"]}


def test_tag_values_global_is_not_written():
    """全球**不写标记** —— 写 `["*"]` 只会让文件里全是噪音。"""
    params = {"values": ["竹制"]}
    tag_values(params, {"竹制": GLOBAL_MARKET})
    assert VALUES_MARKET_KEY not in params
    tag_values(params, {"竹制": ["US"]})
    assert params[VALUES_MARKET_KEY] == {"竹制": ["US"]}
    tag_values(params, {"竹制": GLOBAL_MARKET})          # 改回全球 → 删掉标记
    assert VALUES_MARKET_KEY not in params


def test_market_of_shapes():
    assert market_of(None) == [GLOBAL_MARKET]
    assert market_of({"a": 1}) == [GLOBAL_MARKET]
    assert market_of({"_市场": "US"}) == ["US"]
    assert market_of({"_市场": ["US", "DE"]}) == ["US", "DE"]
    # 直接传一串市场也算（方便调用方不必包一层）
    assert market_of(["US"]) == ["US"]
    assert market_of([]) == [GLOBAL_MARKET]
