"""Redirects are charged terminal notices, never empty success or auto-retries."""
import json

import pytest

from core.mcp import MCPBusinessError
from tests.test_voc_asin_fallback import envelope, item, rows, seed, recovery
from tests.test_voc_mcp_recovery import collector
from tools.voc.redirects import ASINRedirected, notice_from_error

MESSAGE = "[ASIN_REDIRECTED] Redirected to ASIN B002D172C3\nThe product redirected. This request was billed normally."


def redirect():
    payload = {"jsonrpc": "2.0", "result": {"isError": True, "content": [{"type": "text", "text": MESSAGE}]}}
    return MCPBusinessError("get_amazon_reviews", "isError", MESSAGE, payload)


def test_redirect_is_detected_and_ordinary_error_is_not():
    notice = notice_from_error(redirect(), "pangolinfo", "OLD", "amz_us")
    assert notice["destination_asin"] == "B002D172C3" and notice["billed_notice"]
    assert notice["billing_amount"] is None
    assert notice_from_error("connection lost", "pangolinfo", "OLD", "amz_us") is None


def test_redirect_blocks_same_old_asin_across_strata_and_restart(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append(args["asin"])
        raise redirect()
    c = collector(tmp_path, monkeypatch, call)
    with pytest.raises(ASINRedirected):
        c.one_call("pangolinfo", "P", "OLD", "critical", "critical", [1, 2, 3], 3)
    with pytest.raises(ASINRedirected):
        c.one_call("pangolinfo", "P", "OLD", "five_star", "five_star", [5], 1)
    assert calls == ["OLD"] and not c.done
    restored = collector(tmp_path, monkeypatch, call)
    assert not restored.done  # Full JSON-RPC isError receipt cannot become an empty successful page.
    with pytest.raises(ASINRedirected):
        restored.one_call("pangolinfo", "P", "OLD", "critical", "critical", [1, 2, 3], 3)
    assert calls == ["OLD"]
    receipts = restored._read_raw(str(tmp_path / "MCP完整调用回执.jsonl"))
    assert receipts[0]["status"] == "redirected" and receipts[0]["raw"]["result"]["isError"]


@pytest.mark.parametrize("destination_in_catalog", [False, True])
def test_quality_redirect_is_marked_without_retry_or_destination_query(tmp_path, monkeypatch, destination_in_catalog):
    calls = []
    def call(source, args):
        calls.append((source, args["asin"]))
        if source == "pangolinfo":
            raise redirect()
        return envelope(source, [item("a")])
    c = collector(tmp_path / "raw", monkeypatch, call)
    seed(c, "sellersprite")
    r = recovery(c, tmp_path / "out")
    if destination_in_catalog:
        r.catalog["products"].append({"parent_asin": "P", "variant_asin": "B002D172C3"})
    r.run(rows(c))
    summary = r.annotate(rows(c))
    task = next(iter(r.tasks.values()))
    assert task["status"] == ("redirected_manual_review" if destination_in_catalog else "redirected_out_of_scope")
    assert task["redirected_asin"] == "B002D172C3"
    assert summary["pending_tasks"] == 0 and summary["redirected_groups"] == 1
    assert calls == [("sellersprite", "P"), ("pangolinfo", "P")]
    assert all(not row["variant_asin"] for row in rows(c))
    resumed = collector(tmp_path / "raw", monkeypatch, call)
    r2 = recovery(resumed, tmp_path / "out")
    r2.run(rows(resumed))
    assert calls == [("sellersprite", "P"), ("pangolinfo", "P")]


def test_old_failed_task_redirect_is_upgraded_without_paid_call(tmp_path, monkeypatch):
    (tmp_path / "ASIN跨源补抓任务.json").write_text(json.dumps({"tasks": [{"other_source": "pangolinfo",
        "queried_asin": "OLD", "error": MESSAGE, "status": "pending"}]}), encoding="utf-8")
    def unexpected(*args):
        pytest.fail("历史已计费重定向不能重新请求")
    c = collector(tmp_path / "1_采集原始数据", monkeypatch, unexpected)
    assert c.redirect_notice("pangolinfo", "OLD")["destination_asin"] == "B002D172C3"
    with pytest.raises(ASINRedirected):
        c.one_call("pangolinfo", "P", "OLD", "critical", "critical", [1], 1)


def test_successful_review_quoting_error_marker_is_not_a_control_signal(tmp_path, monkeypatch):
    def call(source, args):
        return envelope(source, [dict(item("a", "V"), content=MESSAGE)])
    c = collector(tmp_path, monkeypatch, call)
    receipt = c.one_call("pangolinfo", "P", "P", "critical", "critical", [1], 1)
    assert receipt["status"] == "ok" and not c.redirected_asins


def test_business_error_envelope_also_stays_terminal(tmp_path, monkeypatch):
    calls = []
    c = collector(tmp_path, monkeypatch, lambda s, a: calls.append(a["asin"]) or {"code": "isError", "message": MESSAGE})
    for _ in range(2):
        with pytest.raises(ASINRedirected):
            c.one_call("pangolinfo", "P", "OLD", "critical", "critical", [1], 1)
    assert calls == ["OLD"]
    assert not collector(tmp_path, monkeypatch, lambda *a: None).done
