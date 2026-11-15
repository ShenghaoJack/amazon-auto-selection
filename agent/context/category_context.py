"""落地层: 只读加载一个类目的配置与交付产物, 规范化供 agent 使用。

这是"类目分析助手"的第一层(Phase 1): 所有 agent 分析的数据都从这里取,
保证"LLM 只解释、不编造数字"。本层**只读** `data/<CAT>/` 下的产物,
不调用任何 LLM、不触碰 `steps/*` 与 `core/orchestrator.py`。

设计要点:
- 自包含: 使用 `agent._vendor` 的最小配置/路径/文件读取, 不 import core。
- 缺文件优雅降级: 某产物不存在时返回 None/空, 不整层报错;
  只有 config 缺失才抛 `ConfigError`(无法定位类目)。
- 懒加载 + 缓存: 用 `cached_property` 保证多次访问不重复读盘。
- provenance: 每次读某块数据都记入 `_provenance`, 供服务层生成 sources 引用。

注意(已核实真实产物列名):
- 关键词报告 `{CAT}-keyword-report.xlsx` 的结论 sheet 是 **核心词分析**
  (本层 keywords), 另有 结论总览/季节性分析/关键词总表 等 sheet。
- 初步类目分析报告 sheet: 综合分析 / 关键词与季节性 / 变体明细 / 口径说明。
- 产品属性标注 sheet: 产品属性标注(原拆分列 + 属性_* 列)。
"""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

import pandas as pd

from .._vendor import Config, Paths
from .._vendor.io_utils import read_excel_sheet, read_json, read_txt_lines
from .columns import cell, clean_label, find_col, norm_col, to_num

# ---- 产物相对类目根的路径(名称与主流水线数据契约一致) ----
KEYWORD_REPORT = "{cat}-keyword-report.xlsx"
VARIANT_REPORT = "{cat}-父项变体明细.xlsx"
CATEGORY_REPORT = "{cat}-初步类目分析报告.xlsx"
ATTR_REPORT = "18_标准化/{cat}-属性标注-标准化交付.xlsx"
ATTR_REPORT_LEGACY = "{cat}-产品属性标注.xlsx"   # 旧 step16 产物, 老类目回退用
SPLIT_FILE = "14_流量拆分/流量拆分数据-需人工补充.xlsx"
BESTSELLER_JSON = "{cat}-bestseller-newrelease.json"
CANDIDATE_TXT = "{cat}-keywords-candidates.txt"
KEYWORD_DATA_JSON = "{cat}-amazon-keyword-data.json"
SCREEN_JSON = "{cat}-screen-results.json"

# 关键词报告内部 sheet 名(已核实)
KW_SHEET = "核心词分析"
KW_OVERVIEW_SHEET = "结论总览"
KW_SUMMARY_SHEET = "关键词总表"
KW_SEASON_SHEET = "季节性分析"

# 初步类目分析报告内部 sheet 名(已核实)
REPORT_SHEET_MAIN = "综合分析"
REPORT_SHEET_KW = "关键词与季节性"
REPORT_SHEET_VARIANT = "变体明细"
REPORT_SHEET_NOTES = "口径说明"


