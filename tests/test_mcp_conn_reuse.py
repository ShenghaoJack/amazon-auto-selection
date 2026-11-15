"""回归测试: MCP 连接复用 + 会话内不重复握手(2026-09-15 用户报障"mcp 总是连接不上")。

实测数据(用户本机, 同一端点):
  · 20 次**全新连接**(改动前的做法: 每个请求都新建 httpx.Client) → 1 次
    `[SSL: UNEXPECTED_EOF_WHILE_READING]` 失败(5%), 耗时 86.5s;
  · 20 次**复用同一连接** → 0 失败, 耗时 10.1s(快 8.6 倍)。
根因: 端点解析出 16 个 A 记录(CDN 池), 部分节点会中途 EOF 握手; 而改动前**每个请求都握手一次**,
`_call()` 还在每次工具调用前重新 initialize → 步骤6 有 N 个关键词要 ~2N 次握手,
按 5%/次算 N=20 时"至少失败一次"的概率约 87% —— 于是"总是连接不上"。
"""
import ssl

import httpx
import pytest

from core.config import Config
from core.mcp import MCPClient


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self._payload = payload if payload is not None else {"result": {"tools": [{"name": "google_trend"}]}}
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


def test_http_client_is_reused_across_requests(monkeypatch):
    """多个请求必须复用**同一个** httpx.Client(连接池 + TLS 长连接), 不再每个请求新建。"""
    created: list[int] = []
    real_init = httpx.Client.__init__

    def _init(self, *a, **kw):
        created.append(1)
        return real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", _init)
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, headers=None, json=None: _Resp())
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    c = _client()
    c.check()
    c._post_json({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}})
    c._post_json({"jsonrpc": "2.0", "id": 10, "method": "ping", "params": {}})
    assert len(created) == 1, f"应只建 1 个 httpx.Client, 实际 {len(created)}"
    c.close()


def test_initialize_skipped_when_session_exists(monkeypatch):
    """已有会话(`Mcp-Session-Id`)时不再重复握手 —— 少一半握手就少一半"CDN 节点 EOF"暴露面。"""
    posts: list[dict] = []

    def _post(self, url, headers=None, json=None):     # noqa: A002
        posts.append(json or {})
        if (json or {}).get("method") == "initialize":
            return _Resp(headers={"Mcp-Session-Id": "sid-9"})
        return _Resp(payload={"result": {"data": {"ok": 1}}})

    monkeypatch.setattr(httpx.Client, "post", _post)
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    c = _client()
    c._initialize()
    assert c._session_id == "sid-9"
    c._initialize()                                    # 第二次应直接跳过
    inits = [p for p in posts if p.get("method") == "initialize"]
    assert len(inits) == 1, f"只该握手一次, 实际 {len(inits)}"
    # 后续工具调用也不再握手(会话已建立)
    c._call("google_trend", {"keyword": "x"})
    inits = [p for p in posts if p.get("method") == "initialize"]
    assert len(inits) == 1, "工具调用前不该再握手"


def test_connection_error_resets_client_and_session(monkeypatch):
    """连接类错误后必须**丢弃连接与会话**: 重试时重新握手(而不是复用坏连接/坏会话)。"""
    state = {"n": 0}
    inits: list[int] = []

    def _post(self, url, headers=None, json=None):     # noqa: A002
        state["n"] += 1
        if (json or {}).get("method") == "initialize":
            inits.append(state["n"])
            if len(inits) == 1:
                raise ssl.SSLError(8, "EOF occurred in violation of protocol (_ssl.c:1032)")
            return _Resp(headers={"Mcp-Session-Id": "sid-2"})
        return _Resp(payload={"result": {"data": {"ok": 1}}})

    monkeypatch.setattr(httpx.Client, "post", _post)
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    c = _client()
    c._initialize()                     # 第 1 次握手失败 → 重试成功
    assert c._session_id == "sid-2"
    assert len(inits) == 2, "失败后应重新握手"
    c.close()


def test_call_reraises_business_error_without_conn_reset(monkeypatch):
    """业务错误(如工具执行失败)不是连接问题: 不重试、也不重置连接。"""
    resets: list[str] = []
    monkeypatch.setattr(httpx.Client, "post",
                        lambda self, url, headers=None, json=None: _Resp(
                            payload={"result": {"isError": True,
                                                "content": [{"type": "text", "text": "bad keyword"}]}}))
    monkeypatch.setattr("core.mcp.time.sleep", lambda *_: None)
    c = _client()
    monkeypatch.setattr(c, "_reset_connection", lambda reason="": resets.append(reason))
    with pytest.raises(RuntimeError) as ei:
        c._call("google_trend", {"keyword": "x"})
    assert "执行失败" in str(ei.value)
    assert resets == [], "业务错误不该重置连接"
    c.close()
