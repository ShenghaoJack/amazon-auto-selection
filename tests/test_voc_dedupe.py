# -*- coding: utf-8 -*-
"""VOC 去重测试: 主键 + 兜底键两级索引, 尤其覆盖"跨源合并"这条曾经的漏洞。"""
from tools.voc.dedupe import dedupe_reviews
from tools.voc.normalize import fallback_key


def _row(source, review_id="", author="A", date="2026-01-01", title="t", body="b", **kw):
    row = {
        "review_id": review_id, "source": source, "parent_asin": kw.pop("parent_asin", "B0TEST00001"),
        "variant_asin": kw.pop("variant_asin", ""), "brand": "X", "product_title": "P",
        "star": kw.pop("star", 1), "review_title": title, "review_body": body,
        "review_date": date, "author": author, "verified": True, "vine": False,
        "helpful": kw.pop("helpful", "0"), "variant_attributes": "", "image_url": "",
        "video_url": "", "country": "", "review_url": "", "crawl_date": "2026-10-09",
        "source_stratum": "critical",
        "fallback_key": fallback_key(author, title, body),
        "raw_occurrences": kw.pop("raw_occurrences", 1),
    }
    row.update(kw)
    return row


def test_same_review_within_one_source_is_deduped():
    rows = [_row("pangolinfo", review_id="R1AAAAA00001"), _row("pangolinfo", review_id="R1AAAAA00001")]
    unique, stats = dedupe_reviews(rows)
    assert len(unique) == 1
    assert stats["removed"] == 1
    assert unique[0]["raw_occurrences"] == 2


def test_cross_source_merge_by_primary_key():
    rows = [_row("pangolinfo", review_id="R1AAAAA00001", helpful="3"),
            _row("sellersprite", review_id="R1AAAAA00001", helpful="9")]
    unique, stats = dedupe_reviews(rows)
    assert len(unique) == 1
    assert sorted(unique[0]["sources"]) == ["pangolinfo", "sellersprite"]
    assert stats["cross_source_merges"] == 1


def test_cross_source_merge_when_one_side_has_no_id():
    """卖家精灵无 reviewId: 必须靠兜底键与 Pangolinfo 的同一条评论合并(曾经的漏洞)。"""
    rows = [
        _row("pangolinfo", review_id="R1AAAAA00001", author="Ryan L.", title="Turned moldy",
             body="Turned moldy after a month of usage.", date="2026-09-19"),
        _row("sellersprite", review_id="", author="Ryan L.", title="Turned moldy",
             body="Turned moldy after a month of usage.", date="2026-09-19",
             variant_asin="B01FFD7001"),
    ]
    unique, stats = dedupe_reviews(rows)
    assert len(unique) == 1, "无 ID 源必须能被兜底键合并进有 ID 源"
    assert unique[0]["review_id"] == "R1AAAAA00001"      # 合并后补齐原生 ID
    assert unique[0]["variant_asin"] == "B01FFD7001"     # 缺失字段被补齐
    assert stats["cross_source_merges"] == 1
    assert stats["missing_id"] == 1


def test_helpful_is_never_summed():
    rows = [_row("pangolinfo", review_id="R1AAAAA00001", helpful="3"),
            _row("sellersprite", review_id="R1AAAAA00001", helpful="9")]
    unique, _ = dedupe_reviews(rows)
    assert unique[0]["helpful"] == "9", "Helpful 取较大值展示, 绝不累加"


def test_longer_body_is_kept():
    rows = [_row("pangolinfo", review_id="R1AAAAA00001", body="short"),
            _row("sellersprite", review_id="R1AAAAA00001", body="a much longer body text")]
    unique, _ = dedupe_reviews(rows)
    assert unique[0]["review_body"] == "a much longer body text"


def test_distinct_reviews_stay_separate():
    rows = [_row("pangolinfo", review_id="R1AAAAA00001", author="A", title="t1"),
            _row("sellersprite", review_id="", author="B", title="t2")]
    unique, stats = dedupe_reviews(rows)
    assert len(unique) == 2
    assert stats["removed"] == 0
    assert stats["missing_id"] == 1


def test_duplicate_keys_are_counted():
    rows = [_row("pangolinfo", review_id="R1AAAAA00001"),
            _row("sellersprite", review_id="R1AAAAA00001")]
    _, stats = dedupe_reviews(rows)
    assert stats["duplicate_keys"] >= 1


def test_no_id_rows_one_day_apart_merge_but_months_apart_do_not():
    """兜底键不含日期 → 用日期容差(≤2 天)保护: 跨源 1 天漂移要并, 隔月同文本不能并。"""
    near = [_row("pangolinfo", review_id="", author="A", title="t", body="b", date="2026-09-19"),
            _row("sellersprite", review_id="", author="A", title="t", body="b", date="2026-09-18")]
    unique, stats = dedupe_reviews(near)
    assert len(unique) == 1 and stats["cross_source_merges"] == 1

    far = [_row("pangolinfo", review_id="", author="A", title="t", body="b", date="2026-01-05"),
           _row("sellersprite", review_id="", author="A", title="t", body="b", date="2026-07-05")]
    unique2, stats2 = dedupe_reviews(far)
    assert len(unique2) == 2, "同文本但相隔数月必须视为两条评论"
    assert stats2["date_conflicts"] == 1
