# 单元测试: 步骤14 价格/品牌解析(≤0 视为无效, 不留 0 价行) + 多币种价列识别
import os

import pandas as pd
import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit, _parse_price, _parse_brand, _price_cols_in


@pytest.mark.parametrize(
    "cols,site,exp_main,exp_prime,exp_cur",
    [
        (["价格($)", "prime价格($)"], "美国", "价格($)", "prime价格($)", "USD"),
        (["价格(CA$)", "prime价格(CA$)"], "加拿大", "价格(CA$)", "prime价格(CA$)", "CAD"),
        (["价格(£)", "prime价格(£)"], "英国", "价格(£)", "prime价格(£)", "GBP"),
        (["价格(€)", "prime价格(€)"], "德国", "价格(€)", "prime价格(€)", "EUR"),
        (["价格(¥)", "prime价格(¥)"], "日本", "价格(¥)", "prime价格(¥)", "JPY"),
        (["价格", "prime价格"], "美国", "价格", "prime价格", "USD"),
        (["价格(USD)"], "美国", "价格(USD)", None, "USD"),
        (["价格(MXN$)"], "墨西哥", "价格(MXN$)", None, "MXN"),
    ],
)
def test_price_cols_in_variants(cols, site, exp_main, exp_prime, exp_cur):
    df = pd.DataFrame({c: [1.0] for c in cols})
    main, prime, cur = _price_cols_in(df, site)
    assert main == exp_main
    assert prime == exp_prime
    assert cur == exp_cur


def test_price_cols_in_prefers_bracketed():
    """主价优先带币种括号列(竞品导出), 而非裸'价格'。"""
    df = pd.DataFrame({"价格": [1.0], "价格($)": [2.0], "prime价格($)": [3.0]})
    main, prime, cur = _price_cols_in(df, "美国")
    assert main == "价格($)"
    assert prime == "prime价格($)"


def _ctx14(ws_tmp):
    cfg = Config({"CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": True, "SITE": "美国"})
    return StepContext(cfg, Paths(cfg))


def test_fill_price_uses_recognized_col_and_keeps_rows(ws_tmp):
    """主价=识别列(价格($)); 缺失行补抓失败不删行, 月销额留空, 进人工标记。"""
    ctx = _ctx14(ws_tmp)
    st = Step14TrafficSplit()
    st._scrape_price = lambda c, asin: None  # 补抓全失败
    df = pd.DataFrame(
        {
            "变体ASIN": ["B0AAA1", "B0AAA2"],
            "变体月销量": [10, 20],
            "价格($)": ["10.99", ""],
            "prime价格($)": ["9.99", "19.99"],
        }
    )
    out = st._fill_price(ctx, df)
    assert len(out) == 2  # 不删行
    assert st._price_main_col == "价格($)"
    assert out.loc[0, "价格($)"] == 10.99
    assert out.loc[0, "变体月销额"] == 10 * 10.99
    assert pd.isna(out.loc[1, "价格($)"])  # 补不到保留空
    assert pd.isna(out.loc[1, "变体月销额"])
    assert st._price_currency == "USD"


def test_fill_price_creates_fallback_col_without_any(ws_tmp):
    """无任何价列时建内部'价格'兜底列(极端: 竞品/明细均无价)。"""
    ctx = _ctx14(ws_tmp)
    st = Step14TrafficSplit()
    st._scrape_price = lambda c, asin: 5.0
    df = pd.DataFrame({"变体ASIN": ["B0BBB1"], "变体月销量": [7]})
    out = st._fill_price(ctx, df)
    assert st._price_main_col == "价格"
    assert out.loc[0, "价格"] == 5.0
    assert out.loc[0, "变体月销额"] == 35.0


def test_parse_price_positive():
    html = '<span class="a-offscreen">$15.99</span>'
    assert _parse_price(html) == 15.99


def test_parse_price_positive_integer():
    html = '<span class="a-offscreen">$12</span>'
    assert _parse_price(html) == 12.0


def test_parse_price_rejects_zero():
    """不可售/异常页可能匹配到 0: 必须视为无效(None), 否则月销额=0 污染交付。"""
    assert _parse_price('"priceAmount": "0"') is None
    assert _parse_price('class="a-price-whole">0<span class="a-price-fraction">00</span>') is None
    assert _parse_price("") is None


def test_parse_price_skips_zero_finds_real():
    """页面同时含 0 与真价时, 跳过 0 取真价。"""
    html = '"priceAmount": "0", "displayPrice": "$12.50"'
    assert _parse_price(html) == 12.5


def test_parse_brand_byline():
    html = '<a id="bylineInfo" href="/dp/x">Visit the Acme Store</a>'
    assert _parse_brand(html) == "Acme"


def test_parse_brand_table_fallback():
    html = '<table><tr><th>Brand</th><td>  AcmeCorp  </td></tr></table>'
    assert _parse_brand(html) == "AcmeCorp"


def test_parse_brand_empty():
    assert _parse_brand("<html></html>") == ""
