# -*- coding: utf-8 -*-
"""VOC 去重: 主键 + 兜底键索引 + **日期容差校验**, 支持跨源合并与来源溯源。

为什么要两级索引(2026-10-09 修的缺陷):
  Pangolinfo 有 Amazon 原生 `reviewId`, 卖家精灵**没有 ID**。若只用单一键
  (`id:xxx` 或 `fb:xxx`), 同一条评论在两源会各占一行、永远合不到一起。

为什么兜底键不含日期、但要额外做日期容差校验:
  实测同一条评论, Pangolinfo 给 "September 19, 2026", 卖家精灵给 epoch
  1789758000000(= 2026-09-18 19:00 UTC) —— **两源日期可差 1 天**。若把日期写进键,
  跨源永远合不上; 但若完全不看日期, 同一位作者在不同月份发的"同标题同正文"评论
  (例如复制粘贴的短评) 又会被误并。
  兜底键 = sha1(作者|标题|完整正文), 命中后再比较日期, 相差 ≤2 天(或任一侧无日期)
  才视为同一条评论。

合并规则(对齐 skill data-contract):
  · sources 取并集; raw_occurrences 累加;
  · 保留更完整的正文; 缺 reviewId 的一侧被有 ID 的一侧补齐;
  · **helpful/likes 绝不累加**(取较大值, 仅作展示, 不参与任何求和)。
"""
from __future__ import annotations

from datetime import date

DATE_TOLERANCE_DAYS = 2


def _keys_of(row: dict) -> list[str]:
    keys = []
    rid = str(row.get("review_id") or "").strip().upper()
    if rid:
        keys.append("id:" + rid)
    return keys


def date_compatible(a: str, b: str, tolerance: int = DATE_TOLERANCE_DAYS) -> bool:
    """两日期是否兼容(任一侧缺失 → 兼容; 否则相差不超过 tolerance 天)。"""
    if not a or not b:
        return True
    try:
        da, db = date.fromisoformat(str(a)), date.fromisoformat(str(b))
    except ValueError:
        return True
    return abs((da - db).days) <= tolerance


def _helpful_max(a, b) -> str:
    try:
        return str(max(int(str(a or 0) or 0), int(str(b or 0) or 0)))
    except (TypeError, ValueError):
        return str(a or b or "")


def dedupe_reviews(rows: list[dict]) -> tuple[list[dict], dict]:
    """返回 (唯一评论列表, 统计)。"""
    by_id: dict[str, dict] = {}
    fb_index: dict[str, list[dict]] = {}
    unique: list[dict] = []
    stats = {"raw": len(rows), "unique": 0, "removed": 0, "missing_id": 0,
             "cross_source_merges": 0, "duplicate_keys": 0, "date_conflicts": 0}

    for row in rows:
        if not str(row.get("review_id") or "").strip():
            stats["missing_id"] += 1
        fb = str(row.get("fallback_key") or "").strip()
        hit = None
        for k in _keys_of(row):
            hit = by_id.get(k)
            if hit is not None:
                stats["duplicate_keys"] += 1
                break
        date_checked = False
        if hit is None and fb:
            for prev in fb_index.get(fb, []):
                if prev.get("review_id") and row.get("review_id") and prev["review_id"] != row["review_id"]:
                    continue
                if date_compatible(prev.get("review_date"), row.get("review_date")):
                    hit = prev
                    date_checked = True
                    break
            if hit is None and fb_index.get(fb):
                stats["date_conflicts"] += 1        # 同文本但日期相差过大 → 判为不同评论
        if hit is None:
            new = dict(row)
            new["sources"] = [row.get("source")] if row.get("source") else []
            new["raw_occurrences"] = int(row.get("raw_occurrences") or 1)
            for k in _keys_of(new):
                by_id[k] = new
            if fb:
                fb_index.setdefault(fb, []).append(new)
            unique.append(new)
            continue

        # ---- 合并
        stats["removed"] += 1
        for k in _keys_of(row):
            by_id.setdefault(k, hit)
        if fb and hit not in fb_index.get(fb, []):
            fb_index.setdefault(fb, []).append(hit)
        src = row.get("source")
        if src and src not in hit["sources"]:
            hit["sources"].append(src)
            stats["cross_source_merges"] += 1
        hit["raw_occurrences"] = int(hit.get("raw_occurrences") or 1) + int(row.get("raw_occurrences") or 1)
        if not hit.get("review_id") and row.get("review_id"):
            hit["review_id"] = row["review_id"]
            hit["review_url"] = row.get("review_url") or ""
        if len(str(row.get("review_body") or "")) > len(str(hit.get("review_body") or "")):
            hit["review_body"] = row["review_body"]
        def confirmed(item):
            return bool(item.get("variant_asin")) and (item.get("asin_role") == "attributed"
                or (item.get("source") == "pangolinfo" and item.get("asin_role") not in {"unknown", "conflict"}))
        if hit.get("asin_role") == "conflict" or row.get("asin_role") == "conflict" or (
                confirmed(hit) and confirmed(row) and hit["variant_asin"] != row["variant_asin"]):
            candidates = set(hit.get("identity_candidates", [])) | set(row.get("identity_candidates", []))
            candidates.update(str(item.get("variant_asin")) for item in (hit, row) if confirmed(item))
            hit.update(variant_asin="", asin_role="conflict", attribution_basis="duplicate_review_variant_conflict",
                       identity_candidates=sorted(candidates), asin_status="冲突", asin_recovery_status="待核对")
        elif confirmed(row) and not confirmed(hit):
            for field in ("variant_asin", "parent_asin", "brand", "asin_role", "attribution_basis"):
                hit[field] = row.get(field, "attributed" if field == "asin_role" else "")
            hit.update(asin_status="已补齐", asin_recovery_status="已通过评论去重匹配补齐")
        elif not hit.get("variant_asin") and row.get("variant_asin") and hit.get("asin_role") != "conflict":
            hit["variant_asin"] = row["variant_asin"]
        for field in ("variant_attributes", "brand", "product_title",
                      "country", "image_url", "video_url", "review_date", "author"):
            if not hit.get(field) and row.get(field):
                hit[field] = row[field]
        hit["helpful"] = _helpful_max(hit.get("helpful"), row.get("helpful"))
        hit["verified"] = bool(hit.get("verified")) or bool(row.get("verified"))
        hit["vine"] = bool(hit.get("vine")) or bool(row.get("vine"))
        _ = date_checked

    stats["unique"] = len(unique)
    return unique, stats
