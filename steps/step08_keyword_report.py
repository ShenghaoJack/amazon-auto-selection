"""FR-08 关键词报告(Excel 7 sheet)。

输入(容错): 关键词指标/筛选结果/过滤后榜单详情/关键词清单/月度趋势/双榜清单/详情记录
输出: <CAT>-keyword-report.xlsx
      7 sheet: 结论总览/核心词分析/季节性分析/关键词总表/长尾词/源数据/方法说明

逻辑: 汇总样本统计与开品门槛 -> 核心词指标分析(判定列三色高亮)
      -> 自然月季节性分析(3高峰/3低谷/峰谷比) -> 全候选词总表
      -> 长尾词(**只放关键词**) -> 源数据/方法说明(搜索配置等纯文字口径)
容错: 任一输入缺失仅告警, 对应 sheet 用空表或提示文字, 7 sheet 必须齐全
"""
from __future__ import annotations

import re

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from core.category_config import (
    NON_US_QUALIFIED_TOP,
    is_us_site,
    pick_top_volume_candidates,
)
from core.errors import StepFailedError
from core.io_utils import df_to_excel, read_json, read_jsonl, read_txt_lines
from core.keyword_lists import resolve as resolve_keywords
from core.validation import warn_missing
from steps.base import BaseStep, StepContext

_SHEET_NAMES = ["结论总览", "核心词分析", "季节性分析", "关键词总表", "长尾词", "源数据", "方法说明"]

_FILL_GOOD = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
_FILL_BAD = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
_FILL_WARN = PatternFill(start_color="FFC000", end_color="FFC000", fill_type="solid")
_FILL_MAP = {
    "达标": _FILL_GOOD,
    "不达标": _FILL_BAD,
    "数据不足": _FILL_WARN,
    "显著": _FILL_GOOD,
    "不显著": _FILL_BAD,
}

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})")


def _is_method_keyword(word) -> bool:
    """`搜索配置` 这类方法/口径名不是关键词 —— 规则见 `core/keyword_report_clean.py`(全仓库共用一套)。"""
    try:
        from core.keyword_report_clean import is_method_row
    except Exception:                                       # noqa: BLE001
        return str(word or "").strip() in ("搜索配置", "搜索口径", "口径", "方法", "备注")
    return is_method_row(word, "", "", 1)


