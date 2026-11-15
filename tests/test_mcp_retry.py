"""回归测试: MCP 预检(initialize / tools-list)必须对瞬时连接错误重试(2026-09-15 用户报障)。

现象: 步骤06 开头调用 `check_mcp_key()` → `MCPClient.check()`, 其中 `_initialize()`/`tools/list`
各只有**一次** POST。一次瞬时 TLS 断开即整步 FAILED:
  步骤 06 失败: 卖家精灵 MCP 连接失败: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol
而 19:41:59 步骤05 刚用同一个 MCP 成功跑完 → 纯属网络抖动; `_call()` 一直有 3 次重试
(`httpx.HTTPError`/`OSError`, `ssl.SSLError` 属 `OSError` 子类), 预检却没有 → 补齐。
"""
import ssl

import httpx
import pytest

from core.config import Config
from core.mcp import MCPClient


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self._payload = payload if payload is not None else {"result": {"tools": []}}
        self.status_code = status
        self.headers = headers or {}
        self.request = httpx.Request("POST", "https://mcp.example/mcp")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=self.request, response=self)

    def json(self):
        return self._payload


def _client() -> MCPClient:
    return MCPClient(Config({"SS_MCP_URL": "https://mcp.example/mcp", "SS_MCP_KEY": "k",
                             "SITE": "美国", "DRY_RUN": False}))


def test_call_raw_jsonrpc_error_is_not_treated_as_empty_success(monkeypatch):
    from core.mcp import MCPBusinessError
    client = _client()
    monkeypatch.setattr(client, "_initialize", lambda: None)
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: _Resp(
        payload={"error": {"code": -32603, "message": "upstream failure"}}))
    with pytest.raises(MCPBusinessError, match="upstream failure"):
        client.call_raw("review", {})


def test_initialize_retries_transient_ssl_eof(monkeypatch):
    """第一次 initialize 抛 `ssl.SSLError`(UNEXPECTED_EOF_WHILE_READING) → 重试后成功, 不抛给步骤。"""
    calls = {"n": 0}

    def _post(self, url, headers=None, json=None):     # noqa: A002
        calls["n"] += 1
        if calls["n"] == 1:
            raise ssl.SSLError(8, "EOF occurred in violation of protocol (_ssl.c:1032)")
        return _Resp(headers={"Mcp-Session-Id": "sid-1"})

    monkeypatch.setattr(httpx.Client, "post", _post)
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    c = _client()
    c._initialize()                                   # 不应抛
    assert calls["n"] == 2, "应在瞬时错误后重试一次"
    assert c._session_id == "sid-1"


def test_check_retries_and_returns_real_tools(monkeypatch):
    """`check()` 的 tools/list 撞上瞬时错误 → 重试成功; 仍返回真实工具列表。"""
    calls = {"n": 0}
    tools = [{"name": "keyword_research_trends"}, {"name": "secret_expired"}]

    def _post(self, url, headers=None, json=None):     # noqa: A002
        calls["n"] += 1
        if calls["n"] == 2:                           # 第 1 次(initialize)成功, 第 2 次(tools/list)抖动
            raise httpx.ReadError("connection reset by peer")
        return _Resp(payload={"result": {"tools": tools}})

    monkeypatch.setattr(httpx.Client, "post", _post)
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    got = _client().check()
    assert "keyword_research_trends" in got and calls["n"] == 3


def test_transient_failure_exhausts_retries_with_clear_error(monkeypatch):
    """一直抖动 → 抛最后一次的真实错误(供 check_mcp_key 包装成可读提示)。"""
    def _post(self, url, headers=None, json=None):     # noqa: A002
        raise ssl.SSLError(8, "EOF occurred in violation of protocol (_ssl.c:1032)")

    monkeypatch.setattr(httpx.Client, "post", _post)
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    with pytest.raises(ssl.SSLError) as ei:
        _client().check()
    assert "EOF occurred" in str(ei.value)
    # 步骤侧的包装(用户看到的那句话)
    from steps.base import BaseStep, StepContext
    from core.paths import Paths

    cfg = Config({"SS_MCP_URL": "https://mcp.example/mcp", "SS_MCP_KEY": "k", "CAT": "t",
                  "DATA_ROOT": "data", "DRY_RUN": False})

    class _S(BaseStep):
        number, title, description = 99, "t", "t"
        artifacts = []

        def run(self, ctx):  # pragma: no cover
            pass

    ctx = StepContext(cfg, Paths(cfg))
    with pytest.raises(Exception) as ei2:
        _S().check_mcp_key(ctx)
    assert "卖家精灵 MCP 连接失败" in str(ei2.value)
    assert "EOF occurred" in str(ei2.value), "真实原因要保留在提示里(便于判断是网络还是密钥)"


def test_4xx_fails_fast_without_retrying(monkeypatch):
    """4xx(参数/鉴权问题)不重试 —— 重试没意义, 立刻暴露出来。"""
    calls = {"n": 0}

    def _post(self, url, headers=None, json=None):     # noqa: A002
        calls["n"] += 1
        return _Resp(status=401)

    monkeypatch.setattr(httpx.Client, "post", _post)
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    with pytest.raises(httpx.HTTPStatusError):
        _client()._initialize()
    assert calls["n"] == 1, "4xx 不该重试"
