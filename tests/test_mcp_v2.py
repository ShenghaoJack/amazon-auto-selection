# -*- coding: utf-8 -*-
"""卖家精灵 MCP v2 全面适配单元测试（v1 已弃用）。"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from core.config import Config
from core.errors import StepFailedError
from core.mcp import (
    MCPBusinessError,
    MCPClient,
    SS_MCP_V2_URL,
    normalize_ss_mcp_url,
)
from core.paths import Paths
from steps.base import BaseStep, StepContext
from tools.kwgen_hybrid.glue import CachedCalls, LiveMiner, WeeklyEvidence


class _DummyStep(BaseStep):
    number = 5
    title = "dummy"

    def run(self, ctx: StepContext) -> None:
        pass


def _v2_client(site: str = "美国") -> MCPClient:
    cfg = Config({
        "DRY_RUN": False,
        "SITE": site,
        "SS_MCP_URL": SS_MCP_V2_URL,
        "SS_MCP_KEY": "test-key",
    })
    return MCPClient(cfg)


def test_default_config_and_legacy_v1_url_auto_upgrade():
    assert Config({}).get("SS_MCP_URL") == SS_MCP_V2_URL
    assert normalize_ss_mcp_url("https://mcp.sellersprite.com/mcp") == SS_MCP_V2_URL
    assert normalize_ss_mcp_url("http://mcp.sellersprite.com/mcp/") == SS_MCP_V2_URL
    assert normalize_ss_mcp_url("https://custom.example/mcp") == "https://custom.example/mcp"

    legacy_ss = MCPClient(Config({"SS_MCP_URL": "https://mcp.sellersprite.com/mcp", "SS_MCP_KEY": "k"}))
    assert legacy_ss._url() == SS_MCP_V2_URL

    pango = MCPClient(
        Config({"PANGOLIN_MCP_URL": "https://mcp.sellersprite.com/mcp", "PANGOLIN_MCP_KEY": "k"}),
        url_key="PANGOLIN_MCP_URL",
        key_key="PANGOLIN_MCP_KEY",
    )
    assert pango._url() == "https://mcp.sellersprite.com/mcp"


def test_keyword_miner_v2_recall_and_verify(monkeypatch):
    client = _v2_client()
    calls = []

    def fake_call(tool, args, retries=3):
        calls.append((tool, args))
        if tool == "keyword.related.list":
            return {
                "dataMonth": "202610",
                "total": 1372,
                "page": {"cursor": 100, "size": 100, "hasMore": True, "nextCursor": 200},
                "items": [
                    {"keyword": "air fryer", "searchVolume": 2718141, "ppcBid": 0.63},
                ],
            }
        if tool == "keyword.batch.info":
            out = []
            for kw in args["keywords"]:
                if kw == "kw_missing":
                    out.append({"keyword": kw, "resultStatus": "no_data"})
                else:
                    out.append({"keyword": kw, "searchVolume": 1200, "bid": 0.9, "resultStatus": "success"})
            return {"items": out, "month": args.get("month")}
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(client, "_call", fake_call)

    # 1) 根词扩展召回
    rec = client.keyword_miner({"marketplace": "US", "keyword": "air fryer", "filterRootWord": True, "size": 100, "page": 2})
    assert calls[0][0] == "keyword.related.list"
    assert calls[0][1] == {
        "marketplace": "US",
        "seedKeyword": "air fryer",
        "page": {"cursor": 100, "size": 100},
        "includeKeywords": ["air fryer"],
    }
    assert rec["total"] == 1372
    assert rec["hasNextPage"] is True
    assert rec["items"][0]["searches"] == 2718141
    assert rec["items"][0]["bid"] == 0.63

    # 2) 批量精准验证(25 词拆成 20 + 5，过滤 no_data)
    words = [f"kw_{i}" for i in range(24)] + ["kw_missing"]
    ver = client.keyword_miner({"marketplace": "US", "historyDate": "2026-10", "keywordList": words})
    batch_calls = [c for c in calls if c[0] == "keyword.batch.info"]
    assert len(batch_calls) == 2
    assert len(batch_calls[0][1]["keywords"]) == 20
    assert len(batch_calls[1][1]["keywords"]) == 5
    assert batch_calls[0][1]["month"] == "202610"
    assert len(ver["items"]) == 24
    assert all(it["searches"] == 1200 for it in ver["items"])


def test_keyword_trend_v2_normalization(monkeypatch):
    client = _v2_client()
    seen = {}

    def fake_call(tool, args, retries=3):
        seen["tool"] = tool
        seen["args"] = args
        return {
            "series": {
                "searchVolume": [
                    {"date": "2025-10", "value": 2000},
                    {"date": "2026-10", "value": 2500},
                ],
                "purchaseVolume": [
                    {"date": "2025-10", "value": 40},
                    {"date": "2026-10", "value": 50},
                ],
                "purchaseRate": [
                    {"date": "2026-10", "value": 0.02},
                ],
                "yearlyGrowth": [
                    {"date": "2026-10", "value": 0.25},
                ],
                "chainGrowth": [
                    {"date": "2026-10", "value": 0.0833},
                ],
                "threeMonthGrowth": [
                    {"date": "2026-10", "value": -0.0061},
                ],
            }
        }

    monkeypatch.setattr(client, "_call", fake_call)
    res = client.keyword_trend("air fryer", "202610")
    assert seen["tool"] == "keyword.trend"
    assert seen["args"]["granularity"] == "MONTH"
    assert res["month"] == "202610"
    assert res["search_volume"] == 2500.0
    assert res["mom_yoy"] == 25.0
    assert res["purchase_volume"] == 50.0
    assert res["purchase_rate"] == 0.02
    assert res["mom_change"] == 8.33
    assert res["growth_3m"] == -0.61
    assert len(res["monthly_series"]) == 2


def test_aba_weekly_bid_and_raw_v2(monkeypatch):
    client = _v2_client()

    def fake_call(tool, args, retries=3):
        if tool == "keyword.aba.trend":
            assert args == {"marketplace": "US", "keyword": "air fryer", "granularity": "WEEK"}
            return {
                "series": {
                    "abaRank": [
                        {"date": "2026-10-24", "value": 80},
                        {"date": "2026-10-31", "value": 76},
                    ],
                    "abaSearchVolume": [
                        {"date": "2026-10-24", "value": 350000},
                        {"date": "2026-10-31", "value": 385326},
                    ],
                }
            }
        if tool == "keyword.batch.info":
            assert args == {"marketplace": "US", "keywords": ["air fryer"], "month": "202610"}
            return {"items": [{"keyword": "air fryer", "bid": 0.63, "bidMin": 0.49, "bidMax": 0.79}]}
        raise AssertionError(tool)

    monkeypatch.setattr(client, "_call", fake_call)
    raw = client.aba_weekly_raw("air fryer", "US")
    assert raw == [
        {"label": "20261024", "rank": 80, "searches": 350000},
        {"label": "20261031", "rank": 76, "searches": 385326},
    ]

    bid = client.aba_weekly_bid("air fryer", "202610")
    assert bid["week"] == "20261031"
    assert bid["weekly_search_volume"] == 385326
    assert bid["search_rank"] == 76
    assert bid["cpc"] == 0.63 and bid["cpc_min"] == 0.49 and bid["cpc_max"] == 0.79


def test_google_trend_v2_monthly_and_weekly(monkeypatch):
    client = _v2_client()
    payload = {
        "series": {
            "interest": [
                {"date": "2026-09-06", "value": 40},
                {"date": "2026-09-13", "value": 60},
                {"date": "2026-09-20", "value": None},
                {"date": "2026-10-04", "value": 50},
            ]
        }
    }
    monkeypatch.setattr(client, "_call", lambda tool, args, retries=3: payload)

    monthly = client.google_trend("air fryer")
    assert monthly["google_monthly_index"] == {"2026-09": 50.0, "2026-10": 50.0}

    weekly = client.google_trend_weekly("air fryer")
    assert weekly["google_weekly_index"] == {
        "2026-09-06": 40,
        "2026-09-13": 60,
        "2026-10-04": 50,
    }

    monkeypatch.setattr(client, "_call", lambda tool, args, retries=3: {"series": {}})
    with pytest.raises(ValueError, match="series.interest"):
        client.google_trend_weekly("air fryer")


def test_competitor_lookup_v2_cursor_pagination_and_iso_date(monkeypatch):
    client = _v2_client()
    cursors = []

    def fake_call(tool, args, retries=3):
        assert tool == "asin.list"
        assert args["dataMonth"] == "202610"
        c = args["page"]["cursor"]
        cursors.append(c)
        if c == 0:
            return {
                "items": [{"asin": "B000000001", "fulfillment": "FBA", "availableDate": "2020-07-23"}],
                "page": {"cursor": 0, "size": 100, "hasMore": True, "nextCursor": 1},
            }
        return {
            "items": [{"asin": "B000000002", "fulfillment": "AMZ", "availableDate": "2021-01-15"}],
            "page": {"cursor": 1, "size": 100, "hasMore": False, "nextCursor": None},
        }

    monkeypatch.setattr(client, "_call", fake_call)
    rows = client.competitor_lookup("2026-10", ["B000000001", "B000000002"])
    assert cursors == [0, 1]
    assert [r["asin"] for r in rows] == ["B000000001", "B000000002"]
    assert isinstance(rows[0]["availableDate"], int)
    assert datetime.fromtimestamp(rows[0]["availableDate"] / 1000).strftime("%Y-%m-%d") == "2020-07-23"


def test_call_v2_retryable_and_non_retryable_errors(monkeypatch):
    client = _v2_client()
    monkeypatch.setattr(client, "_initialize", lambda: None)
    monkeypatch.setattr("core.mcp.time.sleep", lambda s: None)

    responses = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": json.dumps({"error": {"code": "RATE_LIMIT", "message": "busy", "retryable": True}})}],
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps({"items": [{"keyword": "ok"}]})}],
            },
        },
    ]

    class _FakeResp:
        def __init__(self, body):
            self._body = body
            self.headers = {"content-type": "application/json"}
            self.text = json.dumps(body)

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    class _FakeHttp:
        def __init__(self, seq):
            self._seq = list(seq)

        def post(self, *args, **kwargs):
            return _FakeResp(self._seq.pop(0))

    fake_http = _FakeHttp(responses)
    monkeypatch.setattr(client, "_http", lambda: fake_http)
    out = client._call("keyword.related.list", {"marketplace": "US", "seedKeyword": "ok"}, retries=2)
    assert out == {"items": [{"keyword": "ok"}]}

    # Non-retryable error raises MCPBusinessError immediately
    err_resp = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps({"error": {"code": "INVALID_KEY", "message": "bad key", "retryable": False}})}],
            },
        }
    ]
    fake_err_http = _FakeHttp(err_resp)
    monkeypatch.setattr(client, "_http", lambda: fake_err_http)
    with pytest.raises(MCPBusinessError) as exc_info:
        client._call("keyword.trend", {"marketplace": "US", "keyword": "x"}, retries=2)
    assert exc_info.value.code == "INVALID_KEY"


def test_check_mcp_key_requires_v2_tools(tmp_path, monkeypatch):
    cfg = Config({"CAT": "Test-US-202610", "DATA_ROOT": str(tmp_path), "DRY_RUN": False})
    ctx = StepContext(cfg, Paths(cfg))
    step = _DummyStep()

    monkeypatch.setattr(ctx, "mcp", lambda: SimpleNamespace(check=lambda: ["keyword.trend", "asin.list"]))
    step.check_mcp_key(ctx)

    monkeypatch.setattr(ctx, "mcp", lambda: SimpleNamespace(check=lambda: ["keyword_research_trends"]))
    with pytest.raises(StepFailedError, match="工具列表异常"):
        step.check_mcp_key(ctx)


def test_call_raw_review_translates_to_v2_asin_review_list(monkeypatch):
    client = _v2_client()
    seen = {}

    def fake_call_raw(tool, args, retries=3):
        if tool == "asin.review.list":
            seen["args"] = args
            return {
                "items": [{"title": f"r{i}"} for i in range(4)],
                "total": 42,
                "billing": {"points": 2},
            }
        raise AssertionError(tool)

    orig_call_raw = MCPClient.call_raw

    def dispatcher(tool, args, retries=3):
        if tool == "review":
            return orig_call_raw(client, tool, args, retries=retries)
        return fake_call_raw(tool, args, retries=retries)

    monkeypatch.setattr(client, "call_raw", dispatcher)
    env = client.call_raw("review", {"marketplace": "US", "asin": "B0TEST0042", "starList": [1, 2, 3], "page": 2, "size": 2})
    assert seen["args"] == {"marketplace": "US", "asin": "B0TEST0042", "star": [1, 2, 3], "limit": 4}
    assert env["code"] == "OK"
    assert env["pointCost"] == 2
    assert env["data"]["total"] == 42
    assert env["data"]["items"] == [{"title": "r2"}, {"title": "r3"}]


def test_kwgen_glue_uses_mcp_v2_methods(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    called = []

    class _FakeMCP:
        def keyword_miner(self, req):
            called.append(("miner", req))
            return {"items": [{"keyword": "air fryer", "searches": 100}]}

        def aba_weekly_raw(self, word, site=""):
            called.append(("aba", word, site))
            return [
                {"label": "20261003", "searches": 100},
                {"label": "20261010", "searches": 100},
                {"label": "20261017", "searches": 100},
                {"label": "20261024", "searches": 100},
                {"label": "20261031", "searches": 100},
            ]

    cache = CachedCalls(tmp_path / "cache.json", {"v": "test"})
    miner = LiveMiner(_FakeMCP(), cache)
    res = miner.query({"marketplace": "US", "keyword": "air fryer"})
    assert res["items"][0]["keyword"] == "air fryer"
    assert called[0][0] == "miner"

    weekly = WeeklyEvidence(_FakeMCP())
    found, meta = weekly.query(["air fryer"], "US", "202610")
    assert found["air fryer"] == 400
    assert meta["source"] == "卖家精灵"
    assert ("aba", "air fryer", "US") in called