class Step08KeywordReport(BaseStep):
    number = 8
    title = "关键词报告"
    description = "FR-08: 生成 7 sheet 关键词报告(结论总览/核心词分析/季节性分析/关键词总表/长尾词/源数据/方法说明)"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")

    @property
    def artifacts(self) -> list[str]:
        return [f"{self._cat}-keyword-report.xlsx"]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始生成关键词报告: CAT={cat}")
        self._trend_month = ctx.config.trend_month
        self._core_pages = max(1, int(ctx.config.get("CORE_SEARCH_PAGES", 5) or 5))
        self._tail_pages = max(1, int(ctx.config.get("LONGTAIL_SEARCH_PAGES", 4) or 4))
        inputs = self._load_inputs(ctx)
        core_words = inputs["core_words"]
        longtail_words = inputs["longtail_words"]
        screen_map = inputs["screen_map"]
        kw_map = inputs["kw_map"]
        monthly = inputs["monthly"]
        gate = int(ctx.config.get("GATE_MIN_QUALIFIED", 2))
        season_threshold = float(ctx.config.get("SEASONAL_PEAK_RATIO", 2.0))

        table_keywords = list(kw_map.keys()) or (core_words + longtail_words)
        sheets = {
            "结论总览": self._build_summary(ctx, inputs, core_words, longtail_words, screen_map, monthly, gate, season_threshold),
            "核心词分析": self._build_core_sheet(
                core_words, kw_map, screen_map, monthly, season_threshold,
                manual_core=set(getattr(inputs.get("plan"), "manual_core", []) or [])),
            "季节性分析": self._build_seasonal_sheet(monthly, season_threshold),
            "关键词总表": self._build_table_sheet(table_keywords, kw_map, screen_map),
            "长尾词": self._build_longtail_sheet(
                longtail_words,
                manual_tail=set(getattr(inputs.get("plan"), "manual_longtail", []) or []),
                auto_tail=set(getattr(inputs.get("plan"), "auto_longtail", []) or [])),
            "源数据": self._build_source_sheet(),
            "方法说明": self._build_method_sheet(),
        }
        if list(sheets.keys()) != _SHEET_NAMES:
            raise StepFailedError("关键词报告 7 sheet 组装不完整, 禁止写出")
        out_path = df_to_excel(ctx.resolve_artifact(self.artifacts[0]), sheets)
        self._apply_verdict_fills(out_path)
        self.log(ctx, f"关键词报告已写入: {out_path} (7 sheet)")

    def _load_inputs(self, ctx: StepContext) -> dict:
        cat = ctx.config.cat
        names = {
            "keyword_data": f"{cat}-amazon-keyword-data.json",
            "screen": f"{cat}-screen-results.json",
            "keywords": f"{cat}-keywords.txt",
            "monthly": f"{cat}-amazon-monthly.json",
            "bestseller": f"{cat}-bestseller-newrelease.json",
            "dual_details": f"{cat}-dual-details.jsonl",
            "dual_filtered": f"{cat}-dual-details-filtered.jsonl",
        }
        paths = {}
        missing = []
        for key, name in names.items():
            paths[key] = ctx.resolve_artifact(name)
            if not warn_missing(paths[key], name):
                missing.append(key)
        if missing:
            self.log(ctx, "输入缺失(容错降级): " + ", ".join(missing))

        keyword_data = read_json(paths["keyword_data"], default=[])
        if not isinstance(keyword_data, list):
            keyword_data = []
        screen_results = read_json(paths["screen"], default=[])
        if not isinstance(screen_results, list):
            screen_results = []
        monthly = read_json(paths["monthly"], default={})
        if not isinstance(monthly, dict):
            monthly = {}
        bestseller = read_json(paths["bestseller"], default={})
        if not isinstance(bestseller, dict):
            bestseller = {}
        core_words, longtail_words = parse_keywords_file(read_txt_lines(paths["keywords"]))
        screen_map = _norm_screen(screen_results)
        # 本模块的 screen_map 是 {词: {verdict, reason}}, 而口径层要的是扁平 {词: verdict}
        verdict_flat = {w: str(v.get("verdict") or "") for w, v in screen_map.items()}
        # 关键词三态(2026-09-29): 报告必须与步骤9 **同一口径** —— 否则会出现
        # "报告显示 ❌停止、实际却在继续跑"或"报告里的核心词不是实际搜的词"。
        non_us = not is_us_site(site=str(ctx.config.get("SITE", "") or ""),
                                market=str(ctx.config.market or ""),
                                cat=str(ctx.config.cat or ""))
        if non_us:
            auto_core = pick_top_volume_candidates(keyword_data, NON_US_QUALIFIED_TOP)
        else:
            auto_core = [w for w in verdict_flat if verdict_flat[w] == "达标"]
        plan = resolve_keywords(
            ctx.paths.cat_root, cat,
            auto_core=auto_core, auto_longtail=longtail_words,
            screen_verdict=verdict_flat,
            gate=int(ctx.config.get("GATE_MIN_QUALIFIED", 2) or 2),
            gate_applies=not non_us,
        )
        self.log(ctx, f"关键词口径: {plan.source_text()}; {plan.gate_text()}")
        return {
            "keyword_data": keyword_data,
            "screen_results": screen_results,
            "screen_map": screen_map,
            "kw_map": _norm_keyword_data(keyword_data),
            "monthly": monthly,
            "bestseller": bestseller,
            "dual_details": read_jsonl(paths["dual_details"]),
            "dual_filtered": read_jsonl(paths["dual_filtered"]),
            "core_words": list(plan.core),
            "longtail_words": list(plan.longtail),
            "plan": plan,
            "non_us": non_us,
        }

    def _build_summary(self, ctx: StepContext, inputs: dict, core_words: list[str], longtail_words: list[str],
                       screen_map: dict, monthly: dict, gate: int, season_threshold: float) -> pd.DataFrame:
        bestseller = inputs["bestseller"]
        bs = bestseller.get("bs") or []
        nr = bestseller.get("nr") or []
        union = bestseller.get("union") or []
        dual_details = inputs["dual_details"]
        dual_filtered = inputs["dual_filtered"]
        screen_all = inputs["screen_results"]
        ok_count = sum(1 for r in dual_details if r.get("ok") is True)
        qualified_all = sum(1 for r in screen_all if str(r.get("verdict", "")).strip() == "达标")
        # 门槛口径(2026-09-29): 与步骤9 完全一致 —— 人工清单生效时按"最终词数"判,
        # 停止标记存在时报 ❌停止, 非 US 站自动态不适用门槛。三者都由 core.keyword_lists 决定。
        plan = inputs.get("plan")
        non_us = bool(inputs.get("non_us"))
        if plan is not None:
            gate_ok = bool(plan.gate_ok) and not plan.stopped
            gate_text = plan.gate_text()
            if non_us and not plan.is_manual and not plan.stopped:
                gate_text = (f"非美国站 → 不适用(不套 SCREEN_REQUIREMENT); " + gate_text)
        else:                                   # 兜底(理论上不会走到): 保持旧文案
            qualified_core = [w for w in core_words if (screen_map.get(w) or {}).get("verdict") == "达标"]
            gate_ok = len(qualified_core) >= gate
            gate_text = (f"达标核心大词 {len(qualified_core)} 个 "
                         f"{'≥' if gate_ok else '<'} 阈值 {gate} → {'✅继续' if gate_ok else '❌停止'}")
        cat_season = category_seasonal(monthly, season_threshold)
        if cat_season["verdict"] == "数据不足":
            season_text = "月度趋势数据缺失, 无法判断"
            peaks_text = ""
            valleys_text = ""
        else:
            peaks_text = ",".join(f"{m}月" for m in cat_season["peaks"])
            valleys_text = ",".join(f"{m}月" for m in cat_season["valleys"])
            season_text = f"峰谷比:{cat_season['ratio']:.2f} 旺季:{peaks_text} 淡季:{valleys_text} 判定:{cat_season['verdict']}"
        if not gate_ok:
            if plan is not None and plan.stopped:
                advice = ("已人工标记停止(本轮不抓取搜索数据); "
                          "如需继续请在看板「关键词清单」面板清除停止标记。")
            else:
                advice = ("核心词未达开品门槛, 建议暂缓投入或更换类目; "
                          "或在看板「关键词清单」面板人工补词 / 下调门槛值。")
        elif cat_season["verdict"] == "数据不足":
            advice = "建议补充月度趋势数据后再评估投放节奏。"
        else:
            advice = (f"旺季({peaks_text})集中备货与广告预算, 淡季({valleys_text})控制库存; "
                      f"优先投放达标核心大词。")
        rows = [
            ("生成日期", self.now()),
            ("目标月份", ctx.config.trend_month or "—"),
            ("数据来源", "FR-00 榜单 / FR-01 详情 / FR-03 过滤 / FR-04 关键词 / FR-05 挖词 / FR-06 趋势 / FR-07 筛选"),
            ("双榜ASIN样本", len(union)),
            ("BS合并", len(bs)),
            ("NR合并", len(nr)),
            ("详情抓取成功数", ok_count),
            ("过滤后保留详情数", len(dual_filtered)),
            ("筛选达标数(全部候选词)", qualified_all),
            ("核心词来源", plan.mode_label if plan is not None else "自动"),
            ("最终生效核心词数", len(core_words)),
            ("其中 FR-07 达标", plan.qualified if plan is not None else 0),
            ("其中无 FR-07 判定", plan.no_verdict if plan is not None else 0),
            ("选定核心词", "、".join(core_words) if core_words else "—"),
            ("长尾词来源", "人工清单" if (plan is not None and plan.longtail_manual_active) else "自动"),
            ("长尾词数", len(longtail_words)),
            ("开品门槛", gate_text),
            ("季节性结论", season_text),
            ("执行建议", advice),
        ]
        return pd.DataFrame(rows, columns=["项目", "内容"])

    def _build_core_sheet(self, core_words: list[str], kw_map: dict, screen_map: dict,
                          monthly: dict, season_threshold: float,
                          manual_core: set | None = None) -> pd.DataFrame:
        cols = ["关键词", "站内月搜索量", "站内数据月份", "站内同比%", "Google指数", "Google同比%",
                "CPC", "CPC下限", "CPC上限", "达标判定", "Google季节性", "站内季节性", "Google数据口径", "Google数据范围"]
        if not core_words:
            return pd.DataFrame(columns=cols)
        manual = set(manual_core or ())
        rows = []
        for w in core_words:
            rec = kw_map.get(w, {})
            gidx = rec.get("google_monthly_index") or {}
            evidence = rec.get("google_evidence")
            valid = isinstance(evidence, dict) and evidence.get("target_month") == getattr(self, "_trend_month", None)
            gyoy = evidence.get("yoy") if valid else google_yoy(gidx, getattr(self, "_trend_month", None))
            gseason = seasonal_label(seasonal_analysis(google_natural_month_means(gidx), season_threshold))
            sseason = seasonal_label(seasonal_analysis(monthly_natural_means(monthly.get(w)), season_threshold))
            rows.append([
                w,
                _to_int(rec.get("monthly_search_volume")),
                rec.get("station_trend_month") or ("旧记录未标注" if rec.get("monthly_search_volume") is not None else "无数据"),
                _r1(rec.get("mom_yoy")),
                _r1(evidence.get("index") if valid else google_index_latest(gidx)),
                _r1(gyoy),
                _r2(rec.get("cpc")),
                _r2(rec.get("cpc_min")),
                _r2(rec.get("cpc_max")),
                _verdict_text(w, screen_map, manual),
                gseason,
                sseason,
                ({"target_month": "目标月", "complete_weeks_mean": "完整周平均指数", "recent_four_weeks_mean": "近4周平均指数", "previous_month": "历史月份", "unavailable": "无数据"}.get(evidence.get("kind"), "未知") if valid else "旧记录"),
                (evidence.get("period", "") if valid else ""),
            ])
        return pd.DataFrame(rows, columns=cols)

    def _build_seasonal_sheet(self, monthly: dict, season_threshold: float) -> pd.DataFrame:
        cols = ["关键词"] + [f"{m}月" for m in range(1, 13)] + ["3高峰", "3低谷", "峰谷比", "判定"]
        if not monthly:
            return pd.DataFrame(columns=cols)
        rows = []
        for w, rows_data in monthly.items():
            means = monthly_natural_means(rows_data)
            analysis = seasonal_analysis(means, season_threshold)
            row = [w]
            for m in range(1, 13):
                v = means.get(m)
                row.append(round(v, 1) if v is not None else None)
            rows.append(_append_season_columns(row, analysis))
        cat = category_seasonal(monthly, season_threshold)
        cat_row = ["类别合成"]
        for m in range(1, 13):
            v = category_month_means(monthly).get(m)
            cat_row.append(round(v, 1) if v is not None else None)
        rows.append(_append_season_columns(cat_row, cat))
        return pd.DataFrame(rows, columns=cols)

    def _build_table_sheet(self, table_keywords: list[str], kw_map: dict, screen_map: dict) -> pd.DataFrame:
        cols = ["关键词", "站内月搜索量", "站内数据月份", "站内同比%", "Google同比%", "CPC", "判定", "原因", "Google数据口径", "Google数据范围"]
        if not table_keywords:
            return pd.DataFrame(columns=cols)
        rows = []
        for w in table_keywords:
            rec = kw_map.get(w, {})
            verdict = screen_map.get(w, {}).get("verdict", "数据不足")
            reason = screen_map.get(w, {}).get("reason", "无筛选记录")
            rows.append([
                w,
                _to_int(rec.get("monthly_search_volume")),
                rec.get("station_trend_month") or ("旧记录未标注" if rec.get("monthly_search_volume") is not None else "无数据"),
                _r1(rec.get("mom_yoy")),
                _r1(rec["google_evidence"].get("yoy") if isinstance(rec.get("google_evidence"), dict) and rec["google_evidence"].get("target_month") == getattr(self, "_trend_month", None) else google_yoy(rec.get("google_monthly_index") or {}, getattr(self, "_trend_month", None))),
                _r2(rec.get("cpc")),
                verdict,
                reason,
                ({"target_month": "目标月", "complete_weeks_mean": "完整周平均指数", "recent_four_weeks_mean": "近4周平均指数", "previous_month": "历史月份", "unavailable": "无数据"}.get(rec["google_evidence"].get("kind"), "未知") if isinstance(rec.get("google_evidence"), dict) and rec["google_evidence"].get("target_month") == getattr(self, "_trend_month", None) else "旧记录"),
                (rec["google_evidence"].get("period", "") if isinstance(rec.get("google_evidence"), dict) and rec["google_evidence"].get("target_month") == getattr(self, "_trend_month", None) else ""),
            ])
        return pd.DataFrame(rows, columns=cols)

    def _build_longtail_sheet(self, longtail_words: list[str], manual_tail: set | None = None,
                              auto_tail: set | None = None) -> pd.DataFrame:
        manual = set(manual_tail or ())
        auto = set(auto_tail or ())
        rows = []
        if longtail_words:
            for w in longtail_words:
                if w in manual and w not in auto:
                    # 人工清单新增的词没跑过 FR-04/FR-05/FR-07, 指标天然为空 ——
                    # 这里如实标注, 免得看成"数据缺失的 bug"(2026-09-29)。
                    note = "人工清单新增(未跑 FR-04/FR-05/FR-07, 无指标)"
                elif w in manual:
                    note = "来自 FR-04 关键词文件(人工清单保留)"
                else:
                    note = "来自 FR-04 关键词文件"
                rows.append([w, "—", note])
        # 2026-10-10(用户要求做成机制): 本 sheet **只放关键词** ——
        # ① 防御过滤: 万一上游(人工清单/关键词文件)混进 `搜索配置` 这类方法名, 在写出前剔除;
        # ② 原先写在这里的 3 行"搜索配置"方法行已移到 `方法说明` sheet(方法/口径不属于关键词清单)。
        # 判定规则与清理脚本共用: core/keyword_report_clean.py
        rows = [r for r in rows if not _is_method_keyword(r[0])]
        return pd.DataFrame(rows, columns=["长尾词", "维度", "说明"])

    def _build_source_sheet(self) -> pd.DataFrame:
        rows = [
            ("双榜ASIN样本", "FR-00 <CAT>-bestseller-newrelease.json", "bs/nr/union 三组 ASIN 列表, 样本数取 union 并集"),
            ("详情抓取成功数", "FR-01 <CAT>-dual-details.jsonl", "每行一条详情抓取记录, ok=true 计为成功"),
            ("过滤后保留详情数", "FR-03 <CAT>-dual-details-filtered.jsonl", "保留行(keep=true) 数量"),
            ("关键词清单", "FR-04 <CAT>-keywords.txt", "[核心大词] = 全部大词候选(不固定 5 个; 达标筛选由 FR-07 对全部候选执行) + [长尾词] 20 行"),
            ("关键词指标", "FR-05 <CAT>-amazon-keyword-data.json", "站内月搜索量/站内同比/CPC/周搜索量/搜索排名/Google 月度指数等"),
            ("月度趋势", "FR-06 <CAT>-amazon-monthly.json", "每词逐月搜索量/购买量/购买率/同比, 用于自然月季节性分析"),
            ("筛选结果", "FR-07 <CAT>-screen-results.json", "每词判定 达标/不达标/数据不足 及原因"),
            ("生成时间", "系统当前时间", "报告生成日期; 目标月份取自配置 TREND_MONTH"),
        ]
        return pd.DataFrame(rows, columns=["数据项", "来源", "说明"])

    def _build_method_sheet(self) -> pd.DataFrame:
        rows = [
            ("筛选标准", "FR-07 筛选脚本逐词判定: 月搜索量>=3万, 站内同比>=-20%, Google同比>=-20%, CPC<3"
                         "(阈值以配置 SEARCH_VOLUME_THRESHOLD/MOM_YOY_MIN/GOOGLE_YOY_MIN/CPC_MAX 为准)"),
            ("关键词清单", "FR-04 <CAT>-keywords.txt + 看板「关键词清单」面板 —— "
                          "自动态: [核心大词] 用 FR-07 达标词(US)/站内搜索量前 N(非 US) + [长尾词] 五维; "
                          "人工态: filter-<CAT>-core-words.txt / -longtail-words.txt(存在即生效, "
                          "删除回自动, 人工词不再看 FR-07 判定); "
                          "停止态: filter-<CAT>-keyword-stop.txt 存在则不抓取"),
            ("开品门槛", "核心词数 >= GATE_MIN_QUALIFIED(默认 2) 判定 ✅继续, 否则 ❌停止; "
                        "人工态按**最终生效核心词数**判(不再看 FR-07 判定), "
                        "非 US 站自动态不适用门槛"),
            ("季节性算法", "按自然月(1-12)对搜索量取均值; 3高峰=均值最大的 3 个月, 3低谷=均值最小的 3 个月; "
                          "峰谷比=高峰均值/低谷均值; 峰谷比 >= SEASONAL_PEAK_RATIO(默认 2.0) 判定显著"),
            ("Google同比", "Google 月度指数最近 12 个月均值 vs 前 12 个月均值的同比变化"),
            ("Google指数", "Google 月度指数中最近一个月的指数值"),
            ("局限性", "数据为抓取时点快照, 受抓取时间/广告位/平台算法影响; Google 指数为相对值; "
                       "月度趋势覆盖时长影响自然月均值稳定性; 缺失输入时相关 sheet 降级为空表或提示文字"),
            ("合规说明", "仅用于类目选品分析; 不抓取个人数据; 遵守亚马逊/Google 平台条款与当地法律法规"),
            # 2026-10-10: 原先这 3 行被写进 `长尾词` sheet(占用了"长尾词/维度"两列), 属于脏数据;
            # 它们是**搜索口径**, 归口到本 sheet(项目/说明 两列), 长尾词 sheet 只留关键词。
            ("搜索配置", f"核心大词每词搜索 {getattr(self, '_core_pages', 5)} 页, "
                        f"长尾词每词搜索 {getattr(self, '_tail_pages', 4)} 页"),
            ("搜索配置", "广告处理: 只取自然位, 跳过含 Sponsored 标记的广告条目"),
            ("搜索配置", "清单合并: 搜索结果 ASIN 并入双榜清单后全量去重"),
        ]
        return pd.DataFrame(rows, columns=["项目", "说明"])

    def _apply_verdict_fills(self, path: str) -> None:
        wb = load_workbook(path)
        for sheet_name, col_name in (("核心词分析", "达标判定"), ("季节性分析", "判定"), ("关键词总表", "判定")):
            ws = wb[sheet_name]
            col_idx = None
            for cell in ws[1]:
                if cell.value == col_name:
                    col_idx = cell.column
                    break
            if col_idx is None:
                continue
            for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                cell = row[0]
                fill = _FILL_MAP.get(str(cell.value).strip())
                if fill is not None:
                    cell.fill = fill
        wb.save(path)


