# -*- coding: utf-8 -*-
"""VOC 采集计划与父ASIN 解析测试(纯逻辑, 不联网)。

重点回归: **页数预算精确等于配置值**, 且探针页不被重复抓取(曾因此每家族多花 1 页积分)。
"""
from __future__ import annotations

import os

import openpyxl
import pytest

from tools.voc import pipeline as P


# ---------------------------------------------------------------- 星级模板与页数计划
def test_parse_star_mix_default_and_tolerance():
    mix = P.parse_star_mix("critical:3,four_star:1,five_star:1")
    assert mix == {"critical": 3, "four_star": 1, "five_star": 1}
    # 中文分号/空格/非法项 容忍
    assert P.parse_star_mix("critical：3；four_star:1,  unknown:5") == {"critical": 3, "four_star": 1}
    assert P.parse_star_mix("") == {}


def test_collection_plan_hits_exact_page_budget():
    for pages in (1, 2, 3, 5, 10, 30):
        plan = P.build_collection_plan(pages, "critical:3,four_star:1,five_star:1")
        assert sum(p for _, _, _, p in plan) == pages, "总页数必须精确等于配置值"
        assert all(p >= 1 for _, _, _, p in plan), "每层至少 1 页"
    plan5 = {s: p for s, _, _, p in P.build_collection_plan(5, "critical:3,four_star:1,five_star:1")}
    assert plan5 == {"critical": 3, "four_star": 1, "five_star": 1}
    plan2 = {s: p for s, _, _, p in P.build_collection_plan(2, "critical:3,four_star:1,five_star:1")}
    assert sum(plan2.values()) == 2 and len(plan2) == 2


def test_plan_pages_equals_configured_pages():
    assert P.plan_pages(5, "critical:3,four_star:1,five_star:1") == 5
    assert P.plan_pages(12, "critical:3,four_star:1,five_star:1") == 12


def test_remaining_plan_excludes_probe_page():
    """探针(1–3★ 第 1 页)已执行 → 剩余计划里 critical 必须减 1, 否则重复抓同一页。"""
    full = {s: p for s, _, _, p in P.build_collection_plan(5, "critical:3,four_star:1,five_star:1")}
    left = {s: p for s, _, _, p in P.remaining_plan(5, "critical:3,four_star:1,five_star:1")}
    assert full["critical"] == 3
    assert left["critical"] == 2, "探针已用掉 critical 的 1 页"
    assert sum(left.values()) == sum(full.values()) - 1


def test_stratum_mapping_matches_sources():
    plan = P.build_collection_plan(5, "critical:3,four_star:1,five_star:1")
    got = {s: (f, stars) for s, f, stars, _ in plan}
    assert got["critical"] == ("critical", [1, 2, 3])
    assert got["four_star"] == ("four_star", [4])
    assert got["five_star"] == ("five_star", [5])


# ---------------------------------------------------------------- 父ASIN 解析
def _traffic(cat_dir: str) -> str:
    d = os.path.join(cat_dir, "14_流量拆分")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "流量拆分数据-需人工补充.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AI已补充"
    ws.append(["父ASIN", "变体ASIN", "月销量", "品牌", "商品标题"])
    ws.append(["B02D13A103", "B0E757120E", 10, "低销", "T-A"])      # 合计 10
    ws.append(["B0BFEBB5AB", "B0385546E5", 800, "高销", "T-B"])      # 合计 800+20
    ws.append(["B0BFEBB5AB", "B011BB9E40", 20, "高销", "T-B"])
    ws.append(["bad-asin", "B0E64ED92A", 999, "噪声", "T-C"])        # 非法父ASIN → 忽略
    wb.save(path)
    wb.close()
    return path


def test_load_parents_from_traffic_sorted_and_limited(ws_tmp):
    cat_dir = str(ws_tmp)
    xlsx = _traffic(cat_dir)
    parents, kids, meta = P.load_parents_from_traffic(xlsx, limit=10)
    assert parents == ["B0BFEBB5AB", "B02D13A103"], "按父体月销量合计降序"
    assert kids["B0BFEBB5AB"] == ["B0385546E5", "B011BB9E40"]
    assert meta["B0BFEBB5AB"]["brand"] == "高销"
    limited, _, _ = P.load_parents_from_traffic(xlsx, limit=1)
    assert limited == ["B0BFEBB5AB"]


