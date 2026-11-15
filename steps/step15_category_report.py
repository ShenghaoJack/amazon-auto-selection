"""FR-15 初步类目分析报告。

输入: 拆分结果(14_流量拆分/流量拆分数据-需人工补充.xlsx 的"自动补充表" sheet)
      + 关键词报告(<CAT>-keyword-report.xlsx 的"核心词分析" sheet)
输出: <CAT>-初步类目分析报告.xlsx, 4 sheet: 综合分析/关键词与季节性/变体明细/口径说明

逻辑: 表格生成库动态计算 KPI/类目门禁/品牌排名/季节性;
      达标行绿色 / 不达标红色 / 数据不足橙色高亮; Top5 品牌行底色标记。
容错: 拆分结果缺失 -> 抛异常阻断; 关键词报告缺失 -> 告警并用空数据。
"""
from __future__ import annotations

import re

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from core import StepDataError, df_to_excel, read_excel_sheet, require_file, warn_missing
from core.currency import SITE_CURRENCY, CURRENCY_NAMES
from steps.step14_traffic_split import _positive_price, _validate_price_currency
from steps._month_fallback import display as _month_display  # noqa: E402
from steps.base import BaseStep, StepContext

SPLIT_FILE = "14_流量拆分/流量拆分数据-需人工补充.xlsx"
SPLIT_SHEET = "AI已补充"
KEYWORD_REPORT = "{cat}-keyword-report.xlsx"
KEYWORD_SHEET = "核心词分析"

GATE_TOP5_REV_SHARE = 60.0
GATE_MEDIAN_PRICE = 35.0
GATE_AMZ_BRAND_SHARE = 20.0
GATE_NEW_SELLERS = 10
BRAND_RANK_LIMIT = 10
TOP5_ROW_COUNT = 5

PASS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
WARN_FILL = PatternFill(start_color="F8CBAD", end_color="F8CBAD", fill_type="solid")
TOP5_FILL = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")

SHEET1_COLS = [
    "板块",
    "项目",
    "数值",
    "判定",
    "品牌",
    "变体月销量",
    "变体月销额",
    "销额占比%",
    "销量占比%",
    "关键词",
    "站内月搜索量",
    "站内同比%",
    "CPC",
    "CPC下限",
    "CPC上限",
    "Google同比%",
    "Google季节性",
    "站内季节性",
]

KW_COLS = [
    "关键词",
    "站内月搜索量",
    "站内同比%",
    "Google同比%",
    "CPC",
    "CPC下限",
    "CPC上限",
    "达标判定",
    "Google季节性",
    "站内季节性",
]

VARIANT_COLS = [
    "变体ASIN",
    "品牌",
    "配送方式",
    "价格",
    "变体月销量",
    "变体月销额",
    "新老品",
    "评分",
]