def parse_keywords_file(lines: list[str]) -> tuple[list[str], list[str]]:
    """解析分区关键词文件: [核心大词](=全部大词候选, 2026-09 不再固定 5 个) + [长尾词] 20 行。"""
    core: list[str] = []
    longtail: list[str] = []
    section = ""
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s
            continue
        if section == "[核心大词]":
            core.append(s)
        elif section == "[长尾词]":
            longtail.append(s)
    return core, longtail


def _norm_keyword_data(records: list[dict]) -> dict:
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        kw = str(rec.get("keyword", "")).strip()
        if kw:
            out[kw] = rec
    return out


def _norm_screen(records: list[dict]) -> dict:
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        kw = str(rec.get("keyword", "")).strip()
        if not kw:
            continue
        out[kw] = {
            "verdict": str(rec.get("verdict", "") or "").strip() or "数据不足",
            "reason": str(rec.get("reason", "") or "").strip() or "—",
        }
    return out


def _verdict_text(word: str, screen_map: dict, manual: set) -> str:
    """「达标判定」列文案: 人工清单新增的词**没跑过 FR-07**, 标"人工新增(未判定)" ——
    写成"数据不足"会让人以为是筛选数据缺了(2026-09-29 用户问"加词后报告会变吗")。"""
    rec = screen_map.get(word)
    if isinstance(rec, dict) and rec:
        return str(rec.get("verdict") or "数据不足")
    return "人工新增(未判定)" if word in manual else "数据不足"


