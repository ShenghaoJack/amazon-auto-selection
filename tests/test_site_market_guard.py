# 单元测试: 站点三口径(CAT 站点后缀 / SITE / MARKET)一致性提示 + 英国站命名识别(2026-09-14)
# 背景: 三个字段彼此原本没有任何校验 —— 写 "…-UK-202609" 却留着 SITE=美国+MARKET=amazon.com 会
#   静默产出"美国站关键词/趋势数据 + 英国站商品页面"; SITE 写成非站点名(如 德国站)还会被
#   core/mcp.py::_marketplace() 静默回落成美国站(US)。这里只提示不阻断。
import re
from pathlib import Path

from core.category_config import (
    KNOWN_SITES,
    REGION_PROFILE,
    SITE_TO_MARKET,
    cat_region_token,
    check_site_market_consistency,
)
from core.catname import display_cat

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- 英国站命名识别
def test_cat_region_token_recognizes_uk_and_gb():
    assert cat_region_token("Cutting Boards-UK-202609") == "uk"
    assert cat_region_token("Cutting Boards-GB-202609") == "gb"
    assert cat_region_token("Cutting Boards-UK") == "uk"
    assert cat_region_token("Cutting Boards-DE-202609") == "de"
    assert cat_region_token("Cutting Boards-FR-202609") == "fr"
    assert cat_region_token("Salt and Pepper Mill Sets-US-202608") == "us"
    # 旧命名里站点码夹在版本与月份之间也要认出来(它本身是类目的站点标识)
    assert cat_region_token("cutting-board-v2-us-202607") == "us"
    assert cat_region_token("cutting-board-us-v2") == "us"
    # 非地区码 / 无后缀 / 只在中间出现 -> 不算站点后缀
    assert cat_region_token("salt-pepper-shaker-sets") == ""
    assert cat_region_token("Cutting Boards-202609") == ""
    assert cat_region_token("Cutting Boards-set") == ""


def test_display_cat_strips_gb_suffix():
    """GB 是亚马逊英国的官方国家码: 必须和 UK 一样被当后缀剥掉, 不能残留在 LLM 提示词里。"""
    assert display_cat("Cutting Boards-GB-202609") == "Cutting Boards"
    assert display_cat("Cutting Boards-GB") == "Cutting Boards"
    assert display_cat("Cutting Boards-UK-202609") == "Cutting Boards"


def test_frontend_region_tokens_include_gb():
    """前端类目中心分组用另一份清单: 两处必须同步, 否则 GB 类目在页面上分不出站点。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"const REGION_TOKENS = new Set\(\[([^\]]*)\]\);", html)
    assert m, "未找到 REGION_TOKENS"
    tokens = {t.strip().strip('"').lower() for t in m.group(1).split(",") if t.strip()}
    for tok in ("us", "uk", "gb", "de", "fr"):
        assert tok in tokens, f"前端 REGION_TOKENS 缺 {tok.upper()}"


def test_site_table_matches_mcp_site_codes():
    """两份站点清单不能漂移: 本模块的 KNOWN_SITES 必须等于 core/mcp.py 的站点码表键。"""
    from core.mcp import _SITE_TO_CODE
    assert set(KNOWN_SITES) == set(_SITE_TO_CODE)
    # 站点名 -> 域名 的映射里, 每个站点都应是某个地区码的站点名
    for site, market in SITE_TO_MARKET.items():
        assert market.startswith("amazon.")
        assert any(p["site"] == site for p in REGION_PROFILE.values())


# --------------------------------------------------------------- 一致性提示
def test_consistent_uk_pair_no_warning():
    assert check_site_market_consistency("Cutting Boards-UK-202609", "英国", "amazon.co.uk") == []
    assert check_site_market_consistency("Cutting Boards-GB-202609", "英国", "amazon.co.uk") == []


def test_consistent_de_fr_us_pairs_no_warning():
    assert check_site_market_consistency("Cutting Boards-DE-202609", "德国", "amazon.de") == []
    assert check_site_market_consistency("Cutting Boards-FR-202609", "法国", "amazon.fr") == []
    assert check_site_market_consistency("Trays-US-202608", "美国", "amazon.com") == []


def test_no_region_suffix_and_no_site_means_no_warning():
    """老类目(无站点后缀、也没写 SITE/MARKET)不应被误报。"""
    assert check_site_market_consistency("salt-pepper-shaker-sets", "", "") == []
    assert check_site_market_consistency("salt-pepper-shaker-sets", "", "amazon.com") == []


def test_cat_suffix_vs_site_mismatch_warns():
    warn = check_site_market_consistency("Cutting Boards-UK-202609", "美国", "amazon.co.uk")
    assert warn and any("SITE" in w and "英国" in w for w in warn)


def test_cat_suffix_vs_market_mismatch_warns():
    warn = check_site_market_consistency("Cutting Boards-UK-202609", "英国", "amazon.com")
    assert warn and any("amazon.co.uk" in w for w in warn)


def test_site_vs_market_mismatch_warns_without_region_suffix():
    warn = check_site_market_consistency("Trays", "德国", "amazon.com")
    assert warn and any("混合结果" in w for w in warn)


def test_all_three_mismatched_warns_multiple():
    warn = check_site_market_consistency("Cutting Boards-UK-202609", "德国", "amazon.com")
    assert len(warn) >= 2


def test_unknown_site_name_warns_about_silent_us_fallback():
    warn = check_site_market_consistency("Trays-DE-202609", "德国站", "amazon.de")
    assert warn and any("静默按美国站" in w for w in warn)


def test_warnings_are_deduplicated_and_prefixed():
    warn = check_site_market_consistency("Cutting Boards-UK-202609", "美国", "amazon.com")
    assert len(warn) == len(set(warn))
    assert all(w.startswith("[SITE_MARKET]") for w in warn)


# --------------------------------------------------------------- 接线(提示真的会被触发)
def test_webui_save_returns_warnings(ws_tmp, monkeypatch):
    """webui 保存带矛盾的站点字段 -> 返回 warnings(不阻断保存)。"""
    proj = Path(ws_tmp)
    (proj / "data").mkdir(parents=True, exist_ok=True)
    import importlib.util

    spec = importlib.util.spec_from_file_location("webui_server_sm", ROOT / "webui" / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "PROJECT_ROOT", proj)
    monkeypatch.setattr(server, "RUN_DIR", proj / ".webui" / "runs")
    from core import config_backup as cb
    monkeypatch.setattr(cb, "BACKUP_DIR", proj / ".webui" / "config-backups")

    cfg = proj / "data" / "Cutting Boards-UK-202609"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        'CAT: "Cutting Boards-UK-202609"\nDATA_ROOT: "data"\nSITE: "美国"\nMARKET: "amazon.com"\n',
        encoding="utf-8")
    out = server.Handler._api_config_save(
        None, {"config_file": "data/Cutting Boards-UK-202609/config.yaml",
               "values": {"MARKET": "amazon.com"}, "lists": {}})
    assert out.get("warnings"), "应给出 CAT 后缀/SITE/MARKET 不一致的提示"
    assert any("英国" in w for w in out["warnings"])
