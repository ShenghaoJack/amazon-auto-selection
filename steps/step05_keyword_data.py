"""FR-05 关键词挖词(三 MCP 接口)。

输入: 大词候选 <CAT>-keywords-candidates.txt(最多 CANDIDATE_LIMIT 个默认 20)
      + **人工核心词清单** `filter-<CAT>-core-words.txt`(2026-09-29 起一并挖, 见下)
输出: 关键词指标数据 <CAT>-amazon-keyword-data.json
      (每词一条: 站内月搜索量/站内同比/购买量/购买率/环比/3月增长/CPC/CPC下限/CPC上限/
       周搜索量/搜索排名/Google 月度指数序列/数据状态)

逻辑: 逐词调数据平台 MCP 三接口(关键词趋势/ABA 周度竞价/Google 趋势)
      -> 单接口失败不影响其他(记录错误信息) -> 结果合并写 JSON(缺失字段留空)
容错: 候选词为空 -> 告警并写出空数组(仍写文件保证 DONE 校验)
断点: 已存在的记录(按 keyword)跳过, 只挖新词

**为什么要挖人工核心词(2026-09-29)**: webui「关键词清单」面板允许人工加词(手填新词、
不达标词), 但那些词不在 FR-04 候选里 → FR-05 不会挖它们 → 步骤8 关键词报告里它们的
指标列全是空的(判定列标"人工新增(未判定)")。把人工核心词并入待挖清单后, 重跑
步骤5 → 7 → 8 就能得到**完整指标的关键词报告**(每词 4 次 MCP 调用; 断点续挖保证
只对真正的新词花配额)。
"""
from __future__ import annotations

import os

from core.errors import StepDataError
from core.io_utils import read_json, read_txt_lines, write_json
from core.keyword_lists import core_manual_name, read_words
from core.mcp import throttle
from core.validation import warn_missing
from steps.base import BaseStep, StepContext


