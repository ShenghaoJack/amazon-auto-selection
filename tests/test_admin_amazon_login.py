"""管理员统一亚马逊共享登录态管理接口与等待逻辑单测。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import amazon_login_manager as alm


def test_normalize_market_and_wait_seconds():
    assert alm.normalize_market("https://www.amazon.co.uk/gp/bestsellers") == "amazon.co.uk"
    assert alm.normalize_market("AMAZON.COM") == "amazon.com"
    with pytest.raises(ValueError, match="无效的亚马逊站点"):
        alm.normalize_market("evil.example.com")

    assert alm.normalize_wait_seconds(None) == alm.DEFAULT_WAIT_SECONDS
    assert alm.normalize_wait_seconds("300") == 300
    assert alm.normalize_wait_seconds(30) == 30
    assert alm.normalize_wait_seconds(1800) == 1800
    with pytest.raises(ValueError, match="等待时间需在"):
        alm.normalize_wait_seconds(10)
    with pytest.raises(ValueError, match="等待时间需在"):
        alm.normalize_wait_seconds(3600)


def test_manager_waits_for_admin_login_and_publishes_share(tmp_path, monkeypatch):
    mgr = alm.AmazonLoginManager(tmp_path)
    monkeypatch.setattr(alm, "_POLL_INTERVAL", 0.02)

    check_calls = {"n": 0}
    saved_calls = []

    class FakePage:
        context = object()

        def close(self):
            pass

    class FakeBrowser:
        def __init__(self, cfg, profile_dir, headed=False):
            self.cfg = cfg
            self.profile_dir = profile_dir
            self.headed = headed

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            assert "/ap/signin" in url

        def check_amazon_login(self, page, navigate=True):
            check_calls["n"] += 1
            # 第 1 次检查现有会话=False，进入登录页后第 3 次轮询变 True
            return check_calls["n"] >= 3

    def fake_save(context, profile_root, market, replace=True):
        saved_calls.append((Path(profile_root), market, replace))
        p = Path(profile_root) / "amazon-login-share" / f"{market}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"encrypted-stub")
        return True

    monkeypatch.setattr(alm, "BrowserAutomation", FakeBrowser)
    monkeypatch.setattr(alm.amazon_login_share, "save", fake_save)

    res = mgr.start_login(market="amazon.com", wait_seconds=60, actor="admin")
    assert res["ok"] is True

    # 等待后台线程完成
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        st = mgr.status()
        if st["task"]["state"] != "running":
            break
        time.sleep(0.02)

    st = mgr.status()
    assert st["task"]["state"] == "succeeded"
    assert st["task"]["wait_seconds"] == 60
    assert saved_calls == [(tmp_path / "data" / "profiles", "amazon.com", True)]
    us = next(m for m in st["markets"] if m["market"] == "amazon.com")
    assert us["shared"] is True
    assert us["updated_at"]


def test_manager_blocks_concurrent_window_and_supports_cancel(tmp_path, monkeypatch):
    mgr = alm.AmazonLoginManager(tmp_path)
    monkeypatch.setattr(alm, "_POLL_INTERVAL", 0.02)
    entered = threading.Event()

    class FakePage:
        context = object()

        def close(self):
            pass

    class SlowBrowser:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            entered.set()

        def check_amazon_login(self, page, navigate=True):
            return False

    monkeypatch.setattr(alm, "BrowserAutomation", SlowBrowser)

    mgr.start_login(market="amazon.de", wait_seconds=120, actor="admin")
    assert entered.wait(timeout=3.0)

    # 进行中时拒绝启动第二个登录窗口
    with pytest.raises(RuntimeError, match="正在进行中"):
        mgr.start_login(market="amazon.com", wait_seconds=60, actor="admin")

    # 取消等待应立刻终止并落为 cancelled
    cancel_res = mgr.cancel_login()
    assert cancel_res["cancelled"] is True
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        st = mgr.status()
        if st["task"]["state"] != "running":
            break
        time.sleep(0.02)
    assert mgr.status()["task"]["state"] == "cancelled"


def test_webui_admin_routes_and_templates_present():
    repo_root = Path(__file__).resolve().parents[1]
    acc_html = (repo_root / "webui" / "accounts.html").read_text(encoding="utf-8")
    idx_html = (repo_root / "webui" / "index.html").read_text(encoding="utf-8")
    srv_py = (repo_root / "webui" / "server.py").read_text(encoding="utf-8")

    assert 'id="amzLoginPanel"' in acc_html
    assert 'id="amzWaitSec"' in acc_html
    assert "/api/admin/amazon-login/start" in acc_html
    assert "/api/admin/amazon-login/cancel" in acc_html
    assert 'id="amzLoginLink"' in idx_html
    assert 'path.startswith("/api/admin/")' in srv_py
    assert '"/api/admin/amazon-login"' in srv_py


def test_http_fetcher_reuses_shared_amazon_cookie_and_honors_explicit_anonymous(tmp_path):
    import os
    from unittest.mock import MagicMock
    from core import amazon_login_share as share
    from core.config import Config
    from core.http_client import HttpFetcher

    if os.name != "nt":
        pytest.skip("Windows DPAPI")

    ctx_mock = MagicMock()
    ctx_mock.cookies.return_value = [
        {"name": "at-main", "value": "shared-token", "domain": ".amazon.com", "path": "/", "expires": -1},
        {"name": "ubid-main", "value": "123-456", "domain": ".amazon.com", "path": "/", "expires": time.time() + 3600},
        {"name": "expired-ck", "value": "old", "domain": ".amazon.com", "path": "/", "expires": 100.0},
        {"name": "other-site", "value": "uk", "domain": ".amazon.co.uk", "path": "/", "expires": -1},
    ]
    assert share.save(ctx_mock, tmp_path, "amazon.com")

    cfg = Config({"MARKET": "amazon.com", "DRY_RUN": False, "PROFILE_DIR": str(tmp_path)})
    hdr = share.resolve_cookie_header(cfg)
    assert "at-main=shared-token" in hdr
    assert "ubid-main=123-456" in hdr
    assert "expired-ck" not in hdr
    assert "other-site" not in hdr

    fetcher = HttpFetcher(cfg)
    assert fetcher._headers().get("Cookie") == hdr

    # 显式 cookie="" 必须严格匿名（供 diagnose_cookie 匿名对照探测）
    anon = HttpFetcher(cfg, cookie="")
    assert "Cookie" not in anon._headers()

    # 类目配置页已隐藏 AMZ_PROFILE 与 COOKIE
    repo_root = Path(__file__).resolve().parents[1]
    idx_html = (repo_root / "webui" / "index.html").read_text(encoding="utf-8")
    assert '"AMZ_PROFILE","COOKIE"' in idx_html
    assert '{id:"亚马逊登录态"' not in idx_html
