# -*- coding: utf-8 -*-
"""回归测试: 404 终态判定、新版无头浏览器指纹启动、步骤10 直连熔断与单会话原地恢复。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core import browser as browser_mod
from core.amazon_variation import parse_variations
from core.config import Config
from core.io_utils import read_jsonl
from core.paths import Paths
from steps.base import StepContext
from steps.step10_variants import Step10Variants


def _valid_product_html(asin: str = "B0TEST1234", market: str = "amazon.com") -> str:
    pad = "x" * 52_000
    return (
        f"<html><head><title>Sample Product : {market}</title></head>"
        f"<body><span id='productTitle'>Sample Product</span>"
        f"<script>var data = {{\"parentAsin\":\"{asin}\",\"dimensionToAsinMap\":{{\"0\":\"{asin}\"}}}};</script>"
        f"{pad}</body></html>"
    )


def _page_not_found_html() -> str:
    return (
        "<!doctype html><html><head><title>Page Not Found</title></head>"
        "<body>Sorry! We couldn't find that page."
        "<!-- To discuss automated access to Amazon data please contact "
        "api-services-support@amazon.com. --></body></html>"
    )


def _captcha_html() -> str:
    return (
        "<!doctype html><html><head><title>Amazon.com</title></head>"
        "<body>Enter the characters you see below validateCaptcha "
        "api-services-support@amazon.com</body></html>"
    )


def _signin_html() -> str:
    return (
        "<!doctype html><html><head><title>Amazon.com</title></head>"
        "<body>Please continue /ap/signin</body></html>"
    )


def _make_ctx(tmp_path: Path, overrides: dict | None = None) -> StepContext:
    data = {
        "CAT": "TestCat-US",
        "SITE": "US",
        "MARKET": "www.amazon.com",
        "DATA_ROOT": str(tmp_path / "data"),
        "DRY_RUN": False,
        "FALLBACK": "auto",
        "FETCH_WORKERS": 1,
        "FETCH_LONG_EVERY": 0,
        "REQUEST_MIN_INTERVAL": 0,
        "REQUEST_MAX_INTERVAL": 0,
        "FETCH_ROUND_PAUSE": 0,
        "BROWSER_ROUND_PAUSE": 0,
        "BROWSER_CHECKPOINT_WAIT": 0,
        "BROWSER_CHECKPOINT_GRACE": 0,
    }
    if overrides:
        data.update(overrides)
    cfg = Config(data)
    paths = Paths(cfg)
    paths.cat_root.mkdir(parents=True, exist_ok=True)
    return StepContext(cfg, paths)


# ---- 1. core/amazon_variation.py ----

def test_parse_variations_page_not_found_beats_footer_marker():
    res = parse_variations(_page_not_found_html(), "B0TEST0001", market="www.amazon.com")
    assert res["status"] == "page_not_found"
    assert res["title"] == "Page Not Found"


def test_parse_variations_real_captcha_remains_blocked():
    res = parse_variations(_captcha_html(), "B0TEST0001", market="www.amazon.com")
    assert res["status"] == "blocked"


def test_page_evidence_classifies_404_and_checkpoint_signin():
    ev_404 = Step10Variants._page_evidence(_page_not_found_html(), "https://www.amazon.com/dp/B0TEST0001")
    assert ev_404["blocked_kind"] == "page_not_found"

    fake_page = SimpleNamespace(url="https://www.amazon.com/checkpoint/continue?token=abc")
    ev_ckpt = Step10Variants._page_evidence("<html><title>Amazon.com</title><body>hi</body></html>",
                                            "https://www.amazon.com/dp/B0TEST1234", fake_page)
    assert ev_ckpt["blocked_kind"] == "signin"

    ev_cap = Step10Variants._page_evidence(_captcha_html(), "https://www.amazon.com/dp/B0TEST1234")
    assert ev_cap["blocked_kind"] == "captcha"


# ---- 2. core/browser.py ----

@pytest.mark.parametrize("market,expected", [
    ("www.amazon.com", ("en-US", "America/Los_Angeles")),
    ("amazon.co.uk", ("en-GB", "Europe/London")),
    ("www.amazon.de", ("de-DE", "Europe/Berlin")),
    ("www.amazon.fr", ("fr-FR", "Europe/Paris")),
    ("www.amazon.co.jp", ("ja-JP", "Asia/Tokyo")),
    ("unknown.example", ("en-US", "America/Los_Angeles")),
])
def test_market_fingerprint(market, expected):
    assert browser_mod._market_fingerprint(market) == expected


def test_browser_automation_new_headless_launch_and_fallback(tmp_path):
    cfg = Config({"MARKET": "www.amazon.co.uk", "DRY_RUN": False})
    fake_ctx = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context.return_value = fake_ctx
    fake_pw_instance = SimpleNamespace(chromium=fake_chromium, stop=MagicMock())
    fake_sync_pw = MagicMock(return_value=SimpleNamespace(start=lambda: fake_pw_instance))

    with (
        patch.object(browser_mod, "_import_playwright", return_value=fake_sync_pw),
        patch.object(browser_mod, "_detect_clean_chrome_ua", return_value="Mozilla/5.0 Chrome/157.0.0.0"),
    ):
        with browser_mod.BrowserAutomation(cfg, str(tmp_path / "prof"), headed=False):
            pass

    kw = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kw["channel"] == "chrome"
    assert kw["headless"] is False
    assert "--headless=new" in kw["args"]
    assert kw["user_agent"] == "Mozilla/5.0 Chrome/157.0.0.0"
    assert kw["locale"] == "en-GB"
    assert kw["timezone_id"] == "Europe/London"
    assert kw["viewport"] == {"width": 1366, "height": 768}

    # Test fallback to bundled Chromium when channel="chrome" fails
    fake_chromium.launch_persistent_context.reset_mock()
    fake_chromium.launch_persistent_context.side_effect = [RuntimeError("no chrome"), fake_ctx]
    with (
        patch.object(browser_mod, "_import_playwright", return_value=fake_sync_pw),
        patch.object(browser_mod, "_detect_clean_chrome_ua", return_value=None),
    ):
        with browser_mod.BrowserAutomation(cfg, str(tmp_path / "prof2"), headed=False):
            pass

    assert fake_chromium.launch_persistent_context.call_count == 2
    fb_kw = fake_chromium.launch_persistent_context.call_args_list[1].kwargs
    assert "channel" not in fb_kw
    assert fb_kw["headless"] is True
    assert fb_kw["locale"] == "en-GB"


def test_browser_automation_headed_preserves_legacy_launch(tmp_path):
    cfg = Config({"MARKET": "www.amazon.com", "DRY_RUN": False})
    fake_ctx = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context.return_value = fake_ctx
    fake_pw_instance = SimpleNamespace(chromium=fake_chromium, stop=MagicMock())
    fake_sync_pw = MagicMock(return_value=SimpleNamespace(start=lambda: fake_pw_instance))

    with patch.object(browser_mod, "_import_playwright", return_value=fake_sync_pw):
        with browser_mod.BrowserAutomation(cfg, str(tmp_path / "prof"), headed=True):
            pass

    kw = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kw["headless"] is False
    assert "channel" not in kw
    assert "--headless=new" not in kw["args"]


# ---- 3. steps/step10_variants.py: breaker + browser recovery ----

def test_direct_sliding_window_breaker_trips_and_skips_direct_retry(tmp_path):
    ctx = _make_ctx(tmp_path, {
        "DIRECT_BREAKER_WINDOW": 5,
        "DIRECT_BREAKER_MIN_RATE": 0.40,
        "FETCH_STALL_ROUNDS": 2,
        "BROWSER_FALLBACK_ROUNDS": 1,
        "BROWSER_HEADED_ROUNDS": 0,
    })
    step = Step10Variants()
    asins = [f"B0TEST{i:04d}" for i in range(12)]
    results_path = str(tmp_path / "results.jsonl")
    direct_called: list[str] = []
    browser_called: list[list[str]] = []

    def fake_fetch_one(_ctx, _http, asin, attempts=1):
        direct_called.append(asin)
        # First 1 ok, next 4 fail -> 1/5 = 20% < 40% -> trips at #5!
        if len(direct_called) == 1:
            return {"request_asin": asin, "ok": True, "status": "ok", "source": "direct_http"}
        return step._failed_record(asin, "failed", step.now())

    def fake_browser_fallback(_ctx, leftover, *, headed=False, results_path=None):
        browser_called.append(list(leftover))
        return [{"request_asin": a, "ok": True, "status": "ok", "source": "local_chrome"} for a in leftover]

    with (
        patch.object(step, "_fetch_one", side_effect=fake_fetch_one),
        patch.object(step, "_browser_fallback", side_effect=fake_browser_fallback),
    ):
        recs = step._fetch_all(ctx, asins, results_path)

    # Stopped direct fetch right at window=5 instead of grinding all 12 or retrying round 2
    assert len(direct_called) == 5
    # All remaining 11 ASINs (4 failed + 7 skipped by breaker) went directly to headless fallback
    assert len(browser_called) == 1
    assert len(browser_called[0]) == 11
    assert sum(1 for r in recs if r.get("ok")) == 12


def test_direct_breaker_ignores_terminal_404_and_respects_fallback_off(tmp_path):
    # Terminal 404s should NOT trip the breaker
    ctx = _make_ctx(tmp_path, {"DIRECT_BREAKER_WINDOW": 3, "DIRECT_BREAKER_MIN_RATE": 0.5})
    step = Step10Variants()
    asins = [f"B0DEAD{i:04d}" for i in range(6)]
    results_path = str(tmp_path / "r1.jsonl")
    calls = []

    def fake_404(_ctx, _http, asin, attempts=1):
        calls.append(asin)
        return step._failed_record(asin, "skipped_page_not_found", step.now())

    with patch.object(step, "_fetch_one", side_effect=fake_404):
        step._fetch_round(ctx, asins, results_path)
    assert len(calls) == 6
    assert step._last_round_tripped is False

    # FALLBACK=off disables the breaker even when all fail
    ctx_off = _make_ctx(tmp_path, {
        "FALLBACK": "off",
        "DIRECT_BREAKER_WINDOW": 3,
        "DIRECT_BREAKER_MIN_RATE": 0.5,
    })
    calls_off = []

    def fake_fail(_ctx, _http, asin, attempts=1):
        calls_off.append(asin)
        return step._failed_record(asin, "failed", step.now())

    with patch.object(step, "_fetch_one", side_effect=fake_fail):
        step._fetch_round(ctx_off, asins, str(tmp_path / "r2.jsonl"))
    assert len(calls_off) == 6
    assert step._last_round_tripped is False


def test_browser_fallback_inplace_recovery_and_incremental_flush(tmp_path):
    ctx = _make_ctx(tmp_path, {
        "FETCH_LONG_EVERY": 2,
        "FETCH_LONG_MIN": 0,
        "FETCH_LONG_MAX": 0,
        "BROWSER_CHECKPOINT_WAIT": 0,
        "BROWSER_CHECKPOINT_GRACE": 0,
    })
    step = Step10Variants()
    results_path = str(tmp_path / "br.jsonl")
    asins = ["B0CAP00001", "B0SIG00002", "B0PNF00003"]
    # Seed old failed rows as _fetch_all would
    Path(results_path).write_text(
        "".join(json.dumps(step._failed_record(a, "failed", step.now())) + "\n" for a in asins),
        encoding="utf-8",
    )

    responses = {
        # 1st try captcha, 2nd try ok
        "B0CAP00001": [_captcha_html(), _valid_product_html("B0CAP00001")],
        # 3x signin (triggers checkpoint wait), 4th try ok
        "B0SIG00002": [_signin_html(), _signin_html(), _signin_html(), _valid_product_html("B0SIG00002")],
        # 404 Page Not Found -> immediate terminal skipped_page_not_found (no retry!)
        "B0PNF00003": [_page_not_found_html()],
    }
    hit_counts = {a: 0 for a in asins}

    class FakePage:
        def __init__(self):
            self.url = ""
            self.mouse = SimpleNamespace(wheel=lambda x, y: None)
            self._cur_asin = ""

        def content(self):
            a = self._cur_asin
            idx = hit_counts[a] - 1
            return responses[a][min(idx, len(responses[a]) - 1)]

    class FakeBrowser:
        def __init__(self):
            self.page = FakePage()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def new_page(self):
            return self.page

        def goto(self, page, url):
            page.url = url
            asin = url.split("/dp/")[1].split("?")[0]
            page._cur_asin = asin
            hit_counts[asin] += 1

    with (
        patch.object(ctx, "browser", return_value=FakeBrowser()),
        patch("steps.step10_variants.time.sleep", return_value=None),
    ):
        recs = step._browser_fallback(ctx, asins, results_path=results_path)
        step._apply_browser_round(ctx, results_path, recs, "test")

    by_asin = {r["request_asin"]: r for r in read_jsonl(results_path)}
    assert len(by_asin) == 3
    assert by_asin["B0CAP00001"]["ok"] is True
    assert hit_counts["B0CAP00001"] == 2
    assert by_asin["B0SIG00002"]["ok"] is True
    assert hit_counts["B0SIG00002"] == 4
    assert by_asin["B0PNF00003"]["status"] == "skipped_page_not_found"
    assert hit_counts["B0PNF00003"] == 1


def test_browser_fallback_stops_early_after_five_unrecovered_signins(tmp_path):
    ctx = _make_ctx(tmp_path, {
        "BROWSER_CHECKPOINT_WAIT": 0,
        "BROWSER_CHECKPOINT_GRACE": 0,
    })
    step = Step10Variants()
    asins = [f"B0SIGN{i:04d}" for i in range(8)]
    visited: list[str] = []

    class AlwaysSigninPage:
        url = "https://www.amazon.com/checkpoint/continue?token=xyz"
        mouse = SimpleNamespace(wheel=lambda x, y: None)

        def content(self):
            return _signin_html()

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def new_page(self):
            return AlwaysSigninPage()

        def goto(self, page, url):
            visited.append(url.split("/dp/")[1].split("?")[0])

    with (
        patch.object(ctx, "browser", return_value=FakeBrowser()),
        patch("steps.step10_variants.time.sleep", return_value=None),
    ):
        recs = step._browser_fallback(ctx, asins)

    assert len(recs) == 8
    # Only the first 5 ASINs were actually navigated; the remaining 3 were circuit-broken
    assert len(set(visited)) == 5
    assert all(not r["ok"] and r.get("blocked_kind") == "signin" for r in recs)

