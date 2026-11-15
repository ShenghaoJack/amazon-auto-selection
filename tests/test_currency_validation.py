from types import SimpleNamespace

import pandas as pd
import pytest

from core import StepDataError
from core.category_config import KNOWN_SITES
from core.currency import SITE_CURRENCY, currency_conflict
from steps.step14_traffic_split import _positive_price, _price_cols_in, _validate_price_currency
from steps.step15_category_report import Step15CategoryReport


def test_all_workbench_sites_have_a_currency():
    assert set(KNOWN_SITES) <= SITE_CURRENCY.keys()


@pytest.mark.parametrize("site,header,code", [
    ("加拿大", "价格($)", "CAD"), ("澳大利亚", "价格($)", "AUD"),
    ("墨西哥", "价格($)", "MXN"), ("巴西", "价格(R$)", "BRL"),
    ("日本", "价格(¥)", "JPY"), ("阿联酋", "价格(AED)", "AED"),
    ("阿联酋", "价格", "AED"),
])
def test_price_header_respects_site(site, header, code):
    assert _price_cols_in(pd.DataFrame(columns=[header]), site)[2] == code


@pytest.mark.parametrize("site,value,expected", [
    ("阿联酋", "AED 120.00", 120), ("阿联酋", "د.إ 120.00", 120),
    ("加拿大", "$ 100.00", 100), ("巴西", "R$ 1.234,50", 1234.5),
    ("加拿大", "USD 100.00", None), ("德国", "USD 100", None),
])
def test_price_parsing(site, value, expected):
    assert _positive_price(value, site) == expected


def test_explicit_conflicts_block_calculation():
    with pytest.raises(StepDataError, match="价格列"):
        _validate_price_currency("加拿大", "价格(USD)", pd.Series([10]))
    with pytest.raises(StepDataError, match="1 行"):
        _validate_price_currency("德国", "价格", pd.Series(["EUR 10", "USD 20"]))
    assert currency_conflict("CA$ 10", "加拿大") == set()


@pytest.mark.parametrize("site,expected", [("美国", "达标"), ("阿联酋", "数据不足"), ("加拿大", "数据不足")])
def test_dollar_gate_does_not_compare_other_currencies(site, expected):
    ctx = SimpleNamespace(config={"SITE": site})
    rows = pd.DataFrame({"_price": [40], "_rev": [40], "_brand": ["x"], "_ship": ["FBA"], "_newold": ["旧"]})
    gates = Step15CategoryReport()._compute_gates(ctx, rows, {"has_ship": True, "has_newold": False, "has_sellers": False})
    assert gates[1][2] == expected
    if site != "美国":
        assert "未换算美元" in gates[1][1]
