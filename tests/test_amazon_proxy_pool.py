"""亚马逊静态住宅代理 + 多账号槽位池单元测试。"""
from __future__ import annotations

from core import amazon_login_share, amazon_pool
from core.amz_crawl_lock import resolve_max_concurrent
from core.config import Config
from core.paths import Paths
from core.proxy_utils import mask_proxy_url, parse_proxy_url, to_curl_proxy
from steps.base import StepContext


def test_parse_and_mask_proxy_formats():
    p1 = parse_proxy_url("socks5://192.0.2.10:45001:exampleuser:examplepass")
    assert p1 is not None
    assert p1.scheme == "socks5"
    assert p1.host == "192.0.2.10"
    assert p1.port == 45001
    assert p1.username == "exampleuser"
    assert p1.password == "examplepass"
    assert p1.needs_bridge_for_chromium is True
    assert to_curl_proxy("socks5://192.0.2.10:45001:exampleuser:examplepass") == "socks5h://exampleuser:examplepass@192.0.2.10:45001"
    masked = mask_proxy_url("socks5://192.0.2.10:45001:exampleuser:examplepass")
    assert "examplepass" not in masked
    assert "192.0.2.10:45001" in masked

    p2 = parse_proxy_url("http://alice:pw123@198.51.100.20:8080")
    assert p2 is not None
    assert p2.needs_bridge_for_chromium is True
    assert p2.standard_url() == "http://alice:pw123@198.51.100.20:8080"

    p3 = parse_proxy_url("http://198.51.100.20:8080")
    assert p3 is not None and p3.needs_bridge_for_chromium is False


def test_pool_crud_encryption_and_auto_concurrency(tmp_path):
    profile_root = tmp_path / "profiles"
    s1 = amazon_pool.upsert_slot(
        profile_root,
        name="美区住宅1",
        market="amazon.com",
        proxy_url="socks5://192.0.2.10:45001:u1:testpassA",
        enabled=True,
    )
    s2 = amazon_pool.upsert_slot(
        profile_root,
        name="美区住宅2",
        market="amazon.com",
        proxy_url="http://u2:testpassB@203.0.113.30:8000",
        enabled=True,
    )
    assert s1["slot_id"] == "slot-1"
    assert s2["slot_id"] == "slot-2"
    # 磁盘文件不应含明文密码
    raw_disk = (profile_root / "amazon-proxy-pool.json").read_text(encoding="utf-8")
    assert "testpassA" not in raw_disk
    assert "testpassB" not in raw_disk

    # 未显式指定并发数时，2 个启用槽位自动将并发上限扩至 2
    cfg = Config({
        "CAT": "CatA-US",
        "DATA_ROOT": str(tmp_path),
        "PROFILE_DIR": str(profile_root),
        "SITE": "美国",
        "MARKET": "amazon.com",
    })
    assert resolve_max_concurrent(cfg) == 2

    # StepContext 自动绑定槽位并透传给 http() 和 browser("amazon")
    ctx = StepContext(cfg, Paths(cfg))
    slot = ctx.amz_slot()
    assert slot is not None and slot["slot_id"] in ("slot-1", "slot-2")
    http = ctx.http()
    assert http.slot_id == slot["slot_id"]
    assert http.proxy_url == slot["proxy_url"]
    browser = ctx.browser("amazon", headed=True)
    assert browser.amazon_slot_id == slot["slot_id"]
    assert browser.proxy_url == slot["proxy_url"]
    assert browser.profile_dir == slot["profile_dir"]

    # 独立槽位共享登录态路径隔离
    p_default = amazon_login_share._path(profile_root, "amazon.com")
    p_slot1 = amazon_login_share._path(profile_root, "amazon.com", slot_id="slot-1")
    assert p_default != p_slot1
    assert p_slot1.name == "amazon.com__slot-1.bin"

    assert amazon_pool.delete_slot(profile_root, "slot-2") is True
    assert resolve_max_concurrent(cfg) == 1
