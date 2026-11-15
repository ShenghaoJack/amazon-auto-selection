# -*- coding: utf-8 -*-
"""VOC 字段归一测试: 两源(有 ID/无 ID)必须得到同一去重键与同一字段口径。"""
import re

from tools.voc.normalize import (attrs_text, fallback_key, fallback_key_strict,
                                 normalize_record, norm_text, parse_star, row_id, to_iso_date)

PANGO = {
    "_source": "pangolinfo", "_request_asin": "B063C59EEB", "_stratum": "critical",
    "reviewId": "R3OTW547YV1FD2", "asin": "B0B9E95A95", "star": "1.0 out of 5 stars",
    "title": "Turned moldy", "content": "Turned moldy after a month of usage.",
    "date": "September 19, 2026", "author": "Ryan L.", "purchased": True, "vineVoice": False,
    "helpful": "", "attributes": [{"Size": "2-Piece"}, {"Color": "Brown"}],
    "imgs": None, "videos": None, "country": "United States",
}
SS = {
    "_source": "sellersprite", "_request_asin": "B063C59EEB", "_queried_asin": "B01FFD7001",
    "_stratum": "critical", "star": 1, "title": "Turned moldy",
    "content": "Turned moldy after a month of usage.", "date": 1789758000000,
    "author": "Ryan L.", "verified": True, "vine": False, "likes": 0,
    "skus": ["Size: 2-Piece", "Color: Brown"], "images": None, "videos": None,
}


def test_date_formats_agree_within_one_day():
    """英文日期与 epoch 毫秒**可能差 1 天**(实测: SS 的 1789758000000 = 2026-09-18 19:00 UTC,
    Pangolinfo 同一评论写 September 19, 2026) —— 故只断言"同一天或相邻一天", 不要求完全相等。"""
    from datetime import date, timedelta
    a = date.fromisoformat(to_iso_date("September 19, 2026"))
    b = date.fromisoformat(to_iso_date(1789758000000))
    assert abs((a - b).days) <= 1, "两源日期漂移不应超过 1 天"
    assert to_iso_date("2026-09-19") == "2026-09-19"
    assert to_iso_date("") == "" and to_iso_date(None) == ""


def test_fallback_key_tolerates_one_day_vendor_drift():
    """兜底键不含日期 → 两源日期差 1 天也能合并(曾经的跨源去重漏洞)。"""
    k1 = fallback_key("Ryan L.", "Turned moldy", "Turned moldy after a month of usage.")
    k2 = fallback_key("Ryan L.", "Turned moldy", "Turned moldy after a month of usage.")
    assert k1 == k2
    strict_a = fallback_key_strict("Ryan L.", "2026-09-19", "Turned moldy", "x")
    strict_b = fallback_key_strict("Ryan L.", "2026-09-18", "Turned moldy", "x")
    assert strict_a != strict_b, "严格键(含日期)保留差异, 供审计使用"


def test_parse_star_both_sources():
    assert parse_star("1.0 out of 5 stars") == 1
    assert parse_star("5.0 out of 5 stars") == 5
    assert parse_star(4) == 4
    assert parse_star("") == 0


def test_attrs_text_equivalent_across_sources():
    assert attrs_text([{"Size": "2-Piece"}, {"Color": "Brown"}]) == "Size=2-Piece; Color=Brown"
    assert attrs_text(["Size: 2-Piece", "Color: Brown"]) == "Size=2-Piece; Color=Brown"


def test_norm_text_strips_trailing_ellipsis():
    assert norm_text("  Turned   Moldy… ") == "turned moldy"


def test_cross_source_records_share_fallback_key_and_merge_fields():
    a = normalize_record(PANGO)
    b = normalize_record(SS)
    assert a["fallback_key"] == b["fallback_key"], "两源同一条评论的兜底键必须一致"
    assert a["review_id"] == "R3OTW547YV1FD2" and b["review_id"] == ""
    assert a["variant_asin"] == "B0B9E95A95"
    assert b["variant_asin"] == ""  # 请求ASIN不能冒充评论实际变体。
    assert b["queried_asin"] == "B01FFD7001" and b["asin_role"] == "unknown"
    assert a["parent_asin"] == b["parent_asin"] == "B063C59EEB"
    assert a["variant_attributes"] == b["variant_attributes"] == "Size=2-Piece; Color=Brown"


def test_row_id_prefers_native_and_synthetic_matches_validator_pattern():
    a = normalize_record(PANGO)
    b = normalize_record(SS)
    assert row_id(a) == "R3OTW547YV1FD2"
    assert re.fullmatch(r"R[A-Z0-9]{5,}", row_id(b)), "生成的 ID 必须匹配校验脚本正则"


def test_fallback_key_uses_complete_body_to_avoid_prefix_collision():
    long_body = "x" * 500
    k1 = fallback_key("A", "t", long_body)
    k2 = fallback_key("A", "t", long_body + "TAIL")
    assert k1 != k2, "正文尾部不同必须保留为不同内容，不能只用前400字合并"
