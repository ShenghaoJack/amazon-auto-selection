# 单元测试: 官网价格表 (抓取/解析/校验/缓存/兜底)
#
# 全部**离线**: 正常解析走 tests/fixtures/pricing_zh.html(官网真实快照),
# 所有"该不该发网络请求"的用例都 monkeypatch 掉 fetch_official_prices 并计数。
import json
import re
import time
from datetime import datetime
from pathlib import Path

import pytest

from core import prices as P
from core.config import Config

FIXTURE = Path(__file__).parent / "fixtures" / "pricing_zh.html"


@pytest.fixture(autouse=True)
def _clean_memo():
    P.reset_memo()
    yield
    P.reset_memo()


@pytest.fixture()
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _cfg(tmp_path, **over) -> Config:
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T"})
    for k, v in over.items():
        cfg.set(k, v)
    return cfg


def _no_fetch(monkeypatch, calls: list):
    """把抓取换成记账器; 返回 calls 供断言"有没有联网"。"""
    def fake(url, *, timeout=0, want_currency="CNY"):
        calls.append(url)
        raise P.PriceError("测试: 禁止真实联网")
    monkeypatch.setattr(P, "fetch_official_prices", fake)


# ------------------------------------------------------------------ 解析

def test_parse_fixture_prices(html):
    """官网真实页面快照 → 两个模型各 6 个数完全正确。"""
    t = P.parse_pricing_html(html, want_currency="CNY", url="fixture")
    assert t.currency == "CNY"
    assert t.prices["deepseek-flash"] == {
        "off": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0},
        "peak": {"in_hit": 0.04, "in_miss": 2.0, "out": 8.0},
    }
    assert t.prices["deepseek-v4-pro"] == {
        "off": {"in_hit": 0.15, "in_miss": 4.5, "out": 13.5},
        "peak": {"in_hit": 0.30, "in_miss": 9.0, "out": 27.0},
    }


def test_parse_ignores_unrelated_numeric_rows(html):
    """「输出长度 最大 384K」「并发限制 2500」这类纯数字**不能**被当价格。

    回归保护: 曾经的朴素做法(按固定列下标取数)会把这些行错当成价格行。
    """
    t = P.parse_pricing_html(html, want_currency="CNY")
    for model in t.prices:
        for period in ("off", "peak"):
            assert set(t.prices[model][period]) == {"in_hit", "in_miss", "out"}
    assert "384" not in json.dumps(t.prices)


def test_currency_mismatch_raises(html):
    """币种防呆: 把中文页的「元」换成「$」, 配 CNY → 必须报错。

    英文页是美元且数字不同(flash 输出 4元 vs $0.6), 不拦截就会把美元当人民币报。
    """
    with pytest.raises(P.PriceParseError) as ei:
        P.parse_pricing_html(html.replace("元", "$"), want_currency="CNY")
    assert "币种" in str(ei.value)


def test_missing_peak_row_raises(html):
    """rowspan 结构的续行被删掉 → 该度量缺高峰价 → 必须报错(不能静默漏档)。"""
    stripped = re.sub(
        r"<tr[\s\S]*?</tr>",
        lambda m: "" if ("高峰时段" in m.group(0) and "0.30" in m.group(0)) else m.group(0),
        html,
    )
    assert stripped != html
    with pytest.raises(P.PriceParseError) as ei:
        P.parse_pricing_html(stripped, want_currency="CNY")
    assert "缺失" in str(ei.value)


def test_insane_price_order_raises(html):
    """命中输入价 >= 未命中输入价(列错位的典型症状) → 必须报错。"""
    broken = re.sub(r">1元\s*<", ">0.01元<", html)
    assert broken != html, "fixture 里没找到预期单元格, 断言失效"
    with pytest.raises(P.PriceParseError) as ei:
        P.parse_pricing_html(broken, want_currency="CNY")
    assert "错位" in str(ei.value)


def test_peak_windows():
    """高峰 = 北京时间周一~周五 9:00-12:00 / 14:00-18:00; 忽略法定节假日。"""
    monday = datetime(2026, 9, 28)
    assert monday.weekday() == 0
    assert P.is_peak(monday.replace(hour=10)) is True
    assert P.is_peak(monday.replace(hour=8, minute=59)) is False
    assert P.is_peak(monday.replace(hour=12)) is False       # 12:00 起是空闲
    assert P.is_peak(monday.replace(hour=13)) is False
    assert P.is_peak(monday.replace(hour=17, minute=59)) is True
    assert P.is_peak(monday.replace(hour=18)) is False
    assert P.is_peak(datetime(2026, 9, 26, 10)) is False     # 周六
    assert P.is_peak(datetime(2026, 9, 27, 10)) is False     # 周日


