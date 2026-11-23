"""FR-06 站内月度趋势。

输入: 关键词文件 <CAT>-keywords.txt([核心大词] = 全部大词候选, 2026-09 不再固定 5 个;
      缺失 -> 回退候选词文件或跳过)
输出: 站内月度序列数据 <CAT>-amazon-monthly.json
      {keyword: [{month, search_volume, purchase_volume, purchase_rate, yoy}]}

逻辑: 读取核心大词(全部候选) -> 逐词调 MCP 关键词趋势接口拉完整历史月度序列
      -> 每词间隔节流 -> 写 JSON
容错: 核心大词为空 -> 写空 JSON 并告警; 单词失败 -> 跳过该词继续;
      全部核心大词失败 -> 抛异常阻断(禁止静默写出空数据标成功)
"""
from __future__ import annotations

import os
import time

from core.errors import StepFailedError
from core.io_utils import read_json, read_txt_lines, write_json
from core.keyword_lists import manual_first
from core.mcp import throttle
from core.validation import warn_missing
from steps.base import BaseStep, StepContext


class Step06Trend(BaseStep):
    number = 6
    title = "站内月度趋势"
    description = "FR-06: 核心大词历史月度序列"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        if not ctx.config.trend_month:
            raise ValueError("配置缺少 TREND_MONTH")

    @property
    def artifacts(self) -> list[str]:
        return [f"{self._cat}-amazon-monthly.json"]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        trend_month = ctx.config.trend_month
        out_path = ctx.resolve_artifact(self.artifacts[0])
        self.log(ctx, f"开始拉取站内月度趋势: CAT={cat} 目标月={trend_month}")

        # 关键词三态(2026-09-29): 人工清单优先 —— 趋势要与步骤9 实际搜索用同一批词。
        auto_core = self._auto_core_words(ctx, cat)
        core_words, src = manual_first(ctx.paths.cat_root, cat, "core", auto_core)
        if core_words:
            self.log(ctx, f"核心词来源={src}, 共 {len(core_words)} 个")
        if not core_words:
            self.log(ctx, "核心大词为空, 写出空 JSON 并告警")
            write_json(out_path, {})
            return

        # 2026-11-22 优化: 优先复用步骤5 已拉取的同目标月站内月度序列, 避免重复消耗 MCP 与 60s 冷却。
        reused = self._load_step5_series(ctx, cat, trend_month)
        missing = [kw for kw in core_words if kw not in reused]
        reused_n = len(core_words) - len(missing)
        if reused_n:
            self.log(
                ctx,
                f"直接复用步骤5 站内月度序列 {reused_n}/{len(core_words)} 个"
                + ("(全部命中, 跳过冷却与重复 MCP 调用)" if not missing else f", 仅补拉剩余 {len(missing)} 个"),
            )

        if missing and not ctx.dry_run:
            self.check_mcp_key(ctx)
            self._cooldown_after_step5(ctx)

        fetched = {}
        failed = 0
        for idx, kw in enumerate(missing):
            try:
                trend = ctx.mcp().keyword_trend(kw, trend_month)
                actual_month = trend.get("month")
                if actual_month and str(actual_month).replace("-", "") != str(trend_month).replace("-", ""):
                    self.log(ctx, f"{kw}: 站内趋势目标月 {trend_month} 无数据，目标指标改用最新有数据月份 {actual_month}；月度序列保留各自月份。")
                series = _normalize_series(trend.get("monthly_series") or [])
                if series:
                    fetched[kw] = series
                else:
                    failed += 1
                    self.log(ctx, f"{kw}: 月度序列为空, 跳过")
            except Exception as exc:
                failed += 1
                self.log(ctx, f"{kw}: 趋势获取失败: {exc}")
            if idx < len(missing) - 1:
                throttle(1.0)

        result = {}
        for kw in core_words:
            if kw in reused:
                result[kw] = reused[kw]
            elif kw in fetched:
                result[kw] = fetched[kw]

        if not result and failed == len(core_words) and core_words:
            raise StepFailedError(f"全部 {len(core_words)} 个核心大词趋势获取失败")

        write_json(out_path, result)
        self.log(ctx, f"月度趋势已写入: {out_path}, 词数 {len(result)}")

    def _load_step5_series(self, ctx: StepContext, cat: str, trend_month: str) -> dict[str, list[dict]]:
        """从步骤5 产物中复用同目标月的站内月度序列, 避免步骤6 重复调 keyword_trend。"""
        step5_path = ctx.resolve_artifact(f"{cat}-amazon-keyword-data.json")
        if not os.path.exists(step5_path):
            return {}
        rows = read_json(step5_path, [])
        if not isinstance(rows, list):
            return {}
        want_month = str(trend_month or "").replace("-", "").strip()
        out: dict[str, list[dict]] = {}
        for rec in rows:
            if not isinstance(rec, dict) or rec.get("status") == "error":
                continue
            kw = str(rec.get("keyword") or "").strip()
            if not kw:
                continue
            req_month = str(rec.get("station_trend_requested_month") or "").replace("-", "").strip()
            if req_month and want_month and req_month != want_month:
                continue
            raw_series = rec.get("station_monthly_series")
            if isinstance(raw_series, list) and raw_series:
                norm = _normalize_series(raw_series)
                if norm:
                    out[kw] = norm
        return out

    def _cooldown_after_step5(self, ctx: StepContext) -> None:
        """步骤5→6 冷却: 步骤5 刚在几十秒内打完数十次 MCP 调用, 卖家精灵按分钟限流
        (ERROR_MAXIMUM_ACCESS_PER_MINUTE), 紧接着拉趋势会被整体拒绝。等待
        STEP56_COOLDOWN_SECONDS 秒(默认 60)让限流窗口清空后再拉趋势。"""
        seconds = float(ctx.config.get("STEP56_COOLDOWN_SECONDS", 60))
        if seconds > 0:
            self.log(ctx, f"步骤5→6 冷却等待 {seconds:g} 秒(避开卖家精灵每分钟限流)...")
            time.sleep(seconds)

    def _auto_core_words(self, ctx: StepContext, cat: str) -> list[str]:
        """自动口径核心词: [核心大词] 分区, 缺失时回退候选词文件, 再缺失返回空列表。"""
        kw_path = ctx.resolve_artifact(f"{cat}-keywords.txt")
        if not warn_missing(kw_path, f"{cat}-keywords.txt"):
            return self._fallback_candidates(ctx, cat)
        core = _parse_core_section(read_txt_lines(kw_path))
        if core:
            return core
        self.log(ctx, "keywords.txt 缺少 [核心大词] 分区, 回退候选词文件")
        return self._fallback_candidates(ctx, cat)

    def _fallback_candidates(self, ctx: StepContext, cat: str) -> list[str]:
        cand_path = ctx.resolve_artifact(f"{cat}-keywords-candidates.txt")
        if warn_missing(cand_path, f"{cat}-keywords-candidates.txt"):
            words = read_txt_lines(cand_path)
            if words:
                self.log(ctx, f"回退使用候选词 {len(words)} 个")
                return words
        return []


def _parse_core_section(lines: list[str]) -> list[str]:
    """解析 keywords.txt 的 [核心大词] 分区(到下一个 [xxx] 分区为止)。"""
    core = []
    in_core = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("["):
            in_core = s == "[核心大词]"
            continue
        if in_core:
            core.append(s)
    return core


def _normalize_series(series) -> list[dict]:
    """把接口返回的月度序列规整为契约字段, 兼容中英文键名。"""
    out = []
    for item in series:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "month": _pick(item, "month", "月份"),
                "search_volume": _pick(item, "search_volume", "搜索量"),
                "purchase_volume": _pick(item, "purchase_volume", "购买量"),
                "purchase_rate": _pick(item, "purchase_rate", "购买率"),
                "yoy": _pick(item, "yoy", "同比"),
            }
        )
    return out


def _pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None
