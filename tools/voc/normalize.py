# -*- coding: utf-8 -*-
"""VOC 字段归一: 把两源(Pangolinfo / 卖家精灵)的评论记录映射为统一 canonical 行。

两源字段差异(2026-10-09 实测):
  Pangolinfo  reviewId(=Amazon 原生 ID) / star="1.0 out of 5 stars" / date="September 19, 2026"
              / attributes=[{Size:...}] / purchased / vineVoice / helpful / country
  卖家精灵     **无 ID** / star=1 / date=1789758000000(epoch ms) / skus=["Size: ..."]
              / verified / vine / likes
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

# 证据库/报告使用的列(顺序即写入工作簿的顺序)
CANON_COLUMNS = [
    "review_id", "parent_asin", "variant_asin", "brand", "product_title", "star",
    "review_title", "review_body", "review_date", "author", "verified", "vine",
    "helpful", "variant_attributes", "image_url", "video_url", "country",
    "review_url", "crawl_date", "source", "source_stratum", "sources",
    "raw_occurrences", "fallback_key", "fallback_key_strict",
    "queried_asin", "asin_role", "attribution_basis",
    "asin_status", "asin_recovery_status",
]


def norm_text(s) -> str:
    """文本归一: 压缩空白 + 小写 + 去掉结尾省略号/Read more。"""
    s = re.sub(r"\s+", " ", str(s or "")).strip().lower()
    return re.sub(r"(read more|…|\.\.\.)+$", "", s).strip()


def to_iso_date(v) -> str:
    """'September 19, 2026' / 1789758000000(ms) / '2026-09-19' → 'YYYY-MM-DD'。"""
    if v in (None, ""):
        return ""
    text = str(v).strip()
    if isinstance(v, (int, float)) or text.isdigit():
        try:
            ts = float(v)
            if ts > 1e11:            # 毫秒
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if m and m.group(1).lower() in MONTHS:
        return "%s-%02d-%02d" % (m.group(3), MONTHS[m.group(1).lower()], int(m.group(2)))
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    return m2.group(0) if m2 else ""


def parse_star(v) -> int:
    """'1.0 out of 5 stars' / 1 → 1。非法 → 0(调用方应据此拦截或标记)。"""
    m = re.search(r"(\d(?:\.\d)?)", str(v or ""))
    if not m:
        return 0
    try:
        return int(round(float(m.group(1))))
    except ValueError:
        return 0


def attrs_text(v) -> str:
    """[{Size:'2-Piece'},{Color:'Brown'}] / ['Size: 2-Piece'] → 'Size=2-Piece; Color=Brown'。

    两源写法不同(字典 vs "键: 值" 字符串), 这里统一为 `键=值` 并去掉分隔符两侧空白,
    保证同一款变体在两源得到完全相同的字符串。
    """
    if not v:
        return ""
    parts: list[str] = []
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict):
                parts += ["%s=%s" % (k, val) for k, val in item.items()]
            else:
                parts.append(str(item))
    elif isinstance(v, dict):
        parts = ["%s=%s" % (k, val) for k, val in v.items()]
    else:
        parts = [str(v)]
    out = []
    for p in parts:
        p = re.sub(r"\s*[:=]\s*", "=", str(p).strip())
        if p:
            out.append(p)
    return "; ".join(out)


def fallback_key(author, title, body) -> str:
    """无 reviewId 时的跨源兜底键；使用完整正文，不按前400字合并。

    为什么不含日期(2026-10-09 实测): 同一条评论, Pangolinfo 给
    "September 19, 2026", 卖家精灵给 epoch 1789758000000(= 2026-09-18 19:00 UTC),
    **两源日期可差 1 天**。若把日期写进键, 跨源就永远合不到一起。
    日期仍会单独保存, 并用 `fallback_key_strict` 保留"严格键"供审计与同源校验。
    """
    raw = "%s|%s|%s" % (norm_text(author), norm_text(title), norm_text(body))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def fallback_key_strict(author, date_iso, title, body) -> str:
    """严格键(含日期): 用于同源内核对与去重审计, 不用于跨源合并。"""
    raw = "%s|%s|%s|%s" % (norm_text(author), date_iso, norm_text(title), norm_text(body))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def row_id(row: dict) -> str:
    """证据库/报告使用的评论ID: 有原生 reviewId 用它, 否则生成 R+9 位大写。

    生成值刻意匹配 skill 校验脚本的正则 `\\bR[A-Z0-9]{5,}\\b`, 保证"代表评论ID → 证据库"
    的交叉校验可用。
    """
    rid = str(row.get("review_id") or "").strip().upper()
    if rid:
        return rid
    return "R" + hashlib.sha1(str(row.get("fallback_key", "")).encode()).hexdigest()[:9].upper()


def normalize_record(rec: dict, meta: dict | None = None) -> dict:
    """把一条原始评论(带 _source/_request_asin/_queried_asin/_stratum)转成 canonical 行。"""
    src = rec.get("_source")
    parent = str(rec.get("_request_asin") or "").upper()
    info = (meta or {}).get(parent, {})
    explicit = {str(rec.get(k) or "").strip().upper()
                for k in ("variantAsin", "childAsin", "variant_asin", "asin", "ASIN", "variantASIN", "childASIN", "child_asin")
                if rec.get(k)}
    attributed = next(iter(explicit)) if len(explicit) == 1 else ""
    if src == "pangolinfo":
        row = {
            "review_id": str(rec.get("reviewId") or "").strip().upper(),
            "variant_asin": attributed,
            "star": parse_star(rec.get("star")),
            "review_title": str(rec.get("title") or ""),
            "review_body": str(rec.get("content") or ""),
            "review_date": to_iso_date(rec.get("date")),
            "author": str(rec.get("author") or ""),
            "verified": bool(rec.get("purchased")),
            "vine": bool(rec.get("vineVoice")),
            "helpful": str(rec.get("helpful") or ""),
            "variant_attributes": attrs_text(rec.get("attributes")),
            "image_url": attrs_text(rec.get("imgs")),
            "video_url": attrs_text(rec.get("videos")),
            "country": str(rec.get("country") or ""),
        }
    else:
        # A queried ASIN can be a parent whose reviews cover many children.
        # Only an explicit review-level child identity may enter the variant join.
        row = {
            "review_id": "",
            "variant_asin": attributed,
            "star": parse_star(rec.get("star")),
            "review_title": str(rec.get("title") or ""),
            "review_body": str(rec.get("content") or ""),
            "review_date": to_iso_date(rec.get("date")),
            "author": str(rec.get("author") or ""),
            "verified": bool(rec.get("verified")),
            "vine": bool(rec.get("vine")),
            "helpful": str(rec.get("likes") or ""),
            "variant_attributes": attrs_text(rec.get("skus")),
            "image_url": attrs_text(rec.get("images")),
            "video_url": attrs_text(rec.get("videos")),
            "country": "",
        }
    row["asin_role"] = "attributed" if row["variant_asin"] else "unknown"
    row["attribution_basis"] = "review_item_explicit_asin" if row["variant_asin"] else "source_did_not_confirm_variant"
    if len(explicit) > 1:
        row.update(asin_role="conflict", attribution_basis="review_item_asin_fields_conflict")
    row["asin_status"] = "已返回" if row["asin_role"] == "attributed" else "冲突" if row["asin_role"] == "conflict" else "缺失"
    row["asin_recovery_status"] = "无需补抓" if row["asin_role"] == "attributed" else "待跨源补抓/重试"
    row.update({
        "queried_asin": str(rec.get("_queried_asin") or rec.get("_request_asin") or "").strip().upper(),
        "parent_asin": parent,
        "brand": info.get("brand", ""),
        "product_title": info.get("title", ""),
        "source": src,
        "source_stratum": rec.get("_stratum", ""),
        "crawl_date": datetime.now().strftime("%Y-%m-%d"),
        "review_url": ("https://www.amazon.com/gp/customer-reviews/%s" % row["review_id"]) if row["review_id"] else "",
        "fallback_key": fallback_key(row["author"], row["review_title"], row["review_body"]),
        "fallback_key_strict": fallback_key_strict(row["author"], row["review_date"],
                                                   row["review_title"], row["review_body"]),
        "raw_occurrences": int(rec.get("_raw_occurrences") or 1),
    })
    return {k: row.get(k, "") for k in CANON_COLUMNS if k not in ("sources",)}