# ------------------------------------------------------------------ 别名 / 未定价

def test_model_alias_mapping():
    """官方明文: 旧模型名按 Flash 价计费 —— 项目 12 个类目配的正是旧名。"""
    for alias in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-flash"):
        assert P.canonical_model(alias) == "deepseek-flash"
    assert P.canonical_model("deepseek-v4-pro") == "deepseek-v4-pro"
    assert P.canonical_model("deepseek/deepseek-flash") == "deepseek-flash"   # litellm 写法


def test_unknown_model_is_unpriced_not_zero():
    """未收录模型(如 Config 默认的 deepseek-chat) → 显式"未定价", 不静默按 0 计。"""
    t = P.builtin_table()
    assert P.canonical_model("deepseek-chat") is None
    cost, priced = t.cost("deepseek-chat", in_hit=100, in_miss=100, out=100, peak=False)
    assert cost == 0.0 and priced is False


# ------------------------------------------------------------------ 兜底链

def test_manual_prices_win_and_skip_network(tmp_path, monkeypatch):
    calls: list = []
    _no_fetch(monkeypatch, calls)
    cfg = _cfg(tmp_path, LLM_PRICES={
        "deepseek-flash": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0}
    })
    t = P.resolve_prices(cfg)
    assert t.source == "配置手填 LLM_PRICES"
    assert calls == []


def test_invalid_manual_falls_through(tmp_path, monkeypatch, html):
    """手填价表不合法 → 忽略并继续走 live 流程(而不是把费用算成 0)。"""
    monkeypatch.setattr(P, "fetch_official_prices",
                        lambda *a, **k: P.parse_pricing_html(html, want_currency="CNY"))
    cfg = _cfg(tmp_path, LLM_PRICES={"deepseek-flash": {"in_hit": "oops"}})
    t = P.resolve_prices(cfg)
    assert t.source.startswith("官网实时")


def test_fresh_cache_skips_network(tmp_path, monkeypatch):
    """TTL 内 → 直接用缓存, **一次网络请求都不发**。"""
    calls: list = []
    _no_fetch(monkeypatch, calls)
    cfg = _cfg(tmp_path, LLM_PRICES_TTL_HOURS=24)
    cache = P.cache_path(str(tmp_path))
    fresh = P.builtin_table("写缓存时的来源")
    fresh.fetched_at = time.time()
    P.save_cache(cache, fresh)
    t = P.resolve_prices(cfg)
    assert calls == []
    assert t.source.startswith("磁盘缓存")


def test_stale_cache_used_when_fetch_fails(tmp_path, monkeypatch):
    """缓存过期 + 抓取失败 → 用过期缓存, 来源必须写明"已过期 N 小时"。"""
    calls: list = []
    _no_fetch(monkeypatch, calls)
    cfg = _cfg(tmp_path, LLM_PRICES_TTL_HOURS=1)
    cache = P.cache_path(str(tmp_path))
    old = P.builtin_table("旧")
    old.fetched_at = time.time() - 72 * 3600
    P.save_cache(cache, old)

    t = P.resolve_prices(cfg)
    assert calls, "应当尝试过抓取"
    assert "已过期" in t.source
    assert t.stale is True
    assert set(t.prices) == set(P._BUILTIN_PRICES)


def test_no_cache_falls_back_to_builtin(tmp_path, monkeypatch):
    """抓取失败且无缓存 → 内置快照; 必须告警, 且**不抛异常**。"""
    calls: list = []
    _no_fetch(monkeypatch, calls)

    class _Log:
        def __init__(self): self.warnings = []
        def warning(self, msg): self.warnings.append(msg)

    log = _Log()
    t = P.resolve_prices(_cfg(tmp_path), log=log)
    assert t.source.startswith("内置快照")
    assert any("内置快照" in w for w in log.warnings)


def test_corrupt_cache_is_tolerated(tmp_path, monkeypatch, html):
    """缓存文件损坏 → 不崩, 照常抓取。"""
    cache = P.cache_path(str(tmp_path))
    Path(cache).parent.mkdir(parents=True, exist_ok=True)
    Path(cache).write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(P, "fetch_official_prices",
                        lambda *a, **k: P.parse_pricing_html(html, want_currency="CNY"))
    t = P.resolve_prices(_cfg(tmp_path))
    assert t.source.startswith("官网实时")


def test_mode_builtin_never_fetches(tmp_path, monkeypatch):
    calls: list = []
    _no_fetch(monkeypatch, calls)
    cfg = _cfg(tmp_path, LLM_PRICES_MODE="builtin")
    t = P.resolve_prices(cfg)
    assert t.source.startswith("内置快照")
    assert calls == []
