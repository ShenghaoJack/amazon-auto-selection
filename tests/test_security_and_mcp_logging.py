"""Unit tests for WebUI security access logging (401/403/404 + login IP) and MCP call logging."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest


def test_webui_security_log_records_401_403_404_with_real_ip(tmp_path, monkeypatch):
    from webui import server as srv

    log_file = tmp_path / "webui_security.log"
    monkeypatch.setenv("WEBUI_SECURITY_LOG_PATH", str(log_file))

    h = srv.Handler.__new__(srv.Handler)
    h.command = "GET"
    h.path = "/api/admin/secret?foo=1"
    h.client_address = ("10.0.0.5", 54321)
    h.headers = {
        "Host": "example.com",
        "X-Forwarded-For": "203.0.113.42, 10.0.0.1",
        "User-Agent": "ScannerBot/1.0",
    }
    h._user = {}

    h._log_security_event(200, "ok")  # should NOT be logged
    h._log_security_event(401, "请先登录")
    h._log_security_event(404, "未知接口: /api/admin/secret")

    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["status"] == 401
    assert lines[0]["ip"] == "203.0.113.42"
    assert lines[0]["path"] == "/api/admin/secret"
    assert lines[0]["ua"] == "ScannerBot/1.0"
    assert lines[0]["reason"] == "请先登录"
    assert lines[1]["status"] == 404


def test_auth_login_records_client_ip_in_audit(tmp_path, monkeypatch):
    from webui import auth

    db_file = tmp_path / "auth.sqlite3"
    monkeypatch.setattr(auth, "DB_PATH", db_file)
    auth.create_user("tester", "StrongPass!2026", "operator", actor="init")

    # Failed login with IP
    assert auth.login("tester", "wrong-password", client_ip="198.51.100.7") is None
    # Successful login with IP
    tok = auth.login("tester", "StrongPass!2026", client_ip="198.51.100.7")
    assert tok

    items = auth.list_audit(limit=10, actor="tester")
    actions = {it["action"]: it["detail"] for it in items}
    assert actions.get("login_failed") == "ip=198.51.100.7"
    assert actions.get("login_success") == "ip=198.51.100.7"


def test_mcp_call_logger_records_success_and_failure_without_leaking_key(tmp_path, monkeypatch):
    from core.config import Config
    from core.mcp import MCPBusinessError, MCPClient

    log_file = tmp_path / "mcp_calls.log"
    monkeypatch.setenv("PIPELINE_MCP_LOG_PATH", str(log_file))

    secret = "TOP_SECRET_MCP_KEY_987654"
    cfg = Config({
        "CAT": "TestCat-US-202611",
        "SS_MCP_URL": "https://mcp.sellersprite.com/v2/mcp",
        "SS_MCP_KEY": secret,
        "DRY_RUN": False,
    })
    client = MCPClient(cfg)
    client._session_id = "sid-1"

    class _FakeResp:
        def __init__(self, payload: dict, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.headers = {"content-type": "application/json"}
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    responses = iter([
        _FakeResp({"result": {"content": [{"type": "text", "text": json.dumps({"code": "OK", "data": {"n": 1}})}]}}),
        _FakeResp({"result": {"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]}}),
    ])
    client._client = SimpleNamespace(post=lambda *a, **k: next(responses), close=lambda: None)

    out = client._call("keyword.batch.info", {"keywords": ["a"]}, retries=1)
    assert out == {"n": 1}

    with pytest.raises(MCPBusinessError):
        client._call("asin.list", {"asins": ["B000000001"]}, retries=1)

    raw_text = log_file.read_text(encoding="utf-8")
    assert secret not in raw_text
    entries = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    assert len(entries) == 2
    assert entries[0]["tool"] == "keyword.batch.info"
    assert entries[0]["ok"] is True
    assert entries[0]["cat"] == "TestCat-US-202611"
    assert entries[0]["status_code"] == 200
    assert entries[1]["tool"] == "asin.list"
    assert entries[1]["ok"] is False
    assert "quota exceeded" in entries[1]["error"]