def test_resolve_parents_falls_back_to_parents_txt(ws_tmp):
    cat_dir = str(ws_tmp)
    with open(os.path.join(cat_dir, "t-parents.txt"), "w", encoding="utf-8") as fh:
        fh.write("\ufeffB00000000C\n\n# 注释\nB00000000C,B0EFC31FEF\n")
    parents, kids, _meta, source = P.resolve_parents(cat_dir, "t")
    assert parents == ["B051064F6D", "B0EFC31FEF"], "容忍 BOM/空行/注释/逗号"
    assert source.endswith("t-parents.txt") and kids == {"B051064F6D": [], "B0EFC31FEF": []}


def test_resolve_parents_raises_when_both_missing(ws_tmp):
    with pytest.raises(FileNotFoundError):
        P.resolve_parents(str(ws_tmp), "t")


def test_traffic_path_shape():
    assert P.traffic_path("data/X").replace("\\", "/").endswith("14_流量拆分/流量拆分数据-需人工补充.xlsx")


# ---------------------------------------------------------------- 归一化+去重(直接复用已验证模块)
def test_normalize_and_dedupe_merges_cross_source():
    raw = [
        {"_source": "pangolinfo", "_request_asin": "B02D13A103", "_stratum": "critical",
         "reviewId": "R1AAAAA00001", "asin": "B0E757120E", "star": "1.0 out of 5 stars",
         "title": "bad", "content": "stopped working", "date": "September 19, 2026",
         "author": "A", "purchased": True, "vineVoice": False, "attributes": [{"Size": "2"}],
         "helpful": "3"},
        {"_source": "sellersprite", "_request_asin": "B02D13A103", "_queried_asin": "B0385546E5",
         "_stratum": "critical", "star": 1, "title": "bad", "content": "stopped working",
         "date": 1789758000000, "author": "A", "verified": True, "vine": False, "likes": 9,
         "skus": ["Size: 2"]},
        {"_source": "pangolinfo", "_request_asin": "B02D13A103", "_stratum": "five_star",
         "reviewId": "R2BBBBB00002", "asin": "B0E757120E", "star": "5.0 out of 5 stars",
         "title": "great", "content": "works great", "date": "August 1, 2026",
         "author": "B", "purchased": True, "vineVoice": False, "helpful": ""},
    ]
    unique, stats, rows, badges = P.normalize_and_dedupe(raw, meta={})
    assert len(rows) == 3
    assert len(unique) == 2, "同一条评论(有ID + 无ID)必须合并"
    merged = [r for r in unique if r["star"] == 1][0]
    assert merged["review_id"] == "R1AAAAA00001"
    assert sorted(merged["sources"]) == ["pangolinfo", "sellersprite"]
    assert merged["helpful"] == "9", "Helpful 取较大值, 不累加"
    assert stats["cross_source_merges"] == 1
    assert not badges, "无星级非法/缺父ASIN/空正文"


def test_marketplace_of_maps_pangolin_site():
    assert P.marketplace_of("amz_us") == "US"
    assert P.marketplace_of("amz_uk") == "UK"


# ---------------------------------------------------------------- 双源兜底 + 无熔断
def test_point_budget_defaults_to_unlimited_and_ignores_config():
    """不再有费用熔断: 配置里残留 VOC_POINT_BUDGET 也不生效(仅显式传参才限流)。"""
    import inspect
    src = inspect.getsource(P.run_voc)
    assert 'config.get("VOC_POINT_BUDGET"' not in src, "run_voc 不得再读配置里的预算键"
    assert P.DEFAULT_POINT_BUDGET == 0, "缺省 = 不限"


def test_switch_source_rotates_and_marks_all_dead():
    """额度耗尽 → 切换到兜底源(全运行生效); 无备用源时标记 all_sources_exhausted。"""
    from core.mcp import MCPBusinessError

    col = P.Collector.__new__(P.Collector)          # 只测纯逻辑, 不建客户端
    col.sources = ["pangolinfo", "sellersprite"]
    col.stopped_reason = ""
    col.sources_dead = False
    col.log = lambda *a, **k: None

    exhausted = MCPBusinessError("get_amazon_reviews", 402, "积分不足, 请充值")
    assert col._switch_source(exhausted, "pangolinfo") is True
    assert col.sources == ["sellersprite", "pangolinfo"], "耗尽源排到末尾 → 后续全走兜底"

    # 兜底源也耗尽 → 标记整体收尾, 不再空转
    col.sources = ["sellersprite"]
    assert col._switch_source(exhausted, "sellersprite") is False
    assert col.sources_dead is True
    assert col.stopped_reason == "all_sources_exhausted"
    assert col.all_sources_dead() is True

    # 非额度类错误不切换(参数错/ASIN 错)
    col2 = P.Collector.__new__(P.Collector)
    col2.sources = ["pangolinfo", "sellersprite"]
    col2.stopped_reason = ""
    col2.sources_dead = False
    col2.log = lambda *a, **k: None
    bad = MCPBusinessError("get_amazon_reviews", 400, "invalid asin")
    assert col2._switch_source(bad, "pangolinfo") is False
    assert col2.sources == ["pangolinfo", "sellersprite"]
    assert col2.sources_dead is False