class Step15CategoryReport(BaseStep):
    number = 15
    title = "初步类目分析报告"
    description = "FR-15: 基于流量拆分与关键词报告生成 4 sheet 初步类目分析报告"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        self._split_path = ctx.resolve_artifact(SPLIT_FILE)
        require_file(self._split_path, SPLIT_FILE)

    @property
    def artifacts(self) -> list[str]:
        return [f"{self._cat}-初步类目分析报告.xlsx"]

    def run(self, ctx: StepContext) -> None:
        cat = self._cat
        self.log(ctx, f"开始生成初步类目分析报告: CAT={cat}")
        split_df = self._read_split(ctx)
        variants, meta = self._clean_variants(ctx, split_df)
        kw_df = self._read_keywords(ctx, cat)
        kpi = self._compute_kpi(ctx, variants)
        gates = self._compute_gates(ctx, variants, meta)
        brand_rank = self._brand_ranking(variants)
        qualified, kw_full = self._keyword_rows(ctx, kw_df)
        season = [(r["关键词"], r["Google季节性"], r["站内季节性"]) for r in kw_full]
        df_s1 = self._build_summary(kpi, gates, brand_rank, qualified, season)
        df_s2 = pd.DataFrame(kw_full, columns=KW_COLS)
        df_s3 = self._variant_detail(variants)
        df_s4 = self._build_notes(ctx, cat)
        path = ctx.resolve_artifact(self.artifacts[0])
        df_to_excel(
            path,
            {
                "综合分析": df_s1,
                "关键词与季节性": df_s2,
                "变体明细": df_s3,
                "口径说明": df_s4,
            },
        )
        self._apply_highlights(path)
        self.log(ctx, f"初步类目分析报告已写入: {path}")

    def _read_split(self, ctx: StepContext) -> pd.DataFrame:
        try:
            df = read_excel_sheet(self._split_path, SPLIT_SHEET)
        except Exception:
            self.log(ctx, f"未找到 sheet '{SPLIT_SHEET}', 回退读取第一个 sheet")
            df = read_excel_sheet(self._split_path, 0)
        if df is None or df.empty:
            raise StepDataError(f"拆分结果为空: {self._split_path}")
        return df

    def _read_keywords(self, ctx: StepContext, cat: str) -> pd.DataFrame:
        path = ctx.resolve_artifact(KEYWORD_REPORT.format(cat=cat))
        if not warn_missing(path, KEYWORD_REPORT.format(cat=cat)):
            self.log(ctx, "关键词报告缺失, 使用空数据继续")
            return pd.DataFrame()
        try:
            return read_excel_sheet(path, KEYWORD_SHEET)
        except Exception:
            self.log(ctx, f"未找到 sheet '{KEYWORD_SHEET}', 使用空数据继续")
            return pd.DataFrame()

    def _clean_variants(self, ctx: StepContext, split_df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
        col_asin = _find_col(split_df, ["变体ASIN", "asin"])
        col_sales = _find_col(split_df, ["变体月销量", "月销量"])
        col_rev = _find_col(split_df, ["变体月销额", "月销额"])
        col_price = _find_col(split_df, ["价格", "售价"])
        col_brand = _find_col(split_df, ["品牌", "brand"])
        col_ship = _find_col(split_df, ["配送方式", "配送"])
        col_newold = _find_col(split_df, ["新老品", "新老"])
        col_rating = _find_col(split_df, ["评分", "rating"])
        # 门禁④ 2026-09-11 用户口径修正: 统计"新品行的**唯一 BuyBox卖家**个数"(去重计数);
        # 旧实现误用了另一列"卖家数"(该 listing 的卖家总数)并取最大值 → 字段名与口径不符。
        # 阈值见下方 config 键 GATE_NEW_SELLERS(默认 10)。
        col_bb = _find_col(split_df, ["BuyBox卖家", "buybox卖家", "buybox seller", "购物车卖家"])
        required = [
            ("变体ASIN", col_asin),
            ("变体月销量", col_sales),
            ("变体月销额", col_rev),
            ("价格", col_price),
        ]
        missing = [name for name, col in required if col is None]
        if missing:
            raise StepDataError(f"自动补充表缺少必要列: {missing}")
        site = str(ctx.config.get("SITE", "") or "").strip()
        _validate_price_currency(site, str(col_price), split_df[col_price])
        for name, col in (("品牌", col_brand), ("配送方式", col_ship), ("新老品", col_newold)):
            if col is None:
                self.log(ctx, f"自动补充表缺少可选列 {name}, 按'未知'处理")
        d = pd.DataFrame()
        d["_asin"] = split_df[col_asin].astype(str).str.strip()
        d["_sales"] = split_df[col_sales].apply(_to_num)
        d["_rev"] = split_df[col_rev].apply(_to_num)
        d["_price"] = split_df[col_price].apply(lambda value: _positive_price(value, site))
        d["_brand"] = split_df[col_brand].apply(_clean_label) if col_brand else "未知"
        d["_ship"] = split_df[col_ship].apply(_clean_label) if col_ship else "未知"
        d["_newold"] = split_df[col_newold].apply(_clean_label) if col_newold else "未知"
        if col_rating is not None:
            d["_rating"] = split_df[col_rating]
        else:
            d["_rating"] = None
        if col_bb is not None:
            d["_bb_seller"] = split_df[col_bb].apply(_clean_label)
        else:
            d["_bb_seller"] = None
        before = len(d)
        d = d[d["_price"].notna() & d["_sales"].notna() & (d["_sales"] > 0)]
        removed = before - len(d)
        d["_rev"] = d["_rev"].fillna(0.0)
        if d.empty:
            raise StepDataError("自动补充表无有效变体数据(无价格、月销量为空或0 的行已全部排除)")
        self.log(ctx, f"变体数据: 读入{before}行, 排除{removed}行, 有效{len(d)}行")
        meta = {
            "has_ship": col_ship is not None,
            "has_newold": col_newold is not None,
            "has_sellers": col_bb is not None,
        }
        return d, meta

    def _compute_kpi(self, ctx: StepContext, d: pd.DataFrame) -> list[tuple[str, object]]:
        sales = float(d["_sales"].sum())
        rev = float(d["_rev"].sum())
        price = d["_price"].dropna()
        kpi: list[tuple[str, object]] = [
            ("变体总数", len(d)),
            ("总变体月销量", int(round(sales))),
            ("总变体月销额", round(rev, 2)),
            ("价格均值", round(float(price.mean()), 2)),
            ("价格中位数", round(float(price.median()), 2)),
            ("品牌数", int(d["_brand"].nunique())),
        ]
        ship = d["_ship"].str.upper()
        kpi.append(("配送方式-FBA", int(ship.str.contains("FBA").sum())))
        kpi.append(("配送方式-AMZ", int(ship.str.contains("AMZ").sum())))
        kpi.append(("配送方式-FBM", int(ship.str.contains("FBM").sum())))
        kpi.append(("新品数", int(d["_newold"].str.contains("新").sum())))
        return kpi

    def _compute_gates(self, ctx: StepContext, d: pd.DataFrame, meta: dict) -> list[tuple[str, str, str]]:
        total_rev = float(d["_rev"].sum())
        top5_rev = float(d.groupby("_brand")["_rev"].sum().nlargest(TOP5_ROW_COUNT).sum())
        top5_share = top5_rev / total_rev * 100 if total_rev > 0 else 0.0
        verdict1 = "达标" if top5_share <= GATE_TOP5_REV_SHARE else "不达标"
        median_price = float(d["_price"].median())
        currency = SITE_CURRENCY.get(str(ctx.config.get("SITE", "") or "").strip(), "")
        verdict2 = ("达标" if median_price > GATE_MEDIAN_PRICE else "不达标") if currency == "USD" else "数据不足"
        value2 = f"{currency or '未知币种'} {median_price:.2f}"
        if currency != "USD":
            value2 += "（未换算美元）"
        if meta["has_ship"]:
            brands = d["_brand"].unique()
            amz_brands = d[d["_ship"].str.upper().str.contains("AMZ")]["_brand"].unique()
            amz_share = len(amz_brands) / len(brands) * 100 if len(brands) > 0 else 0.0
            verdict3 = "达标" if amz_share < GATE_AMZ_BRAND_SHARE else "不达标"
            value3 = f"{amz_share:.2f}%"
        else:
            verdict3 = "数据不足"
            value3 = "无数据"
        gate_new_sellers = _gate_new_sellers(ctx)
        if meta["has_newold"] and meta["has_sellers"]:
            new_rows = d[d["_newold"].str.contains("新")]
            names = new_rows["_bb_seller"]
            names = names[~names.isin(_UNKNOWN_LABELS)]
            uniq = int(names.nunique())
            if len(new_rows) == 0 or uniq == 0:
                verdict4 = "数据不足"
                value4 = "无数据"
            else:
                value4 = str(uniq)
                verdict4 = "达标" if uniq > gate_new_sellers else "不达标"
        else:
            verdict4 = "数据不足"
            value4 = "无数据"
        return [
            (f"门禁①: Top5品牌销额占比≤{GATE_TOP5_REV_SHARE:.0f}%", f"{top5_share:.2f}%", verdict1),
            (f"门禁②: 价格中位数>USD {GATE_MEDIAN_PRICE:.0f}", value2, verdict2),
            (f"门禁③: AMZ配送品牌占比<{GATE_AMZ_BRAND_SHARE:.0f}%", value3, verdict3),
            (f"门禁④: 新品BuyBox卖家数>{gate_new_sellers}", value4, verdict4),
        ]

    def _brand_ranking(self, d: pd.DataFrame) -> list[tuple[str, int, float, float, float]]:
        total_rev = float(d["_rev"].sum())
        total_sales = float(d["_sales"].sum())
        grouped = d.groupby("_brand").agg(sales=("_sales", "sum"), rev=("_rev", "sum")).reset_index()
        grouped = grouped.sort_values("rev", ascending=False).head(BRAND_RANK_LIMIT)
        rows: list[tuple[str, int, float, float, float]] = []
        for _, r in grouped.iterrows():
            rev_share = float(r["rev"]) / total_rev * 100 if total_rev > 0 else 0.0
            sales_share = float(r["sales"]) / total_sales * 100 if total_sales > 0 else 0.0
            rows.append(
                (
                    str(r["_brand"]),
                    int(round(float(r["sales"]))),
                    round(float(r["rev"]), 2),
                    round(rev_share, 2),
                    round(sales_share, 2),
                )
            )
        return rows

    def _keyword_rows(self, ctx: StepContext, kw_df: pd.DataFrame) -> tuple[list[tuple[str, object, object, object]], list[dict]]:
        if kw_df is None or kw_df.empty:
            return [], []
        col_kw = _find_col(kw_df, ["关键词", "keyword"])
        col_vol = _find_col(kw_df, ["站内月搜索量", "月搜索量", "monthly_search_volume"])
        col_yoy = _find_col(kw_df, ["站内同比", "mom_yoy"])
        col_gyoy = _find_col(kw_df, ["google同比", "google_yoy", "谷歌同比"])
        col_cpc = _find_col(kw_df, ["cpc"])
        col_cpcmin = _find_col(kw_df, ["cpc下限", "cpc_min"])
        col_cpcmax = _find_col(kw_df, ["cpc上限", "cpc_max"])
        col_verdict = _find_col(kw_df, ["达标判定", "判定", "verdict", "结论"])
        col_gseason = _find_col(kw_df, ["google季节性", "谷歌季节性", "google_seasonality"])
        col_sseason = _find_col(kw_df, ["站内季节性", "seasonality"])
        if col_kw is None:
            self.log(ctx, "关键词报告缺少关键词列, 使用空数据")
            return [], []
        full: list[dict] = []
        for _, row in kw_df.iterrows():
            kw = str(row.get(col_kw, "") or "").strip()
            if not kw:
                continue
            full.append(
                {
                    "关键词": kw,
                    "站内月搜索量": _fmt_num(row.get(col_vol) if col_vol is not None else None),
                    "站内同比%": _fmt_num(row.get(col_yoy) if col_yoy is not None else None),
                    "Google同比%": _fmt_num(row.get(col_gyoy) if col_gyoy is not None else None),
                    "CPC": _fmt_num(row.get(col_cpc) if col_cpc is not None else None),
                    "CPC下限": _fmt_num(row.get(col_cpcmin) if col_cpcmin is not None else None),
                    "CPC上限": _fmt_num(row.get(col_cpcmax) if col_cpcmax is not None else None),
                    "达标判定": _norm_verdict(row.get(col_verdict) if col_verdict is not None else None),
                    "Google季节性": _fmt_str(row.get(col_gseason) if col_gseason is not None else None),
                    "站内季节性": _fmt_str(row.get(col_sseason) if col_sseason is not None else None),
                }
            )
        qualified = [
            (r["关键词"], r["站内月搜索量"], r["站内同比%"], r["CPC"])
            for r in full
            if r["达标判定"] == "达标"
        ]
        return qualified, full

    def _build_summary(
        self,
        kpi: list[tuple[str, object]],
        gates: list[tuple[str, str, str]],
        brand_rank: list[tuple[str, int, float, float, float]],
        qualified: list[tuple[str, object, object, object]],
        season: list[tuple[str, str, str]],
    ) -> pd.DataFrame:
        rows: list[dict] = []
        for name, value in kpi:
            rows.append({"板块": "核心KPI", "项目": name, "数值": value, "判定": ""})
        for name, value, verdict in gates:
            rows.append({"板块": "类目门禁", "项目": name, "数值": value, "判定": verdict})
        for i, item in enumerate(brand_rank, start=1):
            rows.append(
                {
                    "板块": "品牌销额Top10",
                    "项目": f"Top{i}",
                    "品牌": item[0],
                    "变体月销量": item[1],
                    "变体月销额": item[2],
                    "销额占比%": item[3],
                    "销量占比%": item[4],
                    "判定": "",
                }
            )
        for item in qualified:
            rows.append(
                {
                    "板块": "达标关键词",
                    "关键词": item[0],
                    "站内月搜索量": item[1],
                    "站内同比%": item[2],
                    "CPC": item[3],
                    "判定": "达标",
                }
            )
        for kw, gseason, sseason in season:
            rows.append(
                {
                    "板块": "季节性总结",
                    "关键词": kw,
                    "Google季节性": gseason,
                    "站内季节性": sseason,
                    "判定": "",
                }
            )
        return pd.DataFrame(rows, columns=SHEET1_COLS)

    def _variant_detail(self, d: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "变体ASIN": d["_asin"],
                "品牌": d["_brand"],
                "配送方式": d["_ship"],
                "价格": d["_price"].round(2),
                "变体月销量": d["_sales"].round(0).astype(int),
                "变体月销额": d["_rev"].round(2),
                "新老品": d["_newold"],
                "评分": d["_rating"],
            },
            columns=VARIANT_COLS,
        )

    def _build_notes(self, ctx: StepContext, cat: str) -> pd.DataFrame:
        _tm = ctx.config.traffic_month
        traffic_month = _month_display(_tm) if _tm else "未配置"   # recent30 → 最近30天
        vol_th = ctx.config.get("SEARCH_VOLUME_THRESHOLD", 30000)
        yoy_min = ctx.config.get("MOM_YOY_MIN", -20.0)
        gyoy_min = ctx.config.get("GOOGLE_YOY_MIN", -20.0)
        cpc_max = ctx.config.get("CPC_MAX", 3.0)
        # 币种标签: 按站点(2026-09 多币种价列: 价格($)/价格(CA$)/价格(£)...)
        site = str(ctx.config.get("SITE", "") or "").strip()
        code = SITE_CURRENCY.get(site, "")
        currency_label = f"{CURRENCY_NAMES.get(code, '未知币种')} {code}".strip()
        notes = [
            ("销量口径", "仅使用变体级拆分结果(自动补充表的变体月销量/变体月销额), 不使用父级月销量"),
            ("数据期间", f"流量拆分月份: {traffic_month}"),
            ("数据来源", "拆分结果: 14_流量拆分/流量拆分数据-需人工补充.xlsx(自动补充表); 关键词报告: " + f"{cat}-keyword-report.xlsx(核心词分析)"),
            ("缺失处理", "配送方式与新老品字段缺失时自动标'未知'"),
            ("排除规则", "无价格、变体月销量为空或0 的行已从全部统计中剔除; 变体月销额缺失按 0 参与汇总"),
            ("价格口径", f"价格为变体单价({currency_label}), 价格均值/中位数基于剔除后的有效变体计算"),
            ("关键词门禁阈值", f"月搜索量>={vol_th}, 站内同比>={yoy_min}%, Google同比>={gyoy_min}%, CPC<{cpc_max}"),
            ("类目门禁规则", "① Top5品牌销额占比<=60% ② 价格中位数>USD 35，非美元站未换汇时标数据不足 ③ AMZ配送品牌占比<20%(按品牌数) "
                          "④ 新品BuyBox卖家数>阈值(取'新老品=新品'的行对 BuyBox卖家 去重后的个数; "
                          "阈值=config GATE_NEW_SELLERS, 默认10; 新品行或卖家名缺失标'数据不足')"),
            ("限制说明", "Google指数为归一化 0-100 指数, 非搜索量; 关键词判定沿用步骤7筛选结论"),
            ("季节性判定依据", "关键词报告的 Google季节性/站内季节性 列(步骤8 季节性分析), 缺失时留空"),
        ]
        return pd.DataFrame(notes, columns=["项目", "说明"])

    def _apply_highlights(self, path: str) -> None:
        wb = load_workbook(path)
        ws1 = wb["综合分析"]
        h1 = {str(c.value): c.column for c in ws1[1] if c.value is not None}
        block_col = h1.get("板块")
        judge_col = h1.get("判定")
        item_col = h1.get("项目")
        if block_col and judge_col:
            for row in ws1.iter_rows(min_row=2):
                block = row[block_col - 1].value
                if block == "类目门禁":
                    fill = _verdict_fill(row[judge_col - 1].value)
                    if fill is not None:
                        row[judge_col - 1].fill = fill
                elif block == "达标关键词":
                    row[judge_col - 1].fill = PASS_FILL
                elif block == "品牌销额Top10" and item_col:
                    rank = str(row[item_col - 1].value or "")
                    if rank.startswith("Top") and rank[3:].isdigit() and int(rank[3:]) <= TOP5_ROW_COUNT:
                        for cell in row:
                            cell.fill = TOP5_FILL
        ws2 = wb["关键词与季节性"]
        h2 = {str(c.value): c.column for c in ws2[1] if c.value is not None}
        verdict_col = h2.get("达标判定")
        if verdict_col:
            for row in ws2.iter_rows(min_row=2):
                fill = _verdict_fill(row[verdict_col - 1].value)
                if fill is not None:
                    row[verdict_col - 1].fill = fill
        wb.save(path)


