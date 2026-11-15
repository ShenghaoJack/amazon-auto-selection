from pathlib import Path

import pytest
import yaml

import webui.server as server
from steps._ss_accounts import account_plan


@pytest.fixture
def creation_root(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "push_cat", lambda *args, **kwargs: None)
    (tmp_path / "config.yaml").write_text(
        'CAT: old-US-202601\nSITE: 美国\nMARKET: amazon.com\n'
        'SS_ACCOUNT: old@example.test\nSS_PASSWORD: oldpass\n'
        'SS_ACTIVE_ACCOUNT: backup1\nSS_ACCOUNTS:\n'
        '  - {account: old@example.test, password: oldpass, is_main: true}\n'
        '  - {account: backup@example.test, password: backpass, is_main: false}\n',
        encoding="utf-8",
    )
    return tmp_path


def request(**overrides):
    data = {
        "cat": "Coffee Grinder-US-202401",
        "market_code": "de",
        "year": "2026",
        "month": "8",
        "ss_account": "new@example.test",
        "ss_password": 'pass\\word#both"\'',
        "list_url": "https://www.amazon.de/gp/bestsellers/kitchen/1234",
    }
    data.update(overrides)
    return data


def test_new_category_uses_selected_market_month_and_main_account(creation_root):
    result = server.Handler._api_category_create(None, request())
    assert result["cat"] == "Coffee Grinder-DE-202608"
    cfg = yaml.safe_load((creation_root / result["file"]).read_text(encoding="utf-8"))
    assert (cfg["SITE"], cfg["MARKET"]) == ("德国", "amazon.de")
    assert (cfg["TRAFFIC_MONTH"], cfg["COMPETITOR_MONTH"], cfg["TREND_MONTH"]) == (
        "2026-08", "2026-08", "202608"
    )
    plan = account_plan(cfg)
    assert [(item["kind"], item["account"]) for item in plan] == [
        ("main", "new@example.test"), ("backup", "backup@example.test")
    ]
    assert plan[0]["password"] == request()["ss_password"] == cfg["SS_PASSWORD"]


@pytest.mark.parametrize("changes", [
    {"ss_account": ""}, {"ss_password": ""}, {"market_code": "nl"},
    {"year": "2026", "month": "13"}, {"ss_password": "bad\nvalue"},
    {"list_url": "not a url"},
    {"list_url": "https://www.amazon.com/gp/bestsellers/kitchen/1234"},
    {"list_url": "https://www.amazon.de/gp/new-releases/kitchen/1234"},
    {"list_url": "https://amazon.de.evil.test/gp/bestsellers/kitchen/1234"},
    {"list_url": "https://www.amazon.de/gp/bestsellers/kitchen/1234?ref=test"},
])
def test_invalid_request_does_not_create_files(creation_root, changes):
    with pytest.raises(ValueError):
        server.Handler._api_category_create(None, request(**changes))
    assert not (creation_root / "data").exists()


@pytest.mark.parametrize("url", [
    "", "https://www.amazon.de/-/en/gp/bestsellers/kitchen/1234",
    "https://amazon.de/gp/bestsellers/kitchen/1234/ref=abc",
])
def test_supported_bestseller_urls_are_accepted(creation_root, url):
    result = server.Handler._api_category_create(None, request(list_url=url))
    cfg = yaml.safe_load((creation_root / result["file"]).read_text(encoding="utf-8"))
    assert cfg["LIST_URLS"] == url


def test_existing_category_is_not_overwritten(creation_root):
    result = server.Handler._api_category_create(None, request())
    path = creation_root / result["file"]
    before = path.read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        server.Handler._api_category_create(None, request())
    assert path.read_bytes() == before
