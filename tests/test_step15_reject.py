# -*- coding: utf-8 -*-
"""步骤15 选品报告「否决（不推荐进入）」与「继续深挖该类目」全链路测试。"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
from openpyxl import Workbook

from core import step15_review as review
from core.manual_gates import blocking_gate, pending_gates


@pytest.fixture(autouse=True)
def _isolate_standards(tmp_path, monkeypatch):
    from core import step15_settings
    monkeypatch.setattr(step15_settings, "STANDARDS_DIR", tmp_path / "workbench-settings")


def _make_workbook(root: Path) -> None:
    from tools.step15_market_assessment import SPECS
    root.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "选品指标判定"
    ws.append(["板块", "指标", "实际值", "原表标准", "判定", "证据与限制"])
    for section, name, *_ in SPECS:
        ws.append([section, name, 5, ">3", "达标", "样本数据"])
    ws2 = wb.create_sheet("AI市场建议")
    ws2.append(["项目", "内容"])
    ws2.append(["AI建议", "暂不建议进入：双维重度封锁。"])
    wb.save(review.report_path(root))
    wb.close()


def test_reject_blocks_step16_stops_autopopup_and_is_reversible(tmp_path):
    root = tmp_path / "Demo-US-202610"
    _make_workbook(root)
    review.mark_pending(root)
    statuses = {8: "DONE", 9: "DONE", 15: "DONE", 16: "PENDING", 17: "PENDING", 18: "PENDING"}

    snap = review.view(root)
    assert snap["decision"] == "pending" and snap["rejected"] is False
    assert pending_gates(root, statuses)[0]["kind"] == "market_report"

    # 人工点击否决：不再反复自动弹出待审门禁，但硬阻断步骤16及后续，且持久化操作人与时间
    review.reject(root, snap["revision"], actor="test-user")
    assert review.rejected(root) is True
    assert review.decision_status(root) == "rejected"
    assert review.needs_review(root) is False
    assert review.pending(root) is True
    assert pending_gates(root, statuses) == []
    # 即使历史已跑过步骤16，否决后也不再自动弹出任何后续门禁
    assert pending_gates(root, {**statuses, 16: "DONE"}) == []

    gate = blocking_gate(root, statuses, 16)
    assert gate is not None and gate["kind"] == "market_report_rejected" and gate["after"] == 15

    snap2 = review.view(root)
    assert snap2["decision"] == "rejected" and snap2["rejected"] is True
    assert snap2["decided_by"] == "test-user" and snap2["decided_at"]
    assert len(snap2["decision_history"]) == 1
    assert snap2["decision_history"][0]["action"] == "否决（不推荐进入）"

    # 如改主意，可重新确认继续深挖，放行步骤16，并保留完整决策轨迹
    review.confirm(root, snap2["revision"], actor="admin")
    assert review.rejected(root) is False
    assert review.decision_status(root) == "confirmed"
    assert blocking_gate(root, statuses, 16) is None
    snap3 = review.view(root)
    assert snap3["decided_by"] == "admin" and snap3["decided_at"]
    assert [h["status"] for h in snap3["decision_history"]] == ["rejected", "confirmed"]


def test_server_blocks_resume_after_reject_and_http_reject_endpoint(tmp_path, monkeypatch):
    from webui import auth, server

    cat = "Demo-US-202610"
    root = tmp_path / "data" / cat
    _make_workbook(root)
    review.mark_pending(root)
    (root / "config.yaml").write_text(
        f'CAT: "{cat}"\nDATA_ROOT: "{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "auth.sqlite3")
    monkeypatch.setattr(server.RUNNER, "active_for", lambda c: False)
    monkeypatch.setattr(server, "_lock_live_started", lambda *a: (False, None, False))
    monkeypatch.delenv("WEBUI_PUBLIC_ORIGIN", raising=False)

    auth.create_user("admin", "test-password", "admin", actor="test", first=True)
    token = auth.login("admin", "test-password")
    csrf = auth.session(token)["csrf"]

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_port

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {
            "Host": f"127.0.0.1:{port}",
            "Cookie": "wui_session=" + token,
            "X-CSRF-Token": csrf,
        }
        if method == "POST":
            headers.update({"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
        conn.request(method, path, body=json.dumps(body or {}) if method == "POST" else None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        code = resp.status
        conn.close()
        return code, data

    try:
        cfg_file = "data/" + cat + "/config.yaml"
        code, snap = call("GET", "/api/step15-review?file=" + cfg_file)
        assert code == 200 and snap["decision"] == "pending"

        code, res = call("POST", "/api/step15-review/reject", {"config_file": cfg_file, "revision": snap["revision"]})
        assert code == 200 and res["decision"] == "rejected"
        assert review.rejected(root) is True

        # 否决后再调 _api_run_locked(resume) 必须被阻断并提示已人工否决
        handler = server.Handler.__new__(server.Handler)
        monkeypatch.setattr(server, "safe_config_path", lambda _: root / "config.yaml")
        monkeypatch.setattr(handler, "_status_cfg", lambda _: {"CAT": cat, "DATA_ROOT": str(tmp_path / "data")})
        monkeypatch.setattr(handler, "_require_run", lambda _: None)
        monkeypatch.setattr(server, "_hist_status_snapshot", lambda _: ("", {15: {"status": "DONE"}}))
        start = Mock()
        monkeypatch.setattr(server.RUNNER, "start", start)
        with pytest.raises(server.UserFacingError, match="已人工否决"):
            handler._api_run_locked({"config_file": cfg_file, "mode": "resume"})
        start.assert_not_called()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_frontend_button_order_and_labels():
    html = (Path(__file__).resolve().parents[1] / "webui/index.html").read_text(encoding="utf-8")
    idx_pdf = html.index('id="marketReportPdf"')
    idx_rej = html.index('id="marketReportReject"')
    idx_cfm = html.index('id="marketReportConfirm"')
    assert idx_pdf < idx_rej < idx_cfm
    assert "/api/step15-review/reject" in html
    assert "否决（不推荐进入）" in html
    assert "继续深挖该类目" in html