def _to_float(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    v = _to_float(value)
    return int(v) if v is not None else None


def _r1(value) -> float | None:
    v = _to_float(value)
    return round(v, 1) if v is not None else None


def _r2(value) -> float | None:
    v = _to_float(value)
    return round(v, 2) if v is not None else None


def _month_num(value) -> int | None:
    if value is None:
        return None
    m = _MONTH_RE.match(str(value).strip())
    if not m:
        return None
    num = int(m.group(2))
    if 1 <= num <= 12:
        return num
    return None


def _google_sorted_months(index_map: dict) -> list[str]:
    keys = []
    for k in (index_map or {}).keys():
        ks = str(k).strip()
        if _MONTH_RE.match(ks):
            keys.append(ks)
    return sorted(set(keys))


def google_index_latest(index_map: dict) -> float | None:
    """Google 月度指数中最近一个月的指数值。"""
    months = _google_sorted_months(index_map)
    if not months:
        return None
    return _to_float(index_map.get(months[-1]))


def google_yoy(index_map: dict, target_month: str | None = None) -> float | None:
    """Google 同比%: **目标月指数 vs 去年同月指数**(严格同比)。

    目标月未指定时取序列最后一个月; 目标月或去年同月缺失时
    回退滚动同比(最近 N 月均值 vs 前 N 月均值, 月份对齐)。
    """
    months = _google_sorted_months(index_map)
    if not months:
        return None
    target = _resolve_target(index_map, months, target_month)
    if target:
        cur = _to_float(index_map.get(target))
        if cur is not None:
            y, m = int(target[:4]), target[5:7]
            prev = _to_float(index_map.get(f"{y - 1}-{m}"))
            if prev is not None and prev != 0:
                return (cur - prev) / prev * 100.0
    return _rolling_google_yoy(index_map, months)


def _resolve_target(index_map: dict, months: list, target_month: str | None) -> str | None:
    if not target_month:
        return months[-1] if months else None
    t = str(target_month).strip()
    if len(t) == 6:
        t = f"{t[:4]}-{t[4:6]}"
    return t if t in index_map else None


def _rolling_google_yoy(index_map: dict, months: list) -> float | None:
    """滚动同比(回退): 最近 N 月均值 vs 前 N 月均值, 月份对齐。"""
    values = [_to_float(index_map.get(k)) for k in months]
    values = [v for v in values if v is not None]
    if len(values) < 6:
        return None
    n = min(12, len(values) // 2)
    if n < 3:
        return None
    recent = sum(values[-n:]) / n
    prev = sum(values[-2 * n:-n]) / n
    if prev <= 0:
        return None
    return (recent - prev) / prev * 100.0


def google_natural_month_means(index_map: dict) -> dict[int, float]:
    """把 Google 月度指数按自然月(1-12)取多年均值。"""
    groups: dict[int, list[float]] = {}
    for k, v in (index_map or {}).items():
        num = _month_num(k)
        val = _to_float(v)
        if num is not None and val is not None:
            groups.setdefault(num, []).append(val)
    return {m: sum(vals) / len(vals) for m, vals in groups.items()}


def monthly_natural_means(rows) -> dict[int, float]:
    """把月度趋势行按自然月(1-12)取均值。"""
    groups: dict[int, list[float]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        num = _month_num(r.get("month"))
        val = _to_float(r.get("search_volume"))
        if num is not None and val is not None:
            groups.setdefault(num, []).append(val)
    return {m: sum(vals) / len(vals) for m, vals in groups.items()}


def seasonal_analysis(means: dict[int, float], threshold: float) -> dict:
    """自然月均值 -> 3高峰/3低谷/峰谷比/判定。"""
    months = [m for m, v in means.items() if v is not None]
    if len(months) < 6:
        return {"verdict": "数据不足", "peaks": [], "valleys": [], "ratio": None}
    ordered = sorted(months, key=lambda m: means[m], reverse=True)
    peaks = sorted(ordered[:3], key=lambda m: means[m], reverse=True)
    valleys = sorted(ordered[-3:], key=lambda m: means[m])
    peak_mean = sum(means[m] for m in peaks) / 3
    valley_mean = sum(means[m] for m in valleys) / 3
    if valley_mean <= 0:
        return {"verdict": "数据不足", "peaks": [], "valleys": [], "ratio": None}
    ratio = peak_mean / valley_mean
    verdict = "显著" if ratio >= threshold else "不显著"
    return {"verdict": verdict, "peaks": peaks, "valleys": valleys, "ratio": ratio}


def seasonal_label(analysis: dict) -> str:
    if analysis["verdict"] == "数据不足":
        return "数据不足"
    peaks = ",".join(f"{m}月" for m in analysis["peaks"])
    valleys = ",".join(f"{m}月" for m in analysis["valleys"])
    return f"旺季:{peaks} 淡季:{valleys} 峰谷比:{analysis['ratio']:.2f}"


def category_month_means(monthly: dict) -> dict[int, float]:
    """类别合成: 所有词各自然月均值的再平均。"""
    month_sums: dict[int, list[float]] = {}
    for rows in monthly.values():
        for m, v in monthly_natural_means(rows).items():
            month_sums.setdefault(m, []).append(v)
    return {m: sum(vals) / len(vals) for m, vals in month_sums.items()}


def category_seasonal(monthly: dict, threshold: float) -> dict:
    return seasonal_analysis(category_month_means(monthly), threshold)


def _append_season_columns(row: list, analysis: dict) -> list:
    if analysis["verdict"] == "数据不足":
        return row + ["—", "—", "—", "数据不足"]
    peaks = ",".join(f"{m}月" for m in analysis["peaks"])
    valleys = ",".join(f"{m}月" for m in analysis["valleys"])
    return row + [peaks, valleys, round(analysis["ratio"], 2), analysis["verdict"]]
