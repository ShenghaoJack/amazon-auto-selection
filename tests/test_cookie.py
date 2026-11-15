# 单元测试: Cookie 头解析(Profile 首次登录导入用)
from core.browser import parse_cookie_header


def test_parse_basic():
    header = "session-id=134-8206883; ubid-main=135-6359705; i18n-prefs=USD"
    cookies = parse_cookie_header(header, "amazon.com")
    assert len(cookies) == 3
    assert cookies[0]["name"] == "session-id"
    assert cookies[0]["value"] == "134-8206883"
    assert cookies[0]["domain"] == ".amazon.com"
    assert cookies[0]["path"] == "/"
    assert cookies[0]["secure"] is False


def test_parse_secure_prefix():
    header = "__Secure-id_dbp=Dbsc|xxx; session-token=abc"
    cookies = parse_cookie_header(header, "amazon.com")
    by_name = {c["name"]: c for c in cookies}
    assert by_name["__Secure-id_dbp"]["secure"] is True
    assert by_name["session-token"]["secure"] is False


def test_parse_skip_invalid():
    header = "a=1; ; noequals; =empty; b=2"
    cookies = parse_cookie_header(header, "amazon.com")
    names = [c["name"] for c in cookies]
    assert names == ["a", "b"]


def test_parse_empty():
    assert parse_cookie_header("", "amazon.com") == []
    assert parse_cookie_header("  ;  ", "amazon.com") == []


def test_parse_domain_already_dotted():
    cookies = parse_cookie_header("a=1", ".amazon.com")
    assert cookies[0]["domain"] == ".amazon.com"
