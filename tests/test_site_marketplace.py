# 单元测试: 站点映射(2026-09-15) —— SITE 写德/法/英等站点要自动切到对应 MCP 库
# ---------------------------------------------------------------------------
# 背景(用户提问): "如果站点我写德国和法国会自动切换吗?"
# 实测发现两个问题:
#   ① 精确写站点名(德国/法国/英国…)或国家码(de/fr/uk)本来就会切 —— 这条要守住;
#   ② 但 `德国站`/`德`/`Germany`/`Amazon.de 德国` 等常见写法会**静默回落成 US**,
#      用户以为在挖德国站、实际拿到美国库数据(实测 US 45,370 / UK 71,488 / DE 5,439 各不相同)。
# 修法: `_marketplace()` 归一化匹配(去空白/前后缀 + 站点名/国家码), 认不出才回落 US;
#       `is_known_site()` 供调用方对"认不出"发告警(步骤5 已接日志)。
import pytest

from core.config import Config
from core.mcp import _SITE_TO_CODE, _marketplace, is_known_site


@pytest.mark.parametrize("site,code", [
    ("美国", "US"), ("英国", "UK"), ("德国", "DE"), ("法国", "FR"), ("意大利", "IT"),
    ("西班牙", "ES"), ("加拿大", "CA"), ("日本", "JP"), ("印度", "IN"), ("墨西哥", "MX"),
    ("巴西", "BR"), ("澳大利亚", "AU"), ("阿联酋", "AE"),
])
def test_exact_chinese_site_names_map(site, code):
    """精确站点名 → 对应国家码(用户最常写的写法)。"""
    assert _marketplace(site) == code
    assert is_known_site(site) is True


@pytest.mark.parametrize("site,code", [
    ("us", "US"), ("US", "US"), ("uk", "UK"), ("de", "DE"), ("DE", "DE"),
    ("fr", "FR"), ("jp", "JP"), ("ca", "CA"), ("au", "AU"), ("ae", "AE"),
])
def test_country_codes_map(site, code):
    """直接写国家码也能认(大小写不敏感)。"""
    assert _marketplace(site) == code


@pytest.mark.parametrize("site,code", [
    ("德国站", "DE"), ("法国站", "FR"), ("英国站", "UK"), ("日本站", "JP"),
    ("德国 ", "DE"), ("德国　", "DE"),           # 尾随半角/全角空格
    ("亚马逊德国", "DE"), ("亚马逊法国", "FR"),   # 前缀
    ("Amazon.de", "DE"), ("amazon.fr", "FR"),    # 域名写法
    ("Amazon.co.uk", "UK"),
])
def test_tolerant_writings_map(site, code):
    """常见容错写法必须能识别 —— 这些以前会**静默回落 US**(本次修复的重点)。"""
    assert _marketplace(site) == code, f"{site!r} 应映射为 {code}"
    assert is_known_site(site) is True


@pytest.mark.parametrize("site", ["德", "法", "Germany", "germany", "France", "英国籍"])
def test_unknown_writings_fall_back_to_us_but_are_reported(site):
    """真认不出的写法: 仍回落 US(向后兼容), 但 `is_known_site` 必须报 False 以便告警。"""
    assert _marketplace(site) == "US"
    assert is_known_site(site) is False, f"{site!r} 认不出时必须报 False(否则静默出错)"


def test_empty_site_is_us_and_reported():
    """SITE 空 → US; 空值不算"已识别"(配置缺站点时应提示)。"""
    assert _marketplace("") == "US"
    assert is_known_site("") is False


def test_map_covers_all_supported_sites():
    """映射表本身要覆盖 13 个站点(与 seller sprite 站点名一致), 且值唯一。"""
    assert len(_SITE_TO_CODE) == 13
    assert len(set(_SITE_TO_CODE.values())) == 13


def test_site_market_consistency_uses_same_vocabulary():
    """站点名口径与类目配置里的 KNOWN_SITES / REGION_PROFILE 保持一致(防两套口径漂移)。"""
    from core.category_config import KNOWN_SITES, REGION_PROFILE

    for name in KNOWN_SITES:
        assert name in _SITE_TO_CODE, f"{name} 在 _SITE_TO_CODE 里缺失"
    # REGION_PROFILE 的 site 名(非空)也必须在映射表里
    for prof in REGION_PROFILE.values():
        if prof.get("site"):
            assert prof["site"] in _SITE_TO_CODE, f"{prof['site']} 未登记"
