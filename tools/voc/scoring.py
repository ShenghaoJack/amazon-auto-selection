# -*- coding: utf-8 -*-
"""VOC 评分(对齐 skill taxonomy-and-scoring 的公式与硬性规则)。

正向驱动分 = 20×(0.30P + 0.20B + 0.20R + 0.20V + 0.10C)
负向优先级分 = 20×(0.25F + 0.30S + 0.25H + 0.10T + 0.10C) → P0–P3

硬性规则(原先漏实现, 2026-10-09 补):
  · 可信的 A/B 级安全问题**不得低于 P1**;
  · 仅 D 级(传闻)不得单独产生 P0/P1;
  · T 在"评论数<30/期 或 覆盖<120 天"时固定为 3(数据不足)。
"""
from __future__ import annotations

from .taxonomy import ROLE_STRENGTH, RECOMMEND_ROLES, SAFETY_HIGH


def lvl5(share: float) -> int:
    """频次档位(正向提及率 P / 负向问题率 F / 推荐强度 R 共用阈值)。"""
    if share >= 0.20:
        return 5
    if share >= 0.10:
        return 4
    if share >= 0.05:
        return 3
    if share >= 0.02:
        return 2
    return 1


def conf_level(n: int) -> int:
    """证据置信 C: ≥30→5, 15–29→4, 8–14→3, 3–7→2, <3→1。"""
    if n >= 30:
        return 5
    if n >= 15:
        return 4
    if n >= 8:
        return 3
    if n >= 3:
        return 2
    return 1


def breadth_level(parents: int, total_parents: int) -> int:
    """广度 B: ≥10 父体或 ≥50% 样本→5; 6–9→4; 3–5→3; 2→2; 1→1。"""
    if parents >= 10 or (total_parents and parents / total_parents >= 0.5):
        return 5
    if parents >= 6:
        return 4
    if parents >= 3:
        return 3
    if parents == 2:
        return 2
    return 1


def role_strength(role: str) -> int:
    """证据角色 → 强度 V(未知角色按 experienced_benefit 的 4 处理)。"""
    return ROLE_STRENGTH.get(role, 4)


def mean_strength(roles: list[str]) -> int:
    """一组证据角色的平均强度(四舍五入), 用作 V。空集合返回 1。"""
    if not roles:
        return 1
    return max(1, min(5, int(round(sum(role_strength(r) for r in roles) / len(roles)))))


def recommend_share(roles: list[str]) -> float:
    """R 的分子口径: 该属性正向证据中含推荐/复购/换机信号的比例。"""
    if not roles:
        return 0.0
    return sum(1 for r in roles if r in RECOMMEND_ROLES) / len(roles)


def positive_driver_score(P: int, B: int, R: int, V: int, C: int) -> float:
    """正向驱动分(0–100)。"""
    return round(20 * (0.30 * P + 0.20 * B + 0.20 * R + 0.20 * V + 0.10 * C), 1)


def positive_verdict(score: float) -> str:
    if score >= 80:
        return "核心差异点—保留并放大"
    if score >= 65:
        return "强价值驱动—重点呈现"
    if score >= 50:
        return "辅助价值—分场景验证"
    return "新兴/低显著—持续观察"


def safety_h(grades: list[str]) -> int:
    """安全项 H: 可信 A/B→5; C→3; 仅 D 或无→1。"""
    g = {str(x).upper() for x in grades if x}
    if g & SAFETY_HIGH:
        return 5
    if "C" in g:
        return 3
    return 1


def negative_priority_score(F: int, S: int, H: int, T: int, C: int) -> float:
    """负向优先级分(0–100)。"""
    return round(20 * (0.25 * F + 0.30 * S + 0.25 * H + 0.10 * T + 0.10 * C), 1)


def priority_of(score: float, grades: list[str] | None = None) -> str:
    """分数 → P0–P3, 并应用安全等级硬性规则。"""
    pri = "P0" if score >= 80 else "P1" if score >= 65 else "P2" if score >= 50 else "P3"
    g = {str(x).upper() for x in (grades or []) if x}
    if g & SAFETY_HIGH and pri in ("P2", "P3"):
        pri = "P1"                       # 可信 A/B 级不得低于 P1
    if not (g & SAFETY_HIGH) and "C" not in g and "D" in g and pri in ("P0", "P1"):
        pri = "P2"                       # 仅 D 级(传闻)不得单独产生 P0/P1
    return pri


def is_p0_or_p1(pri: str) -> bool:
    return pri in ("P0", "P1")