class CategoryContext:
    """只读加载一个类目的配置与交付产物。"""

    def __init__(self, config_path: str):
        self.config_path = str(config_path)
        self.config: Config = Config.load(config_path)
        self.paths = Paths(self.config)
        self.cat = self.config.cat
        if not self.cat:
            raise ValueError("配置缺少 CAT(类目名), 无法定位产物目录")
        # 记录"读过哪些数据块", 服务层据此生成 sources 引用
        self._provenance: list[str] = []

    # ------------------------------------------------------------------ 工具
    def _touch(self, block: str) -> None:
        if block not in self._provenance:
            self._provenance.append(block)

    def _exists(self, rel: str) -> bool:
        return (self.paths.cat_root / rel).exists()

    def _excel_sheet(self, rel: str, sheet, block: str, default=None):
        """读某个 excel 的某 sheet; 记录 provenance; 缺文件/缺 sheet 返回 default。"""
        self._touch(block)
        if not self._exists(rel):
            return default
        try:
            df = read_excel_sheet(str(self.paths.cat_root / rel), sheet)
            return df if not df.empty else default
        except Exception:
            return default

    # ------------------------------------------------------------------ 产物
    @cached_property
    def keywords(self) -> pd.DataFrame | None:
        """步骤8 关键词报告 · 核心词分析(搜索量/同比/CPC/达标判定/季节性)。"""
        return self._excel_sheet(KEYWORD_REPORT.format(cat=self.cat), KW_SHEET, "keywords")

    @cached_property
    def keyword_overview(self) -> pd.DataFrame | None:
        """步骤8 关键词报告 · 结论总览。"""
        return self._excel_sheet(KEYWORD_REPORT.format(cat=self.cat), KW_OVERVIEW_SHEET, "keyword_overview")

    @cached_property
    def keyword_summary(self) -> pd.DataFrame | None:
        """步骤8 关键词报告 · 关键词总表(含判定+原因)。"""
        return self._excel_sheet(KEYWORD_REPORT.format(cat=self.cat), KW_SUMMARY_SHEET, "keyword_summary")

    @cached_property
    def keyword_seasonality(self) -> pd.DataFrame | None:
        """步骤8 关键词报告 · 季节性分析(按月)。"""
        return self._excel_sheet(KEYWORD_REPORT.format(cat=self.cat), KW_SEASON_SHEET, "keyword_seasonality")

    @cached_property
    def variants(self) -> pd.DataFrame | None:
        """步骤10 父项变体明细 · 变体明细。"""
        return self._excel_sheet(VARIANT_REPORT.format(cat=self.cat), 0, "variants")

    @cached_property
    def split(self) -> pd.DataFrame | None:
        """步骤14 流量拆分 · AI已补充(变体级月销量/月销额/主价)。"""
        return self._excel_sheet(SPLIT_FILE, "AI已补充", "split")

    @cached_property
    def split_original(self) -> pd.DataFrame | None:
        """步骤14 流量拆分 · 原始表(用于核对主价口径)。"""
        return self._excel_sheet(SPLIT_FILE, "原始表", "split_original")

    @cached_property
    def report_sheets(self) -> dict[str, pd.DataFrame]:
        """步骤15 初步类目分析报告 · 全部 sheet。"""
        self._touch("report")
        rel = CATEGORY_REPORT.format(cat=self.cat)
        if not self._exists(rel):
            return {}
        try:
            with pd.ExcelFile(self.paths.cat_root / rel) as xl:
                return {s: xl.parse(s) for s in xl.sheet_names}
        except Exception:
            return {}

    @cached_property
    def report_main(self) -> pd.DataFrame | None:
        return self.report_sheets.get(REPORT_SHEET_MAIN)

    @cached_property
    def attributes(self) -> pd.DataFrame | None:
        """步骤17 属性标注 · 标准化交付(旧类目回退读 step16 产品属性标注)。"""
        new = ATTR_REPORT.format(cat=self.cat)
        legacy = ATTR_REPORT_LEGACY.format(cat=self.cat)
        if self._exists(new):
            return self._excel_sheet(new, 0, "attributes")
        return self._excel_sheet(legacy, 0, "attributes")

    @cached_property
    def bestsellers(self) -> dict | None:
        """步骤0 榜单 JSON(BS/NR ASIN 清单)。"""
        self._touch("bestsellers")
        return read_json(str(self.paths.cat_root / BESTSELLER_JSON.format(cat=self.cat)))

    @cached_property
    def candidate_keywords(self) -> list[str]:
        """步骤4 关键词候选(大词候选, 行文件)。"""
        self._touch("candidate_keywords")
        return read_txt_lines(str(self.paths.cat_root / CANDIDATE_TXT.format(cat=self.cat)))

    @cached_property
    def keyword_data(self) -> list[dict]:
        """步骤5 关键词挖词指标(monthly_search_volume 等)。"""
        self._touch("keyword_data")
        data = read_json(str(self.paths.cat_root / KEYWORD_DATA_JSON.format(cat=self.cat)))
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return []

    @cached_property
    def screen_results(self) -> list[dict]:
        """步骤7 筛选判定(达标/不达标/数据不足)。"""
        self._touch("screen_results")
        data = read_json(str(self.paths.cat_root / SCREEN_JSON.format(cat=self.cat)))
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return []

    @cached_property
    def monitoring(self) -> dict:
        """步骤17 数据成熟度: 步骤状态 + 告警 + 体检(说明数据是否可靠/完整)。"""
        mdir = Path(self.paths.monitor_dir())
        self._touch("monitoring")
        step_status: dict[str, str] = {}
        sp = mdir / "step-status.txt"
        if sp.exists():
            for ln in sp.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = [x.strip() for x in ln.split("|")]
                if parts:
                    step_status[parts[0]] = parts[1] if len(parts) > 1 else "?"
        alerts = []
        ap = mdir / "monitor-alerts.txt"
        if ap.exists():
            alerts = [x.strip() for x in ap.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
        return {
            "step_status": step_status,
            "alerts": alerts[-40:],
            "health": read_json(str(mdir / "health-report.json"), {}),
        }

    # ------------------------------------------------------------------ 汇总助手
    @staticmethod
    def _present(v) -> bool:
        """按类型判空: None / 空 DataFrame / 空 dict / 空 list / 空 str 都视为不可用。"""
        if v is None:
            return False
        if isinstance(v, pd.DataFrame):
            return not v.empty
        if isinstance(v, (dict, list, set, tuple, str)):
            return len(v) > 0
        return bool(v)

    def available(self) -> list[str]:
        """返回当前实际可用的数据块列表(供服务层/前端提示)。"""
        return [b for b in (
            "keywords", "keyword_overview", "keyword_summary", "keyword_seasonality",
            "variants", "split", "report_sheets", "attributes", "bestsellers",
            "candidate_keywords", "keyword_data", "screen_results", "monitoring",
        ) if self._present(getattr(self, b))]

    def overview(self) -> dict:
        return {
            "cat": self.cat,
            "site": self.config.site,
            "market": self.config.market,
            "traffic_month": self.config.traffic_month,
            "competitor_month": self.config.competitor_month,
            "trend_month": self.config.trend_month,
            "dry_run": self.config.dry_run,
            "available": self.available(),
            "provenance": list(self._provenance),
        }

    # ---- 从"综合分析"提取结构化行(供工具直接使用) ----
    def _report_block(self, block_name: str) -> pd.DataFrame | None:
        df = self.report_main
        if df is None:
            return None
        bc = find_col(df, ["板块"])
        if bc is None:
            return None
        return df[df[bc].astype(str) == block_name].reset_index(drop=True)

    @cached_property
    def kpi_rows(self) -> pd.DataFrame | None:
        """核心KPI 行(项目/数值)。"""
        return self._report_block("核心KPI")

    @cached_property
    def gate_rows(self) -> pd.DataFrame | None:
        """类目门禁 行(项目/数值/判定)。"""
        return self._report_block("类目门禁")

    @cached_property
    def brand_rank_rows(self) -> pd.DataFrame | None:
        """品牌销额Top10 行。"""
        return self._report_block("品牌销额Top10")

    @cached_property
    def qualified_keywords(self) -> list[str]:
        """判定为'达标'的关键词清单。"""
        df = self.keywords
        if df is None or df.empty:
            return []
        vc = find_col(df, ["达标判定", "判定", "verdict", "结论"])
        kc = find_col(df, ["关键词", "keyword"])
        if vc is None or kc is None:
            return []
        hit = df[df[vc].astype(str) == "达标"]
        return [str(x).strip() for x in hit[kc].head(30) if str(x).strip()]

    @cached_property
    def attribute_freq(self) -> list[dict]:
        """统计属性标注里各属性列的值分布(找差异化卖点/共性)。"""
        df = self.attributes
        if df is None or df.empty:
            return []
        attr_cols = [c for c in df.columns if str(c).startswith("属性_") and not str(c).endswith("_来源")]
        out = []
        for c in attr_cols[:10]:
            try:
                vc = df[c].value_counts(dropna=True).head(5)
            except Exception:
                continue
            out.append({"attr": str(c), "top": {str(k): int(v) for k, v in vc.items()}})
        return out

    # ---- 便捷的文字化(供日志/排序) ----
    def describe(self) -> str:
        return (
            f"类目={self.cat} 站点={self.config.site} 市场={self.config.market} "
            f"数据月份={self.config.traffic_month or '-'} 可用块={len(self.available())}"
        )
