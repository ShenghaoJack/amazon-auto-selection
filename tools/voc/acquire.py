# -*- coding: utf-8 -*-
"""VOC 采集计划与"额度耗尽"判定。

页数预算(用户口径): **每个父体 5 页**, 分层 critical(1–3★) 3 页 + four_star 1 页 + five_star 1 页。
实现要点(2026-10-09 修): 先抓 1 页 critical 作**探针**(父体常无评论, 需据此决定是否回退到子体),
探针已消耗 critical 的 1 页, 因此命中后的剩余计划里 critical 只能再抓 **2 页** ——
否则第 1 页被重复抓取, 每家族多花 1 页积分并产生 10 条重复评论。
"""
from __future__ import annotations

import re

DEFAULT_TOTAL_PAGES = 5
# (stratum, pangolinfo filterByStar, 卖家精灵 starList, 满额页数)
STRATA = [
    ("critical", "critical", [1, 2, 3], 3),
    ("four_star", "four_star", [4], 1),
    ("five_star", "five_star", [5], 1),
]
PROBE_STRATUM = "critical"
PROBE_PAGES = 1

RATE_HINTS = ("rate_limit", "rate limit", "too many requests", "reduce qps", "限流", "请求过快", "请求频率过高")


def looks_minute_limited(*texts) -> bool:
    blob = " ".join(str(t or "") for t in texts).lower()
    return ("error_maximum_access_per_minute" in blob
            or bool(re.search(r"每分钟.{0,12}(?:上限|限额)", blob))
            or bool(re.search(r"(?:maximum|limit).{0,30}per[ _-]minute|per[ _-]minute.{0,30}(?:maximum|exceed|limit)", blob)))


def looks_rate_limited(*texts) -> bool:
    blob = " ".join(str(t or "") for t in texts).lower()
    return (looks_minute_limited(*texts) or any(str(t).strip() == "429" for t in texts)
            or any(h in blob for h in RATE_HINTS))


def full_plan() -> list[tuple[str, str, list[int], int]]:
    """满额计划(探针 + 剩余), 合计 DEFAULT_TOTAL_PAGES 页。"""
    return [(s, p, stars, pages) for s, p, stars, pages in STRATA]


def remaining_plan() -> list[tuple[str, str, list[int], int]]:
    """探针(critical 1 页)已执行后的剩余计划; critical 减 1 页。"""
    out = []
    for stratum, p_filter, stars, pages in STRATA:
        left = pages - PROBE_PAGES if stratum == PROBE_STRATUM else pages
        if left > 0:
            out.append((stratum, p_filter, stars, left))
    return out


def planned_pages() -> int:
    """一个父体的计划总页数(探针 + 剩余)。"""
    return PROBE_PAGES + sum(x[3] for x in remaining_plan())


def looks_exhausted(*texts) -> bool:
    """Require an explicit balance/quota failure; rate limits never disable a source."""
    blob = " ".join(str(t or "") for t in texts).lower()
    if looks_rate_limited(*texts):
        return False
    resource = r"(?:points?|credits?|balance|quota|funds|积分|余额|额度|配额)"
    depleted = r"(?:insufficient|exhausted|depleted|exceeded|empty|不足|耗尽|用完|超限|已用尽)"
    return bool(re.search(resource + r"[^\n]{0,60}" + depleted, blob)
                or re.search(depleted + r"[^\n]{0,60}" + resource, blob)
                or re.search(r"(?:no|zero) remaining " + resource, blob)
                or "请充值" in blob or "please recharge" in blob)


def stratum_of(name: str) -> tuple[str, list[int]]:
    """stratum 名 → (pangolinfo filter, 卖家精灵 starList)。"""
    for stratum, p_filter, stars, _ in STRATA:
        if stratum == name:
            return p_filter, stars
    raise KeyError(name)