_UNKNOWN_LABELS = {"未知", "", "nan", "none", "null", "-"}


def _gate_new_sellers(ctx) -> int:
    """门禁④ 阈值: config `GATE_NEW_SELLERS`(默认 10)。

    2026-09-11 起该阈值**可配置**(此前写死在模块常量里, 配置中心也改不到);
    未配置/非法/<=0 时回落到模块默认 GATE_NEW_SELLERS。
    注意: 门禁①②③ 的阈值仍是写死的模块常量(60 / $35 / 20%)。
    """
    try:
        value = int(float(ctx.config.get("GATE_NEW_SELLERS", GATE_NEW_SELLERS) or GATE_NEW_SELLERS))
    except (TypeError, ValueError):
        return GATE_NEW_SELLERS
    return value if value > 0 else GATE_NEW_SELLERS


def _norm_col(name) -> str:
    return re.sub(r"[\s_\-（）()%]", "", str(name)).lower()


def _find_col(df: pd.DataFrame, candidates: list[str]):
    cols = {_norm_col(c): c for c in df.columns}
    for cand in candidates:
        key = _norm_col(cand)
        if key in cols:
            return cols[key]
    for cand in candidates:
        key = _norm_col(cand)
        for norm, col in cols.items():
            if key and key in norm:
                return col
    return None


def _to_num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, bool):
            return None
        return float(value)
    s = str(value).strip().replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    if not s or s.lower() in ("nan", "none", "null", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean_label(value) -> str:
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return "未知"
    return s


def _fmt_num(value):
    num = _to_num(value)
    if num is None:
        return ""
    if float(num).is_integer():
        return int(num)
    return round(num, 2)


def _fmt_str(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s


def _norm_verdict(value) -> str:
    s = str(value or "").strip()
    if s in ("达标", "不达标", "数据不足"):
        return s
    return "数据不足"


def _verdict_fill(verdict):
    if verdict == "达标":
        return PASS_FILL
    if verdict == "不达标":
        return FAIL_FILL
    if verdict == "数据不足":
        return WARN_FILL
    return None