def test_resolve_pangolinfo_creds_url_is_hardcoded(monkeypatch):
    """★ URL **写死在代码里**（2026-10-12 用户口径）：config / 环境变量都**不再**能改它。

    为什么：它是**固定的服务端点、不是秘密** —— 放 config.yaml 只会让 14 个类目各抄一份空值、
    Web 配置清单里还多两个没用的框。KEY 仍走 配置 → 环境变量 → 本机 codex 文件。
    """
    monkeypatch.setenv("PANGOLIN_MCP_URL", "https://env.example/mcp")
    monkeypatch.setenv("PANGOLIN_MCP_KEY", "env-token")

    from core.config import Config
    url, key = P.resolve_pangolinfo_creds(
        Config({"CAT": "t", "DATA_ROOT": ".", "PANGOLIN_MCP_URL": "https://cfg.example/mcp"}))
    assert url == P.PANGOLIN_MCP_URL, "URL 必须来自代码常量，不受 config/环境变量影响"
    assert key == "env-token"


def test_resolve_pangolinfo_creds_key_config_wins_over_env(monkeypatch):
    """KEY 的顺序仍是 **config → 环境变量 → 本机 codex 文件**（向后兼容）。"""
    monkeypatch.setenv("PANGOLIN_MCP_KEY", "env-token")

    from core.config import Config
    cfg = Config({"CAT": "t", "DATA_ROOT": ".", "PANGOLIN_MCP_KEY": "cfg-token"})
    url, key = P.resolve_pangolinfo_creds(cfg)
    assert url == P.PANGOLIN_MCP_URL and key == "cfg-token", "config 优先于环境变量"


# ---------------------------------------------------------------- VOC_SITE 自适应(fix 抓错站)
def test_voc_site_follows_site():
    """★ `VOC_SITE` **由 `SITE` 自适应**（2026-10-12 用户口径：不再单设一个一样的参数）。"""
    from core.config import Config
    for site, want in (("美国", "amz_us"), ("英国", "amz_uk"), ("德国", "amz_de"),
                       ("日本", "amz_jp"), ("法国", "amz_fr")):
        got = P.voc_site_of(Config({"CAT": "t", "SITE": site}))
        assert got == want, (site, got)


def test_voc_site_ignores_stale_config_copy():
    """★★ **这是修掉的那个真 bug**：配置里残留的 `VOC_SITE`（旧副本）**不得再生效**。

    实测背景：`VOC_SITE` 原来是一份独立拷贝，Web「新建类目」`copy_from` 时被一起复制 →
    英国站/德国站的类目里仍是 `amz_us`（实测 3 个类目全中）→
    **英/德站一直在抓美国站评论，且没有任何提示**。现在以 `SITE` 为准。
    """
    from core.config import Config
    cfg = Config({"CAT": "t", "SITE": "英国", "VOC_SITE": "amz_us"})
    assert P.voc_site_of(cfg) == "amz_uk", "必须跟 SITE 走，不能沿用旧副本"


def test_voc_site_falls_back_to_market_then_default():
    """`SITE` 认不出 → 回落 `MARKET` 域名；再不行 → 默认 `amz_us`。"""
    from core.config import Config
    assert P.voc_site_of(Config({"CAT": "t", "SITE": "", "MARKET": "amazon.co.uk"})) == "amz_uk"
    assert P.voc_site_of(Config({"CAT": "t", "SITE": "德国站", "MARKET": "amazon.de"})) == "amz_de"
    assert P.voc_site_of(Config({"CAT": "t"})) == P.DEFAULT_SITE


def test_source_priority_is_hardcoded():
    """★ 数据源顺序**写死在代码**（就两个源，没有第三种方式）；配置里的旧键必须被忽略。"""
    from core.config import Config
    from tools.voc import pipeline as _P
    assert _P.DEFAULT_SOURCE_PRIORITY == "pangolinfo,sellersprite"