class Step05KeywordData(BaseStep):
    number = 5
    title = "关键词挖词"
    description = "FR-05: 三 MCP 接口逐词挖取关键词指标数据"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        if not ctx.config.trend_month:
            raise ValueError("配置缺少 TREND_MONTH")

    @property
    def artifacts(self) -> list[str]:
        return [f"{self._cat}-amazon-keyword-data.json"]

    def validate_artifacts(self, ctx: StepContext) -> None:
        """断点保护: 产物全部为 error 记录时视为不合格, 要求重新挖词。"""
        out = ctx.resolve_artifact(self.artifacts[0])
        if not os.path.exists(out):
            return
        rows = read_json(out, None)
        if isinstance(rows, list) and rows:
            if all(isinstance(r, dict) and r.get("status") == "error" for r in rows):
                raise StepDataError("关键词挖词产物全部为 error 记录, 需要重新挖词")

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        trend_month = ctx.config.trend_month
        out_path = ctx.resolve_artifact(self.artifacts[0])
        self.log(ctx, f"开始关键词挖词: CAT={cat} 目标月={trend_month}")
        # 站点自证(2026-09-15): MCP 的 marketplace 由配置 SITE 决定(SITE→国家码), 但日志
        # 原先不打印它 —— 跑非 US 站时无法从日志核对"挖的到底是哪个站的关键词库"。
        # 这里把解析结果与 SITE 一起打出来(实测 US/UK/DE 返回的搜索量确实不同, 参数真实生效)。
        from core.mcp import _marketplace as _mp
        from core.mcp import is_known_site as _known
        _site = str(ctx.config.get("SITE", "") or "")
        self.log(ctx, f"挖词站点: SITE={_site or '(空)'} → marketplace={_mp(_site)}"
                      f"(步骤5/6 走 MCP 站点; 步骤0/1/8/9/10 走 MARKET={ctx.config.market})")
        if not _known(_site):
            # 认不出的站点会被静默当作美国站 —— 必须显式告警, 否则用户以为在挖德/法站,
            # 实际拿到的是美国库数据(2026-09-15 实测: '德国站'/'Germany' 等写法都会回落 US)。
            self.log(ctx, f"⚠ 站点名无法识别: SITE={_site!r} → 已按 **US(美国站)** 取数; "
                          f"若你要挖其它站点, 请把 SITE 改成这些之一: "
                          f"美国/英国/德国/法国/意大利/西班牙/加拿大/日本/印度/墨西哥/巴西/澳大利亚/阿联酋"
                          f"(或直接写国家码 us/uk/de/fr/it/es/ca/jp/in/mx/br/au/ae)")
        if not ctx.dry_run:
            self.check_mcp_key(ctx)

        candidates = self._load_candidates(ctx, cat, out_path)
        if candidates is None:
            return

        existing = read_json(out_path, [])
        if not isinstance(existing, list):
            self.log(ctx, "既有产物格式异常(非数组), 视为空重新挖取")
            existing = []
        existing_by_kw = {}
        for rec in existing:
            if isinstance(rec, dict) and rec.get("keyword"):
                existing_by_kw[str(rec["keyword"])] = rec
        new_words = [
            kw
            for kw in candidates
            if kw not in existing_by_kw or existing_by_kw[kw].get("status") == "error"
        ]
        est_calls = len(new_words) * 4      # 每词 4 次 MCP: 趋势 / ABA / CPC(keyword_miner) / Google
        self.log(
            ctx,
            f"待挖清单 {len(candidates)} 个(FR-04 候选 + 人工核心词, 已去重), "
            f"已有 {len(existing_by_kw)} 个, 本次待挖 {len(new_words)} 个 → 约 {est_calls} 次 MCP 调用",
        )
        if len(new_words) > 30:
            self.log(ctx, f"⚠ 本次待挖 {len(new_words)} 个词(约 {est_calls} 次 MCP 调用), 数量偏多: "
                          f"若只人工加了几个词, 先检查人工核心词清单是不是贴多了; "
                          f"断点续挖会跳过已有词, 中断后再跑可继续")

        new_by_kw = {}
        for idx, kw in enumerate(new_words):
            new_by_kw[kw] = self._mine_one(ctx, kw, trend_month)
            if idx < len(new_words) - 1:
                throttle(1.0)

        records = []
        for kw in candidates:
            rec = existing_by_kw.get(kw)
            if rec is None or rec.get("status") == "error":
                rec = new_by_kw.get(kw) or rec
            elif (rec.get("google_evidence") or {}).get("target_month") != trend_month or (rec.get("google_evidence") or {}).get("kind") != "target_month":
                self._google_evidence(ctx, rec, kw, trend_month, refresh_monthly=True)
            records.append(rec)

        write_json(out_path, records)
        error_n = sum(1 for r in records if r.get("status") == "error")
        self.log(
            ctx,
            f"挖词完成: 共 {len(records)} 条, 新增 {len(new_by_kw)} 条, "
            f"error {error_n} 条, 产物: {out_path}",
        )

    def _load_candidates(self, ctx: StepContext, cat: str, out_path: str):
        """待挖词 = FR-04 候选词 ∪ **人工核心词清单**(2026-09-29)。

        人工清单里的词(手填新词/不达标词)不在候选里, 但用户要它们在关键词报告里有指标,
        所以并入一起挖; 断点续挖(按 keyword 跳过已有记录)保证只对**新词**花 MCP 调用。
        两者都为空时写空数组并返回 None(保持旧行为)。
        """
        cand_path = ctx.resolve_artifact(f"{cat}-keywords-candidates.txt")
        cands: list[str] = []
        if os.path.exists(cand_path):
            cands = _dedup(read_txt_lines(cand_path))
        else:
            warn_missing(cand_path, f"{cat}-keywords-candidates.txt")

        manual_path = os.path.join(str(ctx.paths.cat_root), core_manual_name(cat))
        manual = read_words(manual_path)
        words = _dedup(cands + manual)
        if manual:
            dup = len(cands) + len(manual) - len(words)
            self.log(ctx, f"人工核心词清单 {len(manual)} 个(其中 {dup} 个已在候选里) → 一并挖词: "
                          f"{manual_path}")
        if not words:
            self.log(ctx, "候选词与人工核心词清单均为空, 写出空数组")
            write_json(out_path, [])
            return None
        return words

    def _mine_one(self, ctx: StepContext, kw: str, trend_month: str) -> dict:
        """挖取单个词的三接口数据, 单接口失败不影响其他。"""
        rec = {
            "keyword": kw,
            "monthly_search_volume": None,
            "mom_yoy": None,
            "purchase_volume": None,
            "purchase_rate": None,
            "mom_change": None,
            "growth_3m": None,
            "cpc": None,
            "cpc_min": None,
            "cpc_max": None,
            "weekly_search_volume": None,
            "search_rank": None,
            "google_monthly_index": {},
            "status": "ok",
            "error": "",
        }
        errors = []
        try:
            self.log(ctx, f"  挖词[{kw}] keyword_trend...")
            trend = ctx.mcp().keyword_trend(kw, trend_month)
            if trend.get("status") == "error":
                errors.append(f"keyword_trend: {trend.get('error') or 'status=error'}")
            else:
                rec["monthly_search_volume"] = trend.get("search_volume")
                rec["mom_yoy"] = trend.get("mom_yoy")
                rec["purchase_volume"] = trend.get("purchase_volume")
                rec["purchase_rate"] = trend.get("purchase_rate")
                rec["mom_change"] = trend.get("mom_change")
                rec["growth_3m"] = trend.get("growth_3m")
        except Exception as exc:
            errors.append(f"keyword_trend: {exc}")
        try:
            self.log(ctx, f"  挖词[{kw}] aba_weekly_bid...")
            aba = ctx.mcp().aba_weekly_bid(kw, trend_month)
            if aba.get("status") == "error":
                errors.append(f"aba_weekly_bid: {aba.get('error') or 'status=error'}")
            else:
                rec["cpc"] = aba.get("cpc")
                rec["cpc_min"] = aba.get("cpc_min")
                rec["cpc_max"] = aba.get("cpc_max")
                rec["weekly_search_volume"] = aba.get("weekly_search_volume")
                rec["search_rank"] = aba.get("search_rank")
        except Exception as exc:
            errors.append(f"aba_weekly_bid: {exc}")
        try:
            self.log(ctx, f"  挖词[{kw}] google_trend...")
            goog = ctx.mcp().google_trend(kw)
            if goog.get("status") == "error":
                errors.append(f"google_trend: {goog.get('error') or 'status=error'}")
            else:
                index_map = goog.get("google_monthly_index") or {}
                if isinstance(index_map, dict):
                    rec["google_monthly_index"] = index_map
        except Exception as exc:
            errors.append(f"google_trend: {exc}")
        self._google_evidence(ctx, rec, kw, trend_month)
        if len(errors) == 3:
            rec["status"] = "error"
        if errors:
            rec["error"] = "; ".join(errors)
        return rec

    def _google_evidence(self, ctx, rec, kw, trend_month, refresh_monthly=False):
        from core.google_trends_fallback import resolve_google_trend
        if refresh_monthly:
            try:
                result = ctx.mcp().google_trend(kw)
                if result.get("status") != "error" and isinstance(result.get("google_monthly_index"), dict):
                    rec["google_monthly_index"] = result["google_monthly_index"]
            except Exception as exc:
                self.log(ctx, f"  挖词[{kw}] Google 月趋势刷新失败：{type(exc).__name__}")
        monthly = rec.get("google_monthly_index") or {}
        evidence = resolve_google_trend(monthly, {}, trend_month)
        if evidence["kind"] != "target_month":
            try:
                weekly = ctx.mcp().google_trend_weekly(kw).get("google_weekly_index") or {}
                rec["google_weekly_index"] = weekly
                evidence = resolve_google_trend(monthly, weekly, trend_month)
            except Exception as exc:
                self.log(ctx, f"  挖词[{kw}] Google 周趋势获取失败：{type(exc).__name__}；尝试最近可用月。")
        evidence["target_month"] = trend_month
        evidence["source"] = "卖家精灵 Google Trends"
        rec["google_evidence"] = evidence
        if evidence["kind"] != "target_month":
            self.log(ctx, f"  挖词[{kw}] Google 趋势兜底：{evidence['kind']}，范围 {evidence['period'] or '无'}；同比 {'可计算' if evidence['yoy'] is not None else '不足'}。")


def _dedup(words: list[str]) -> list[str]:
    """按出现顺序去重。"""
    seen = set()
    out = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out
