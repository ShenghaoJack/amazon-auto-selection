# -*- coding: utf-8 -*-
"""VOC 采集计划测试: 每父体 5 页预算, 且探针页不得被重复抓取(2026-10-09 修的 off-by-one)。"""
import httpx

from core.mcp import MCPBusinessError, parse_mcp_json
from tools.voc.acquire import (DEFAULT_TOTAL_PAGES, PROBE_PAGES, full_plan, looks_exhausted,
                               planned_pages, remaining_plan, stratum_of)


def test_page_budget_is_five_and_probe_not_refetched():
    assert planned_pages() == DEFAULT_TOTAL_PAGES == 5
    left = {s: p for s, _, _, p in remaining_plan()}
    assert left["critical"] == 2, "探针已抓第 1 页, 剩余 critical 只能是 2 页(否则重复抓取)"
    assert left["four_star"] == 1 and left["five_star"] == 1
    assert PROBE_PAGES + sum(left.values()) == 5


def test_full_plan_matches_strata():
    plan = {s: p for s, _, _, p in full_plan()}
    assert plan == {"critical": 3, "four_star": 1, "five_star": 1}
    assert sum(plan.values()) == 5


def test_stratum_mapping():
    assert stratum_of("critical") == ("critical", [1, 2, 3])
    assert stratum_of("four_star") == ("four_star", [4])


def test_exhaustion_detection():
    assert looks_exhausted("积分不足, 请充值")
    assert looks_exhausted("insufficient points")
    assert not looks_exhausted("invalid asin B095AC5A88")


class _Resp:
    def __init__(self, payload=None, text="", ctype="application/json"):
        self._payload = payload
        self.text = text
        self.headers = {"content-type": ctype}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_parse_mcp_json_plain_and_sse():
    plain = _Resp(payload={"result": {"tools": []}})
    assert parse_mcp_json(plain) == {"result": {"tools": []}}

    sse_text = 'event: message\ndata: {"result":{"isError":false}}\n\n'
    sse = _Resp(payload=None, text=sse_text, ctype="text/event-stream")
    assert parse_mcp_json(sse) == {"result": {"isError": False}}

    # 有些服务端不声明 content-type, 但正文是 SSE → 也要能解析
    sse2 = _Resp(payload=None, text='data: {"a":1}\n\n')
    assert parse_mcp_json(sse2) == {"a": 1}


def test_business_error_carries_code_and_payload():
    err = MCPBusinessError("get_amazon_reviews", 402, "余额不足", {"code": 402, "pointCost": 0})
    assert isinstance(err, RuntimeError)
    assert err.code == 402 and err.payload["code"] == 402
    assert "余额不足" in str(err)


def test_mcp_client_endpoint_override_uses_given_keys():
    from core.config import Config
    from core.mcp import MCPClient
    cfg = Config({"PANGOLIN_MCP_URL": "https://example.invalid/mcp",
                  "PANGOLIN_MCP_KEY": "tok-123"})
    c = MCPClient(cfg, url_key="PANGOLIN_MCP_URL", key_key="PANGOLIN_MCP_KEY",
                  auth_header="Authorization", auth_scheme="Bearer ")
    assert c._url() == "https://example.invalid/mcp"
    assert c._headers()["Authorization"] == "Bearer tok-123"

    ss = MCPClient(Config({"SS_MCP_URL": "https://ss.invalid/mcp", "SS_MCP_KEY": "k"}))
    assert ss._headers()["secret-key"] == "k", "默认仍走卖家精灵的 secret-key 头"
