"""Unit tests for Step 14 BuyBox/sellerNation MCP backfill and Amazon -> AMZ fulfillment normalization."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step14_traffic_split import Step14TrafficSplit


def _make_ctx(tmp_path, *, month: str = "202610", lookup_fn=None) -> StepContext:
    cfg = Config({
        "CAT": "TestCat-US-202610",
        "CATEGORY_NAME": "TestCat",
        "DATA_ROOT": str(tmp_path),
        "SITE": "美国",
        "TREND_MONTH": month,
        "COMPETITOR_MONTH": month,
        "TRAFFIC_MONTH": month,
    })
    ctx = StepContext(cfg, Paths(cfg))
    mcp_stub = SimpleNamespace(competitor_lookup=lookup_fn)
    ctx._mcp = mcp_stub
    return ctx


def test_amazon_buybox_case_insensitive_overrides_fba_fbm_and_empty(tmp_path):
    calls = []

    def fake_lookup(month, asins):
        calls.append((month, list(asins)))
        return []

    ctx = _make_ctx(tmp_path, lookup_fn=fake_lookup)
    df = pd.DataFrame([
        {"变体ASIN": "A1", "BuyBox卖家": "Amazon", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-01"},
        {"变体ASIN": "A2", "BuyBox卖家": "AMAZON", "卖家所属地": "", "配送方式": "FBM", "上架时间": "2024-01-02"},
        {"变体ASIN": "A3", "BuyBox卖家": "Amazon.com", "卖家所属地": "", "配送方式": "", "上架时间": "2024-01-03"},
        {"变体ASIN": "A4", "BuyBox卖家": "amazon.co.uk", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-04"},
        {"变体ASIN": "A5", "BuyBox卖家": "Amazon Resale", "卖家所属地": "", "配送方式": "FBM", "上架时间": "2024-01-05"},
    ])
    out = Step14TrafficSplit()._fill_logistics(ctx, df)
    assert list(out["配送方式"]) == ["AMZ", "AMZ", "AMZ", "AMZ", "AMZ"]
    # Known Amazon rows with availableDate should NOT trigger MCP queries just because 卖家所属地 is empty
    assert calls == []


def test_mcp_fills_buybox_and_nation_before_amz_fulfillment_correction(tmp_path):
    calls = []

    def fake_lookup(month, asins):
        calls.append((month, list(asins)))
        if month == "2026-10":
            return [
                # Originally missing BuyBox卖家 & had FBA -> MCP reveals AMAZON.COM -> must become AMZ, and never fill 卖家所属地 even if sellerNation present
                {"asin": "B1", "sellerName": "AMAZON.COM", "sellerNation": "US", "fulfillment": "FBA", "availableDate": "2023-05-01"},
                # Third-party missing all fields -> filled from latest month
                {"asin": "B2", "sellerName": "PetShop LLC", "sellerNation": "US", "fulfillment": "FBA", "availableDate": "2025-02-15"},
                # Partial hit in latest month: only sellerName, missing sellerNation -> older month should fill nation
                {"asin": "B3", "sellerName": "DirectStore", "sellerNation": None, "fulfillment": "FBM", "availableDate": "2025-03-10"},
            ]
        if month == "2026-09":
            return [
                {"asin": "B3", "sellerName": "OlderName", "sellerNation": "CN", "fulfillment": "FBA", "availableDate": "2024-01-01"},
            ]
        return []

    ctx = _make_ctx(tmp_path, month="202610", lookup_fn=fake_lookup)
    df = pd.DataFrame([
        {"变体ASIN": "B1", "BuyBox卖家": "", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2023-05-01"},
        {"变体ASIN": "B2", "BuyBox卖家": "", "卖家所属地": "", "配送方式": "", "上架时间": ""},
        {"变体ASIN": "B3", "BuyBox卖家": "", "卖家所属地": "", "配送方式": "", "上架时间": ""},
        {"变体ASIN": "B4", "BuyBox卖家": "UnknownShop", "卖家所属地": "", "配送方式": "", "上架时间": ""},
    ])
    out = Step14TrafficSplit()._fill_logistics(ctx, df).set_index("变体ASIN")

    assert out.at["B1", "BuyBox卖家"] == "AMAZON.COM"
    assert out.at["B1", "配送方式"] == "AMZ"
    assert out.at["B1", "卖家所属地"] == ""

    assert out.at["B2", "BuyBox卖家"] == "PetShop LLC"
    assert out.at["B2", "卖家所属地"] == "US"
    assert out.at["B2", "配送方式"] == "FBA"
    assert out.at["B2", "上架时间"] == "2025-02-15"

    # B3 keeps latest month sellerName/fulfillment/availableDate, gets sellerNation from 202609
    assert out.at["B3", "BuyBox卖家"] == "DirectStore"
    assert out.at["B3", "卖家所属地"] == "CN"
    assert out.at["B3", "配送方式"] == "FBM"
    assert out.at["B3", "上架时间"] == "2025-03-10"

    # B4 had no MCP hits -> 配送方式 falls back to 未知
    assert out.at["B4", "配送方式"] == "未知"

    # B1 and B2 were complete after 2026-10, so 2026-09 should NOT re-query B1 or B2
    m202609_asins = [asins for m, asins in calls if m == "2026-09"][0]
    assert "B1" not in m202609_asins
    assert "B2" not in m202609_asins
    assert "B3" in m202609_asins


def test_recent30_uses_near_30_days_mcp_lookup(tmp_path):
    calls = []

    def fake_lookup(month, asins):
        calls.append((month, list(asins)))
        return [
            {"asin": "C1", "sellerName": "Amazon", "sellerNation": None, "fulfillment": "FBA", "availableDate": "2024-06-01"},
            {"asin": "C2", "sellerName": "Acme Co", "sellerNation": "US", "fulfillment": "FBA", "availableDate": "2025-01-01"},
        ]

    ctx = _make_ctx(tmp_path, month="最近30天", lookup_fn=fake_lookup)
    df = pd.DataFrame([
        {"变体ASIN": "C1", "BuyBox卖家": "", "卖家所属地": "", "配送方式": "", "上架时间": ""},
        {"变体ASIN": "C2", "BuyBox卖家": "", "卖家所属地": "", "配送方式": "", "上架时间": ""},
    ])
    out = Step14TrafficSplit()._fill_logistics(ctx, df).set_index("变体ASIN")

    assert calls == [("", ["C1", "C2"])]
    assert out.at["C1", "BuyBox卖家"] == "Amazon"
    assert out.at["C1", "配送方式"] == "AMZ"
    assert out.at["C2", "卖家所属地"] == "US"
    assert out.at["C2", "配送方式"] == "FBA"


def test_sync_seller_nation_by_seller_and_unique_brand(tmp_path):
    ctx = _make_ctx(tmp_path, month="202610", lookup_fn=lambda m, a: [])
    df = pd.DataFrame([
        # Known rows
        {"变体ASIN": "S1", "品牌": "BrandUnique", "BuyBox卖家": "ShopA", "卖家所属地": "CN", "配送方式": "FBA", "上架时间": "2024-01-01"},
        {"变体ASIN": "S2", "品牌": "BrandConflict", "BuyBox卖家": "ShopCN", "卖家所属地": "CN", "配送方式": "FBA", "上架时间": "2024-01-01"},
        {"变体ASIN": "S3", "品牌": "BrandConflict", "BuyBox卖家": "ShopUS", "卖家所属地": "US", "配送方式": "FBA", "上架时间": "2024-01-01"},
        {"变体ASIN": "S4", "品牌": "Generic", "BuyBox卖家": "GenShop1", "卖家所属地": "US", "配送方式": "FBA", "上架时间": "2024-01-01"},
        # Targets to test:
        # T1: Same BuyBox seller (ShopUS) inside a conflicting brand -> filled via BuyBox seller = US
        {"变体ASIN": "T1", "品牌": "BrandConflict", "BuyBox卖家": "shopus", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-01"},
        # T2: Different seller, unique brand (BrandUnique) -> filled via brand = CN
        {"变体ASIN": "T2", "品牌": "brandunique", "BuyBox卖家": "ShopB", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-01"},
        # T3: Conflicting brand + unknown seller -> stays empty
        {"变体ASIN": "T3", "品牌": "BrandConflict", "BuyBox卖家": "ShopOther", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-01"},
        # T4: Generic brand -> must NOT sync even though S4 is US
        {"变体ASIN": "T4", "品牌": "Generic", "BuyBox卖家": "GenShop2", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-01"},
        # T5: Amazon self-operated inside BrandUnique -> must stay empty (and AMZ)
        {"变体ASIN": "T5", "品牌": "BrandUnique", "BuyBox卖家": "AMAZON", "卖家所属地": "", "配送方式": "FBA", "上架时间": "2024-01-01"},
    ])
    out = Step14TrafficSplit()._fill_logistics(ctx, df).set_index("变体ASIN")

    assert out.at["T1", "卖家所属地"] == "US"
    assert out.at["T2", "卖家所属地"] == "CN"
    assert out.at["T3", "卖家所属地"] == ""
    assert out.at["T4", "卖家所属地"] == ""
    assert out.at["T5", "卖家所属地"] == ""
    assert out.at["T5", "配送方式"] == "AMZ"
