# -*- coding: utf-8 -*-
"""回归测试: 4 项性能优化 (HTTP Session复用+404速停 / 步骤1单会话+熔断 / 步骤5-6复用+真断点 / 步骤16并发抽取)。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core import http_client
from core.config import Config
from core.io_utils import write_json
from core.paths import Paths
from steps.base import StepContext
from steps.step01_details import Step01Details
from steps.step05_keyword_data import Step05KeywordData
from steps.step06_trend import Step06Trend
from steps.step10_variants import Step10Variants
from steps.step16_attribute_annotation import Step16AttributeAnnotation


def _ctx(tmp_path: Path, **overrides) -> StepContext:
    data = {
        "CAT": "PerfCat-US",
        "SITE": "US",
        "MARKET": "www.amazon.com",
        "DATA_ROOT": str(tmp_path / "data"),
        "TREND_MONTH": "2026-10",
        "DRY_RUN": False,
        "REQUEST_MIN_INTERVAL": 0,
        "REQUEST_MAX_INTERVAL": 0,
        "FETCH_LONG_EVERY": 0,
        "BROWSER_CHECKPOINT_WAIT": 0,
    }
    data.update(overrides)
    cfg = Config(data)
    paths = Paths(cfg)
    paths.cat_root.mkdir(parents=True, exist_ok=True)
    return StepContext(cfg, paths)


# ---- 1. core/http_client.py + Step10 per-thread HttpFetcher reuse ----

def test_http_fetcher_reuses_session_and_stops_immediately_on_404(tmp_path, monkeypatch):
    created = []
    closed = []
    calls = []

    class FakeSession:
        def __init__(self, impersonate="chrome"):
            created.append(self)

        def get(self, url, **kw):
            calls.append(url)
            if "DEAD" in url:
                return SimpleNamespace(status_code=404, text="<title>Page Not Found</title>", url=url)
            if "SOFT404" in url:
                return SimpleNamespace(
                    status_code=200,
                    text="<title>Page Not Found</title><!-- automated access -->",
                    url=url,
                )
            if "BOOM" in url and len(calls) == 4:
                raise OSError("connection reset")
            return SimpleNamespace(
                status_code=200,
                text='<span id="productTitle">Valid Title</span>',
                url=url,
            )

        def close(self):
            closed.append(self)

    monkeypatch.setattr(http_client.cffi_requests, "Session", FakeSession)
    monkeypatch.setattr(http_client.time, "sleep", lambda *_: None)

    cfg = Config({"DRY_RUN": False, "REQUEST_MIN_INTERVAL": 0, "REQUEST_MAX_INTERVAL": 0, "REQUEST_RETRY": 3})
    fetcher = http_client.HttpFetcher(cfg)

    r1 = fetcher.get("https://www.amazon.com/dp/B0OK000001")
    r2 = fetcher.get("https://www.amazon.com/dp/B0DEAD0002")
    r3 = fetcher.get("https://www.amazon.com/dp/B0SOFT4043")
    assert r1.ok is True
    assert r2.status_class == "not_found"
    assert r3.status_class == "not_found"
    assert len(calls) == 3
    assert len(created) == 1

    r4 = fetcher.get("https://www.amazon.com/dp/B0BOOM0004")
    assert r4.ok is True
    assert len(created) == 2
    assert len(closed) == 1
    fetcher.close()
    assert len(closed) == 2


def test_step10_fetch_round_reuses_single_http_fetcher_per_thread(tmp_path):
    ctx = _ctx(tmp_path, FETCH_WORKERS=1, FALLBACK="off")
    step = Step10Variants()
    asins = [f"B0REUSE{i:03d}" for i in range(5)]
    results_path = str(tmp_path / "results.jsonl")
    created_fetchers = []
    seen_fetchers = []

    class SpyFetcher:
        def __init__(self, *a, **k):
            self.closed = False
            created_fetchers.append(self)

        def close(self):
            self.closed = True

    def fake_fetch_one(_ctx, http, asin, attempts=1):
        seen_fetchers.append(http)
        return {"request_asin": asin, "ok": True, "status": "ok", "source": "direct_http"}

    with (
        patch("steps.step10_variants.HttpFetcher", SpyFetcher),
        patch.object(step, "_fetch_one", side_effect=fake_fetch_one),
    ):
        step._fetch_round(ctx, asins, results_path)

    assert len(created_fetchers) == 1
    assert len(seen_fetchers) == 5
    assert all(h is created_fetchers[0] for h in seen_fetchers)
    assert created_fetchers[0].closed is True


# ---- 2. steps/step01_details.py: shared browser session + 404 stop + direct breaker ----

def test_step01_reuses_single_browser_session_and_trips_direct_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
    ctx = _ctx(tmp_path, STEP01_DIRECT_BREAKER_WINDOW=3, STEP01_DIRECT_BREAKER_MIN_RATE=0.40)
    asins = [f"B0S100000{i}" for i in range(6)]
    write_json(
        ctx.resolve_artifact(f"{ctx.config.cat}-bestseller-newrelease.json"),
        {"bs": asins, "nr": [], "union": asins, "processed_urls": []},
    )

    http_calls = []

    class BlockedHttp:
        def fetch_product_page(self, asin):
            http_calls.append(asin)
            return SimpleNamespace(
                request_asin=asin, ok=False, status="bot_blocked",
                fetched_at="2026-11-22 00:00:00", title="", bullets=[],
            )

    browser_enters = 0
    browser_exits = 0
    navigations = []

    class FakePage:
        def __init__(self):
            self.url = ""
            self.mouse = SimpleNamespace(wheel=lambda x, y: None)

        def content(self):
            if "B0S1000004" in self.url:
                return "<title>Page Not Found</title><!-- automated access -->"
            return '<span id="productTitle">Browser Title</span><div id="feature-bullets"><ul><li>Bullet 1</li></ul></div>'

        def close(self):
            pass

    class FakeBrowser:
        def __enter__(self):
            nonlocal browser_enters
            browser_enters += 1
            return self

        def __exit__(self, *a):
            nonlocal browser_exits
            browser_exits += 1

        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            page.url = url
            navigations.append(url)

    monkeypatch.setattr(ctx, "http", lambda: BlockedHttp())
    monkeypatch.setattr(ctx, "browser", lambda site="amazon", **kw: FakeBrowser())
    monkeypatch.setattr("steps.step01_details.time.sleep", lambda *_: None)

    step = Step01Details()
    step.validate_upstream(ctx)
    step.run(ctx)

    assert len(http_calls) == 3
    assert browser_enters == 1 and browser_exits == 1
    assert len(navigations) == 6
    rows = [json.loads(ln) for ln in Path(ctx.resolve_artifact(step.artifacts[0])).read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_asin = {r["request_asin"]: r for r in rows}
    assert by_asin["B0S1000004"]["status"] == "not_found"
    assert sum(1 for r in rows if r["ok"]) == 5


# ---- 3. steps/step05_keyword_data.py & steps/step06_trend.py ----

def test_step05_saves_incrementally_and_step06_reuses_monthly_series(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, DRY_RUN=True, TREND_MONTH="2026-10")
    cat = ctx.config.cat
    (Path(ctx.paths.cat_root) / f"{cat}-keywords-candidates.txt").write_text("cat feeder\npet bowl\n", encoding="utf-8")
    (Path(ctx.paths.cat_root) / f"{cat}-keywords.txt").write_text("[核心大词]\ncat feeder\npet bowl\n", encoding="utf-8")

    step5 = Step05KeywordData()
    step5.validate_upstream(ctx)
    orig_mine = step5._mine_one
    seen = []

    def crash_on_second(c, kw, tm):
        seen.append(kw)
        if len(seen) == 2:
            raise RuntimeError("simulated mid-run crash")
        return orig_mine(c, kw, tm)

    monkeypatch.setattr(step5, "_mine_one", crash_on_second)
    with pytest.raises(RuntimeError, match="simulated mid-run crash"):
        step5.run(ctx)

    partial = json.loads(Path(ctx.resolve_artifact(step5.artifacts[0])).read_text(encoding="utf-8"))
    assert [r["keyword"] for r in partial] == ["cat feeder"]
    assert partial[0].get("station_monthly_series")

    monkeypatch.setattr(step5, "_mine_one", orig_mine)
    step5.run(ctx)
    full = json.loads(Path(ctx.resolve_artifact(step5.artifacts[0])).read_text(encoding="utf-8"))
    assert [r["keyword"] for r in full] == ["cat feeder", "pet bowl"]
    assert all(r.get("station_monthly_series") for r in full)

    ctx_live = _ctx(tmp_path, DRY_RUN=False, TREND_MONTH="2026-10")
    mcp_calls = []
    cooldown_called = []

    class FakeMCP:
        def check(self):
            return ["keyword.trend"]

        def keyword_trend(self, kw, tm):
            mcp_calls.append((kw, tm))
            return {"month": tm, "monthly_series": [{"month": "2026-10", "search_volume": 999}]}

    monkeypatch.setattr(ctx_live, "mcp", lambda: FakeMCP())
    step6 = Step06Trend()
    monkeypatch.setattr(step6, "_cooldown_after_step5", lambda c: cooldown_called.append(True))
    step6.validate_upstream(ctx_live)
    step6.run(ctx_live)

    assert mcp_calls == []
    assert cooldown_called == []
    monthly = json.loads(Path(ctx_live.resolve_artifact(step6.artifacts[0])).read_text(encoding="utf-8"))
    assert list(monthly.keys()) == ["cat feeder", "pet bowl"]

    (Path(ctx_live.paths.cat_root) / f"{cat}-keywords.txt").write_text(
        "[核心大词]\ncat feeder\npet bowl\nnew kw\n", encoding="utf-8"
    )
    step6.run(ctx_live)
    assert mcp_calls == [("new kw", "2026-10")]
    assert len(cooldown_called) == 1
    monthly2 = json.loads(Path(ctx_live.resolve_artifact(step6.artifacts[0])).read_text(encoding="utf-8"))
    assert list(monthly2.keys()) == ["cat feeder", "pet bowl", "new kw"]


# ---- 4. steps/step16_attribute_annotation.py: concurrent extraction ----

def test_step16_price_band_extraction_runs_concurrently_and_deterministically(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, DRY_RUN=True, STEP16_EXTRACT_WORKERS=4, ATTR_MERGE_MODE="none")
    df = pd.DataFrame([
        {
            "ASIN": f"B0ATTR{i:04d}",
            "标题": f"Auto Cat Feeder Model {i} Stainless Steel",
            "五点描述": "WiFi app control; 4L capacity; dual power supply",
            "价格": 20.0 + i * 5.0,
        }
        for i in range(40)
    ])
    step16 = Step16AttributeAnnotation()
    step16._cat = ctx.config.cat

    thread_ids: set[int] = set()
    active_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_extract(_ctx, sub, round_max=25):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
            thread_ids.add(threading.get_ident())
        time.sleep(0.05)
        with active_lock:
            active -= 1
        return ["食碗材质", "容量", "是否支持WiFi"], {
            "食碗材质": "食碗材质(不锈钢/塑料等)",
            "容量": "容量(储粮仓容积)",
            "是否支持WiFi": "是否支持WiFi(是否联网)",
        }

    merge_lock = threading.Lock()
    merge_active = 0
    merge_max_active = 0

    def fake_global_merge(llm, names, **kw):
        nonlocal merge_active, merge_max_active
        with merge_lock:
            merge_active += 1
            merge_max_active = max(merge_max_active, merge_active)
        time.sleep(0.04)
        with merge_lock:
            merge_active -= 1
        return {n: n for n in names}

    monkeypatch.setattr(step16, "_extract_attrs_retry", fake_extract)
    monkeypatch.setattr("tools.attr_stability.global_merge.global_merge", fake_global_merge)
    monkeypatch.setattr("tools.attr_stability.global_merge.global_merge_vote", fake_global_merge)
    monkeypatch.setattr(
        "tools.attr_stability.final_reconcile.reconcile_final_attributes",
        lambda attrs, llm, log=None: (attrs, []),
    )
    monkeypatch.setattr(step16, "_backfill_band_data", lambda *a, **k: {})
    monkeypatch.setattr(step16, "_keyword_targeted_check", lambda *a, **k: [])

    attrs = step16._extract_spec_attrs_by_price_band(ctx, df)
    assert max_active >= 2, f"应并发执行多个抽取任务, 实际最大并发={max_active}"
    assert merge_max_active >= 2, f"应并发执行多段归并任务, 实际最大并发={merge_max_active}"
    assert len(thread_ids) >= 2
    names = {a["name"] for a in attrs}
    assert {"食碗材质", "容量"}.issubset(names)
    assert any("WiFi" in n for n in names)

    # 校验并发结果与单线程结果完全一致(确定性)
    ctx_seq = _ctx(tmp_path / "seq", DRY_RUN=True, STEP16_EXTRACT_WORKERS=1, ATTR_MERGE_MODE="none")
    step16_seq = Step16AttributeAnnotation()
    step16_seq._cat = ctx_seq.config.cat
    monkeypatch.setattr(step16_seq, "_extract_attrs_retry", fake_extract)
    monkeypatch.setattr(step16_seq, "_backfill_band_data", lambda *a, **k: {})
    monkeypatch.setattr(step16_seq, "_keyword_targeted_check", lambda *a, **k: [])
    attrs_seq = step16_seq._extract_spec_attrs_by_price_band(ctx_seq, df)
    assert [(a["name"], a.get("runs")) for a in attrs] == [
        (a["name"], a.get("runs")) for a in attrs_seq
    ]


# ---- 5. core/amz_crawl_lock.py + step10 consecutive captcha stepped cooldown ----

def test_amz_crawl_slot_limits_cross_category_concurrency(tmp_path, monkeypatch):
    from core.amz_crawl_lock import AmzCrawlSlot

    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
    active = 0
    max_active = 0
    lock = threading.Lock()
    logs = []

    def worker(tag):
        nonlocal active, max_active
        with AmzCrawlSlot(
            max_concurrent=1,
            wait_seconds=10,
            label=tag,
            log=logs.append,
            poll=0.05,
        ):
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.18)
            with lock:
                active -= 1

    t1 = threading.Thread(target=worker, args=("CatA",))
    t2 = threading.Thread(target=worker, args=("CatB",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert max_active == 1
    assert any("排队等待中" in m for m in logs)


def test_step10_browser_fallback_captcha_stepped_cooldown_and_early_stop(tmp_path, monkeypatch):
    from contextlib import nullcontext
    import steps.step10_variants as s10mod

    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
    ctx = _ctx(
        tmp_path,
        DRY_RUN=False,
        BROWSER_CAPTCHA_COOLDOWNS="45,90,180",
        BROWSER_CAPTCHA_MAX_STREAK=5,
        FETCH_LONG_EVERY=0,
    )
    step = Step10Variants()
    step._cat = ctx.config.cat

    class DummyBrowser:
        def new_page(self):
            return object()

        def goto(self, page, url):
            return None

    monkeypatch.setattr(ctx, "browser", lambda *a, **k: nullcontext(DummyBrowser()))
    sleeps = []
    monkeypatch.setattr(s10mod.time, "sleep", lambda sec: sleeps.append(float(sec)))

    # 连续 5 次 captcha 后触发提前熔断(第 2/3/4 次分别阶梯冷却 45/90/180s; 第 6/7 个跳过)
    def fake_fetch_one(_ctx, browser, page, asin, now, **kw):
        rec = step._failed_record(asin, "failed", now)
        rec["blocked_kind"] = "captcha"
        return rec

    monkeypatch.setattr(step, "_browser_fetch_one", fake_fetch_one)
    asins = [f"B0CAP{i:05d}" for i in range(7)]
    out = step._browser_fallback(ctx, asins)
    assert len(out) == 7
    assert [s for s in sleeps if s >= 45.0] == [45.0, 90.0, 180.0]
    assert out[5]["error"] == "连续 captcha 限流熔断跳过, 交由后续兜底"
    assert out[6]["error"] == "连续 captcha 限流熔断跳过, 交由后续兜底"


def test_amz_crawl_slot_fifo_and_short_batch_priority_and_stale_cleanup(tmp_path, monkeypatch):
    from core.amz_crawl_lock import AmzCrawlSlot, PRIORITY_NORMAL, PRIORITY_SHORT, _queue_dir

    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
    # 写入一个死进程僵尸票据，验证自动清理不阻塞队列
    qdir = _queue_dir()
    qdir.mkdir(parents=True, exist_ok=True)
    stale = qdir / "q-00-00000000000000000001-99999999-000001-deadbeef.ticket"
    stale.write_text('{"pid": 99999999, "label": "dead"}', encoding="utf-8")

    order = []
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def holder():
        with AmzCrawlSlot(max_concurrent=1, wait_seconds=10, label="Holder", poll=0.05):
            holder_entered.set()
            release_holder.wait(5)

    def waiter(name, prio):
        with AmzCrawlSlot(max_concurrent=1, wait_seconds=10, label=name, poll=0.05, priority=prio, remaining=20 if prio == PRIORITY_SHORT else 500):
            order.append(name)
            time.sleep(0.08)

    th = threading.Thread(target=holder)
    th.start()
    assert holder_entered.wait(3)

    # 先排两个普通任务 A -> B，再排一个小批优先收尾任务 Short(prio=0)
    ta = threading.Thread(target=waiter, args=("NormalA", PRIORITY_NORMAL))
    ta.start()
    time.sleep(0.08)
    tb = threading.Thread(target=waiter, args=("NormalB", PRIORITY_NORMAL))
    tb.start()
    time.sleep(0.08)
    ts = threading.Thread(target=waiter, args=("ShortFinish", PRIORITY_SHORT))
    ts.start()
    time.sleep(0.12)

    release_holder.set()
    for t in (th, ta, tb, ts):
        t.join(timeout=10)

    assert not stale.exists()
    assert order == ["ShortFinish", "NormalA", "NormalB"]


def test_step10_browser_fallback_time_slice_yields_only_when_waiters_present(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from core.amz_crawl_lock import AmzCrawlSlot, PRIORITY_SHORT

    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
    ctx = _ctx(
        tmp_path,
        DRY_RUN=False,
        FETCH_LONG_EVERY=0,
        AMZ_CRAWL_SLICE_SIZE=3,
        AMZ_CRAWL_YIELD_PAUSE=0.05,
        AMZ_CRAWL_SHORT_BATCH=2,
    )
    step = Step10Variants()
    step._cat = ctx.config.cat

    browser_sessions = 0
    events = []
    waiter_started = threading.Event()
    waiter_done = threading.Event()

    class DummyBrowser:
        def new_page(self):
            return object()

    def fake_browser(*a, **k):
        nonlocal browser_sessions
        browser_sessions += 1
        return nullcontext(DummyBrowser())

    monkeypatch.setattr(ctx, "browser", fake_browser)

    def fake_fetch_one(_ctx, browser, page, asin, now, **kw):
        events.append(asin)
        # 处理到第 2 条时，另一个小批任务入队等待
        if len(events) == 2 and not waiter_started.is_set():
            waiter_started.set()
            def other_cat():
                with AmzCrawlSlot(
                    max_concurrent=1,
                    wait_seconds=10,
                    label="ShortCat",
                    poll=0.05,
                    priority=PRIORITY_SHORT,
                    remaining=2,
                ):
                    events.append("OTHER_CAT")
                    time.sleep(0.05)
                waiter_done.set()
            threading.Thread(target=other_cat, daemon=True).start()
            time.sleep(0.12)  # 确保排队票据落盘
        return {"request_asin": asin, "ok": True, "status": "ok"}

    monkeypatch.setattr(step, "_browser_fetch_one", fake_fetch_one)
    asins = [f"B0SLICE{i:03d}" for i in range(6)]
    out = step._browser_fallback(ctx, asins)
    assert waiter_done.wait(5)
    assert len(out) == 6 and all(r["ok"] for r in out)
    # 处理满 3 条时间片后让渡给排队中的 OTHER_CAT，再续跑后 3 条
    assert events == ["B0SLICE000", "B0SLICE001", "B0SLICE002", "OTHER_CAT", "B0SLICE003", "B0SLICE004", "B0SLICE005"]
    assert browser_sessions == 2

    # 无排队者时不让渡，单会话连续跑完
    browser_sessions = 0
    events.clear()
    out2 = step._browser_fallback(ctx, asins)
    assert len(out2) == 6
    assert browser_sessions == 1


def test_amz_crawl_slot_shared_ip_cooldown_and_network_error_breaker(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from core.amz_crawl_lock import AmzCrawlSlot, remaining_ip_cooldown, set_ip_cooldown
    import steps.step10_variants as s10mod

    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
    # 1) 共享 IP 冷却会阻止排队任务立即拿槽，直到冷却到期
    set_ip_cooldown(0.25, reason="连续captcha限流", label="CatA·步骤10")
    rem, info = remaining_ip_cooldown()
    assert rem > 0 and info.get("label") == "CatA·步骤10"
    logs = []
    t0 = time.monotonic()
    with AmzCrawlSlot(max_concurrent=1, wait_seconds=5, label="CatB", log=logs.append, poll=0.05):
        waited = time.monotonic() - t0
    assert waited >= 0.2
    assert any("本机 IP 共享冷却中" in m for m in logs)
    assert remaining_ip_cooldown()[0] == 0.0

    # 2) 连续网络异常触发阶梯冷却并在 6 次后提前止损，且不覆盖 captcha 共享冷却逻辑
    ctx = _ctx(
        tmp_path,
        DRY_RUN=False,
        FETCH_LONG_EVERY=0,
        AMZ_IP_COOLDOWN_SECONDS=0,
    )
    step = Step10Variants()
    step._cat = ctx.config.cat

    class DummyBrowser:
        def new_page(self):
            return object()

    monkeypatch.setattr(ctx, "browser", lambda *a, **k: nullcontext(DummyBrowser()))
    sleeps = []
    monkeypatch.setattr(s10mod.time, "sleep", lambda sec: sleeps.append(float(sec)))

    def fake_err(_ctx, browser, page, asin, now, **kw):
        rec = step._failed_record(asin, "failed", now)
        rec["blocked_kind"] = "error"
        return rec

    monkeypatch.setattr(step, "_browser_fetch_one", fake_err)
    out = step._browser_fallback(ctx, [f"B0ERR{i:05d}" for i in range(8)])
    assert len(out) == 8
    assert sleeps == [30.0, 60.0, 120.0, 120.0]
    assert out[6]["error"] == "连续网络异常熔断跳过, 交由后续兜底"
    assert out[7]["error"] == "连续网络异常熔断跳过, 交由后续兜底"
