"""FR-14 流量拆分(pandas 全流程)。

输入: FR-10 变体明细 xlsx + FR-10 results.jsonl(全图/价格) + FR-11 流量来源合并 xlsx + FR-13 竞品合并 xlsx
输出: 14_流量拆分/流量拆分数据-需人工补充.xlsx, 3 sheet:
      原始表(未处理合并结果) / AI已补充(处理+补齐后) / 人工待补充表(配送方式或新老品为"未知")

逻辑(1-15, 对齐需求文档 FR-14): 明细标准化与去重 -> 图片统一(多图逗号拼接) -> 竞品关联
  (竞品列带"卖家精灵-"/"查竞品-"前缀, ASIN 去重防笛卡尔积) -> 流量来源关联(清洗/去重)
  -> 月销量、评分与评分数统一(过滤前按父ASIN取最大有效值) -> 变体流量过滤 -> 流量占比(单变体为 1)
  -> 行清洗 -> 品牌补齐(网页补抓->"未知") -> 销量拆分(占比=1 直接取, 多变体 ceil)
  -> 价格补齐(JSON兜底+带COOKIE网页补抓, 每请求2秒, 无价转人工) -> 第三方补齐
  (配送方式/上架时间: 竞品数据 + MCP competitor_lookup 6个月逐月, 40/批, 临时文件,
   仅取 fulfillment≠NA, 最近月份优先, 6个月查不到标"未知") -> 上架天数与新老品 -> 3 sheet

容错: 变体明细缺失抛异常阻断; 流量来源/竞品/results.jsonl 缺失告警并继续(空表);
      多变体按已有正流量变体分配全部父体销量, 缺失/零流量变体不参与;
      多变体父体整组无正流量时整组过滤, 不进入拆分且不阻断; 标准产物完整写入后原子替换, 占用时报错阻断;
      每步写进度日志(断点可查);
      单月 MCP 查询失败不中断(记日志继续); 补抓失败标"未知"。
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from datetime import datetime
from html import unescape

import pandas as pd

from core import (
    StepDataError,
    StepFailedError,
    dedup_keep_best,
    read_excel_all_sheets,
    read_excel_sheet,
    read_jsonl,
    require_file,
    warn_missing,
)
from core.http_client import HttpFetcher
from core.currency import SITE_CURRENCY, currency_conflict, currency_markers, strip_currency
from steps._month_fallback import is_recent30  # noqa: E402
from steps.base import BaseStep, StepContext

_DETAIL_ALIASES = {
    "父ASIN": ["父ASIN", "父asin", "parent_asin", "parent asin", "父体ASIN", "父体asin", "parentasin", "父体"],
    "变体ASIN": ["变体ASIN", "变体asin", "variant_asin", "variant asin", "子ASIN", "子asin", "ASIN", "asin", "变体"],
    # canonical 名称对齐需求文档(2026-09): 图片链接 / 五点描述(变体名称/标签不输出)
    "图片链接": ["图片链接", "变体主图", "变体主图链接", "主图", "主图链接", "主图URL", "主图url", "variant_image", "image", "图片URL"],
    "标题": ["标题", "商品标题", "产品标题", "title", "商品名称"],
    "五点描述": ["五点描述", "五点", "卖点", "五点卖点", "bullets", "五点简介"],
    "品牌": ["品牌", "brand", "商标"],
    "月销量": ["月销量", "销量", "月销售", "monthly_sales", "sales"],
    "评分": ["评分", "星级", "rating", "stars", "分数"],
    "评分数": ["评分数", "评分数量", "rating_count", "ratings_count"],
    "配送方式": ["配送方式", "配送", "发货方式", "delivery", "fulfillment", "发货渠道"],
    "上架时间": ["上架时间", "上架日期", "上市时间", "list_date", "listed_date", "上架日"],
    "流量词数": ["流量词数", "关键词数", "流量关键词数", "traffic_count", "词数"],
}

# 价格列智能识别(2026-09 变更): 主价数据源 = 卖家精灵竞品导出价列, 列名随站点币种变化
# (美国 价格($)/prime价格($); 加拿大 价格(CA$); 英国 价格(£); 德国 价格(€); 日本 价格(¥) ...)
# 识别规则: 主价 = 非 prime 的 "价格/Price" 列(可带币种括号), 优先带括号列; prime = prime价格(币种)。
_CURRENCY_BY_SITE = SITE_CURRENCY
_PRICE_RE = re.compile(r"^价格(?:\([^)]*\))?$")
_PRICE_RE_EN = re.compile(r"^price(?:\([^)]*\))?$", re.IGNORECASE)
_PRIME_PRICE_RE = re.compile(r"^(?:prime\s*)?(?:价格|price)(?:\([^)]*\))?$", re.IGNORECASE)


def _price_cols_in(df: pd.DataFrame, site: str = "") -> tuple[str | None, str | None, str]:
    """智能识别表内 主价列 / prime价列 / 币种代码。

    主价候选: 非 prime 前缀的 价格/Price 列(可带币种括号); 优先带括号(卖家精灵竞品导出),
    其次裸 "价格"。prime 候选: prime价格(币种)/Prime Price 等。
    币种: 列名中的符号结合站点识别，无括号时按站点兜底；未知站点不猜测。
    返回 (main_col, prime_col, currency_code)。
    """
    main_cands, prime_cands = [], []
    for c in df.columns:
        name = str(c).strip()
        low = name.lower()
        if _PRIME_PRICE_RE.match(low):
            if low.startswith("prime"):
                prime_cands.append(name)
            else:
                main_cands.append(name)
        elif _PRICE_RE.match(name) or _PRICE_RE_EN.match(low):
            main_cands.append(name)

    def _pick(cands: list[str]):
        if not cands:
            return None
        with_bracket = [c for c in cands if re.search(r"\([^)]*\)", str(c))]
        return (with_bracket or cands)[0]

    main_col, prime_col = _pick(main_cands), _pick(prime_cands)
    symbol = ""
    for c in (main_col, prime_col):
        if c:
            m = re.search(r"\(([^)]*)\)", str(c))
            if m:
                symbol = m.group(1).strip()
                break
    codes = currency_markers(symbol, site)
    return main_col, prime_col, next(iter(codes)) if len(codes) == 1 else _CURRENCY_BY_SITE.get(str(site).strip(), "")


def _validate_price_currency(site: str, column: str, values) -> None:
    expected = SITE_CURRENCY.get(site.strip())
    if not expected:
        raise StepDataError(f"站点 {site!r} 未配置价格币种，无法安全计算，请检查站点配置")
    conflicts = currency_conflict(column, site)
    if conflicts:
        raise StepDataError(f"价格列 {column!r} 的币种 {', '.join(sorted(conflicts))} 与 {site}站本币 {expected} 不一致，请核对来源")
    examples = []
    count = 0
    for index, value in values.items():
        conflicts = currency_conflict(value, site)
        if conflicts:
            count += 1
            if len(examples) < 3:
                examples.append(f"行索引 {index}: {str(value)[:80]}")
    if count:
        raise StepDataError(f"{column} 有 {count} 行价格币种与 {site}站本币 {expected} 不一致：{'；'.join(examples)}。请核对原数据，系统不会自动换汇")

_TRAFFIC_ALIASES = {
    "父ASIN": ["父ASIN", "父asin", "parent_asin", "父体ASIN"],
    "变体ASIN": ["变体ASIN", "变体asin", "variant_asin", "子ASIN"],
    # 2026-09 实测修正: 卖家精灵"查流量来源"导出中 "全部流量词" 列是**数字**(该 ASIN 流量词总数),
    # 各渠道列(自然搜索词/AC推荐词/SP广告词...)同样为渠道词数统计 —— 故映射为"流量词数";
    # "流量词"(词文本列表)在计数版导出中不存在, 词文本仅当有明细导出时才可填
    "流量词": ["流量词", "流量关键词", "关键词", "traffic_keyword", "流量词(全部)"],
    "流量词数": ["流量词数", "关键词数", "流量关键词数", "traffic_count", "词数", "全部流量词"],
    "月销量": ["月销量", "销量", "monthly_sales"],
    "评分": ["评分", "星级", "rating"],
    "评分数": ["评分数", "评分数量", "rating_count", "ratings_count"],
    "来源文件": ["来源文件", "来源", "文件名", "file"],
}

_COMPETITOR_ALIASES = {
    "ASIN": ["ASIN", "asin", "商品ASIN"],
    "父ASIN": ["父ASIN", "父asin", "parent_asin"],
    "变体ASIN": ["变体ASIN", "变体asin", "variant_asin"],
    "月销量": ["月销量", "销量", "monthly_sales"],
    "评分": ["评分", "星级", "rating"],
    "评分数": ["评分数", "评分数量", "rating_count", "ratings_count"],
    "商品标题": ["商品标题", "竞品标题", "product_title"],
    "配送方式": ["配送方式", "配送", "发货方式", "fulfillment"],
    "上架时间": ["上架时间", "上架日期", "上市时间", "list_date", "listed_date"],
    "流量词数": ["流量词数", "关键词数", "traffic_count"],
}

_RAW_COLUMNS = [
    "父ASIN", "变体ASIN", "标题", "五点描述", "品牌", "价格",
    "月销量", "评分", "商品标题", "流量词", "流量词数", "配送方式", "上架时间",
    "来源文件", "图片链接", "主图", "变体数",
    "卖家精灵-父ASIN", "查竞品-流量词数", "卖家精灵-变体ASIN",
]

_PROCESSED_COLUMNS = [
    "父ASIN", "变体ASIN", "标题", "五点描述", "主图", "图片链接",
    "品牌", "价格", "月销量", "流量词", "流量词数", "流量占比",
    "变体月销量", "变体月销额", "评分", "商品标题", "配送方式", "上架时间",
    "上架天数", "新老品", "变体数",
]

_COALESCE_COLS = {"月销量", "评分", "评分数", "价格", "品牌", "配送方式", "上架时间", "流量词数"}

# 2026-09: 流量列组仅保留"流量词数", 其余流量来源列(渠道分类/各渠道词数)输出时删除
_TRAFFIC_DROP_COLS = {
    "流量来源", "自然搜索词", "AC推荐词", "ER推荐词", "4星推荐词",
    "HR推荐词", "SP广告词", "视频广告词", "品牌广告词",
}

_PRICE_CURRENCY = r"(?:(?:US|CA|AU|MX|C|A|S|R)\$|[$€£¥￥₹₩])"
_PRICE_TEXT = rf"({_PRICE_CURRENCY}\s*[0-9][0-9.,\s]*|[0-9][0-9.,\s]*\s*{_PRICE_CURRENCY})"
_PRICE_PATTERNS = (
    r'id=[\"\']priceblock_ourprice[\"\'][^>]*>\s*' + _PRICE_TEXT + r'(?=\s*<)',
    r'id=[\"\']priceblock_dealprice[\"\'][^>]*>\s*' + _PRICE_TEXT + r'(?=\s*<)',
    r'class=[\"\'][^\"\']*\ba-offscreen\b[^\"\']*[\"\'][^>]*>\s*' + _PRICE_TEXT + r'(?=\s*<)',
    # 兜底: 页面内嵌价格 JSON(规格: 正则失败解析内嵌 JSON)
    r'"priceAmount"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?(?=\s*[,}\]]|\s*$)',
    r'"displayPrice"\s*:\s*"([^"\n]*)"',
    r'"ourPrice"\s*:\s*"([^"\n]*)"',
    r'"(?:displayPrice|ourPrice)"\s*:\s*([0-9]+(?:\.[0-9]+)?)(?=\s*[,}\]]|\s*$)',
)


class Step14TrafficSplit(BaseStep):
    number = 14
    title = "流量拆分"
    description = "FR-14: 变体明细/流量来源/竞品数据关联合并, 流量占比拆分与缺失字段补齐, 输出 3 sheet 拆分结果表"
    artifacts = ["14_流量拆分/流量拆分数据-需人工补充.xlsx"]
    archive_dirs = ["14_流量拆分"]

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        require_file(self._detail_path(ctx), f"变体明细 {os.path.basename(self._detail_path(ctx))} (FR-10 产物)")
        warn_missing(self._traffic_path(ctx), "流量来源合并 (FR-11 产物)")
        warn_missing(self._competitor_path(ctx), "竞品合并 (FR-13 产物)")
        warn_missing(self._results_path(ctx), "变体详情记录 results.jsonl (FR-10 产物)")

    def _colreg(self, cols) -> None:
        """登记列输出顺序(保持首次出现序; 明细→竞品→流量按登记段自然分组,
        计算列不登记, 输出时自然落在尾部)。"""
        if not hasattr(self, "_col_order"):
            self._col_order = []
        for c in cols:
            c = str(c)
            if c not in self._col_order:
                self._col_order.append(c)

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始流量拆分: CAT={cat}")
        self._scrape_cache = {}
        self._col_order = []
        self._site = str(ctx.config.get("SITE", "") or "")
        detail = self._load_detail(ctx)
        detail = self._merge_images(ctx, detail)
        detail = self._join_competitor(ctx, detail)
        detail = self._join_traffic(ctx, detail)
        raw = detail.copy()
        detail = self._unify_sales(ctx, detail)
        detail = self._unify_ratings(ctx, detail)
        detail = self._variant_count_and_filter(ctx, detail)
        detail = self._calc_ratio(ctx, detail)
        detail = self._clean_rows(ctx, detail)
        detail = self._fill_brand(ctx, detail)
        detail = self._split_sales(ctx, detail)
        detail = self._fill_price(ctx, detail)
        detail = self._fill_logistics(ctx, detail)
        detail = self._calc_listed_days(ctx, detail)
        self._export(ctx, raw, detail)

    def _detail_path(self, ctx: StepContext) -> str:
        return ctx.resolve_artifact(f"{ctx.config.cat}-父项变体明细.xlsx")

    def _traffic_path(self, ctx: StepContext) -> str:
        return ctx.resolve_artifact("11_查流量来源/合并-11_查流量来源.xlsx")

    def _competitor_path(self, ctx: StepContext) -> str:
        return ctx.resolve_artifact("13_查竞品下载/合并-13_查竞品下载.xlsx")

    def _results_path(self, ctx: StepContext) -> str:
        return ctx.resolve_artifact(f"{ctx.config.cat}-results.jsonl")

    def _load_detail(self, ctx: StepContext) -> pd.DataFrame:
        path = self._detail_path(ctx)
        self.log(ctx, f"1. 读取变体明细: {path}")
        require_file(path, f"变体明细 {os.path.basename(path)} (FR-10 产物)")
        try:
            df = read_excel_sheet(path, "变体明细")
        except Exception:
            self.log(ctx, "1. sheet '变体明细' 未找到, 自动定位")
            df = self._auto_locate_detail(path)
        df = self._normalize_columns(df, _DETAIL_ALIASES)
        for col in ("父ASIN", "变体ASIN"):
            if col not in df.columns:
                raise StepDataError(f"变体明细缺少必需列: {col}")
        for col in ("月销量", "评分", "流量词数"):
            # 仅对**已存在**的列数值化; 明细无这些列时不创建空列
            # (避免空列混入明细登记段/污染合并与输出分组)
            if col in df.columns:
                df[col] = self._to_float(df[col])
        weight_cols = [c for c in ("标题", "五点描述", "五点") if c in df.columns]
        before = len(df)
        df = dedup_keep_best(df, ["父ASIN", "变体ASIN"], weight_cols)
        self.log(ctx, f"1. 标准化完成, 按(父ASIN,变体ASIN)去重: {before} -> {len(df)} 行")
        # 登记明细列输出顺序: 核心列(父/变体/标题/五点/图)在前, 其余(来源ASIN等)随后
        core = [c for c in ("父ASIN", "变体ASIN", "标题", "五点描述", "图片链接") if c in df.columns]
        self._colreg(core + [c for c in df.columns if c not in core])
        return df

    def _auto_locate_detail(self, path: str) -> pd.DataFrame:
        sheets = read_excel_all_sheets(path)
        if not sheets:
            raise StepDataError(f"变体明细 Excel 无任何 sheet: {path}")
        for name in sheets:
            if "变体" in str(name):
                return sheets[name]
        first = next(iter(sheets))
        return sheets[first]

    def _merge_images(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "2. 图片链接统一(results.jsonl 全图逗号拼接, 无图保持明细原图片链接)")
        if "图片链接" not in df.columns:
            df["图片链接"] = ""
        path = self._results_path(ctx)
        if not warn_missing(path, "results.jsonl (FR-10)"):
            self.log(ctx, "2. results.jsonl 缺失, 保持明细原图片链接")
            df["__jsonl_price"] = float("nan")
            df["__jsonl_brand"] = ""
            return df
        img_map = {}
        price_map = {}
        brand_map = {}
        for rec in read_jsonl(path):
            images = _norm_images(rec.get("images"))
            req_asin = str(rec.get("request_asin") or "").strip()
            parent_asin = str(rec.get("parent_asin") or "").strip()
            if images:
                if req_asin and (req_asin not in img_map or len(images) > len(img_map[req_asin])):
                    img_map[req_asin] = images
                if parent_asin and parent_asin not in img_map:
                    img_map[parent_asin] = images
            price = rec.get("price", rec.get("价格"))
            json_site = str(ctx.config.get("SITE", "") or "")
            if isinstance(price, dict):
                for va in (rec.get("variant_asins") or []):
                    raw_price = price.get(str(va))
                    if currency_conflict(raw_price, json_site):
                        raise StepDataError(f"JSONL价格币种与{json_site}站不一致，ASIN={va}，请核对原始数据")
                    pv = _positive_price(raw_price, json_site)
                    if pv is not None:
                        price_map.setdefault(str(va), pv)
            else:
                if currency_conflict(price, json_site):
                    raise StepDataError(f"JSONL价格币种与{json_site}站不一致，ASIN={req_asin}，请核对原始数据")
                pv = _positive_price(price, json_site)
                if pv is not None and req_asin:
                    price_map.setdefault(req_asin, pv)
            brand = rec.get("brand") or rec.get("品牌")
            if brand:
                brand_val = str(brand).strip()
                asins = []
                if req_asin:
                    asins.append(req_asin)
                if parent_asin:
                    asins.append(parent_asin)
                for va in (rec.get("variant_asins") or []):
                    if va:
                        asins.append(str(va))
                for a in asins:
                    brand_map.setdefault(a, brand_val)
        get_img = self._make_lookup(img_map, fallback_parent=True)
        get_price = self._make_lookup(price_map, fallback_parent=False)
        get_brand = self._make_lookup(brand_map, fallback_parent=True)
        df["图片链接"] = df.apply(lambda r: ", ".join(get_img(r)) if get_img(r) else str(r.get("图片链接") or ""), axis=1)
        df["__jsonl_price"] = df.apply(lambda r: get_price(r), axis=1)
        df["__jsonl_brand"] = df.apply(lambda r: get_brand(r) or "", axis=1)
        return df

    def _make_lookup(self, mapping, fallback_parent=True):
        def lookup(row):
            key = str(row.get("变体ASIN") or "")
            if not key or key == "nan":
                key = str(row.get("父ASIN") or "")
            if key in mapping:
                return mapping[key]
            if fallback_parent:
                pkey = str(row.get("父ASIN") or "")
                if pkey in mapping:
                    return mapping[pkey]
            return None
        return lookup

    def _join_competitor(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self._site = str(ctx.config.get("SITE", "") or "")
        self.log(ctx, "3. 竞品数据关联(列名标准化/加卖家精灵-前缀/按 ASIN 去重保留最全/左关联)")
        path = self._competitor_path(ctx)
        if not warn_missing(path, "竞品合并 (FR-13)"):
            self.log(ctx, "3. 竞品数据缺失, 跳过关联(用空竞品表)")
            return df
        comp = read_excel_sheet(path)
        comp = self._normalize_columns(comp, _COMPETITOR_ALIASES)
        # 规格: 竞品列带来源前缀防碰撞/可溯源
        rename = {}
        if "父ASIN" in comp.columns:
            rename["父ASIN"] = "卖家精灵-父ASIN"
        if "ASIN" in comp.columns:
            rename["ASIN"] = "卖家精灵-变体ASIN"
        if "流量词数" in comp.columns:
            rename["流量词数"] = "查竞品-流量词数"
        if rename:
            comp = comp.rename(columns=rename)
        key = None
        for cand in ("卖家精灵-变体ASIN", "变体ASIN", "ASIN"):
            if cand in comp.columns:
                key = cand
                break
        if key is None:
            self.log(ctx, "3. 竞品数据缺少 ASIN 类列, 跳过关联")
            return df
        weight = [c for c in ("商品标题", "标题") if c in comp.columns]
        before = len(comp)
        comp = dedup_keep_best(comp, [key], weight)
        self.log(ctx, f"3. 竞品按 {key} 去重: {before} -> {len(comp)} 行")
        if comp.empty:
            self.log(ctx, "3. 竞品数据为空, 跳过关联")
            return df
        merged = df.merge(comp, left_on="变体ASIN", right_on=key, how="left", suffixes=("_x", "_y"))
        # 登记竞品来源列(输出分组: 竞品块)
        self._colreg([c for c in comp.columns if c != key])
        if key != "变体ASIN" and key in merged.columns:
            merged = merged.drop(columns=[key])
        merged = self._resolve_suffixes(merged)
        self.log(ctx, f"3. 关联后行数: {len(merged)}")
        return merged

    def _join_traffic(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "4. 流量来源关联(多文件合并标记来源/清洗/按流量词数降序去重/左关联)")
        traffic = self._load_traffic(ctx)
        if traffic is None:
            self.log(ctx, "4. 流量来源缺失, 跳过关联(流量词置空)")
            return self._empty_traffic_cols(df)
        traffic = self._normalize_columns(traffic, _TRAFFIC_ALIASES)
        # 2026-09 变更: 主价数据源=卖家精灵竞品导出价列; 流量来源自带的"价格"列弃用
        # (避免与竞品"价格($)"等主价列并存混淆), 关联前移除
        drop_price = [c for c in traffic.columns
                      if _PRICE_RE.match(str(c).strip()) or _PRICE_RE_EN.match(str(c).strip())]
        if drop_price:
            traffic = traffic.drop(columns=drop_price, errors="ignore")
        key = None
        for cand in ("变体ASIN", "ASIN", "父ASIN"):
            if cand in traffic.columns:
                key = cand
                break
        if key is None:
            self.log(ctx, "4. 流量来源缺少 ASIN 类列, 跳过关联")
            return self._empty_traffic_cols(df)
        traffic["变体ASIN"] = traffic[key].fillna("").astype(str).str.strip()
        traffic = traffic[traffic["变体ASIN"] != ""]
        if traffic.empty:
            self.log(ctx, "4. 流量来源清洗后为空, 跳过关联")
            return self._empty_traffic_cols(df)
        traffic["流量词数"] = self._to_float(traffic.get("流量词数"))
        traffic["流量词数"] = traffic["流量词数"].where(traffic["流量词数"] >= 0)
        traffic = traffic.sort_values("流量词数", ascending=False, na_position="last")
        traffic = traffic.drop_duplicates(subset=["变体ASIN"], keep="first")
        traffic = traffic.reset_index(drop=True)
        keep = [c for c in traffic.columns if c not in ("变体ASIN", key, "父ASIN")]
        # 登记流量来源列(输出分组: 流量块; 同名冲突列已先登记在竞品块的保持原组)
        self._colreg(keep)
        right = traffic[["变体ASIN"] + keep]
        merged = df.merge(right, on="变体ASIN", how="left", suffixes=("_x", "_y"))
        merged = self._resolve_suffixes(merged, prefer_right={"月销量", "评分", "评分数", "流量词数"})
        self.log(ctx, f"4. 流量来源去重后 {len(traffic)} 行, 关联后总行数 {len(merged)}")
        return merged

    def _empty_traffic_cols(self, df: pd.DataFrame) -> pd.DataFrame:
        if "流量词" not in df.columns:
            df["流量词"] = ""
        if "流量词数" not in df.columns:
            df["流量词数"] = float("nan")
        return df

    def _load_traffic(self, ctx: StepContext) -> pd.DataFrame | None:
        path = self._traffic_path(ctx)
        if os.path.exists(path):
            df = read_excel_sheet(path)
            if "来源文件" not in df.columns:
                df["来源文件"] = os.path.basename(path)
            return df
        directory = os.path.dirname(path)
        files = []
        if os.path.isdir(directory):
            merged_name = os.path.basename(path)
            files = sorted(
                f for f in os.listdir(directory)
                if f.endswith(".xlsx") and not f.startswith("~$") and f != merged_name
            )
        frames = []
        for f in files:
            try:
                fdf = read_excel_sheet(os.path.join(directory, f))
            except Exception:
                continue
            fdf["来源文件"] = f
            frames.append(fdf)
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True, sort=False)

    def _variant_count_and_filter(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "5. 变体数量统计与过滤(单变体父体 或 多变体且流量词数>0)")
        df["变体数"] = df.groupby("父ASIN")["变体ASIN"].transform("nunique")
        cnt = self._to_float(df.get("流量词数")).reindex(df.index)
        df["流量词数"] = cnt.where(cnt >= 0)
        cnt = df["流量词数"]
        multi = df["变体数"] > 1
        # 用户口径：缺失/零流量变体不参与分配，其余正流量变体承接全部父体销量。
        # 多变体父体整组无正流量时直接整组过滤，原始记录保留但不生成流量待补表。
        has_traffic = cnt > 0
        no_positive = multi & ~has_traffic.groupby(df["父ASIN"]).transform("any")
        self.log(ctx, f"5. 整组无正流量的多变体父体 {df.loc[no_positive, '父ASIN'].nunique()} 个 / {int(no_positive.sum())} 行, 整组过滤不参与拆分")
        excluded = multi & ~has_traffic
        self.log(ctx, f"5. 按已知正流量分配全部父体销量; 不参与分配的缺失/零流量变体 {int(excluded.sum())} 行(保留于原始表)")
        before = len(df)
        keep = (df["变体数"] == 1) | has_traffic
        df = df[keep].reset_index(drop=True)
        self.log(ctx, f"5. 变体统计完成, 过滤后: {before} -> {len(df)} 行")
        return df

    def _calc_ratio(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "6. 流量占比计算(父体内流量词数占比, 单变体为 1)")
        if "流量词数" not in df.columns:
            df["流量词数"] = float("nan")
        df["流量词数"] = self._to_float(df["流量词数"])
        parent_total = df.groupby("父ASIN")["流量词数"].transform("sum")
        # 父体总流量 = 该父体内(保留变体)流量词数之和(占比分母; 无流量数据父体留空)
        df["父体总流量"] = parent_total.where(df["变体数"] > 1)
        ratio = df["流量词数"] / parent_total
        ratio = ratio.where(parent_total > 0)
        df["流量占比"] = ratio
        df.loc[df["变体数"] == 1, "流量占比"] = 1.0
        if df["流量占比"].isna().any():
            raise StepDataError("步骤14存在无法计算流量占比的多变体，禁止使用占比1兜底")
        # 保留计算精度，避免极小的正占比四舍五入为零后无法拆分。
        return df

    def _unify_sales(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "5前置. 月销量统一(过滤前同父ASIN 取最大有效月销量)")
        if "月销量" not in df.columns:
            df["月销量"] = float("nan")
        df["月销量"] = self._to_float(df["月销量"])
        df["月销量"] = df["月销量"].where(df["月销量"] >= 0)
        parent_max = df.groupby("父ASIN")["月销量"].transform("max")
        df["月销量"] = parent_max
        return df

    def _unify_ratings(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "5前置. 评分及评分数统一(过滤前同父ASIN分别取最大有效值)")
        for col in ("评分", "评分数"):
            if col not in df.columns:
                df[col] = float("nan")
            values = self._to_float(df[col])
            valid = values >= 0
            if col == "评分":
                valid &= values <= 5
            else:
                valid &= values.mod(1) == 0
            df[col] = values.where(valid).groupby(df["父ASIN"]).transform("max")
        return df

    def _clean_rows(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        # 2026-09: 月销量(父级统一补全后)做一轮筛选——保留月销量 > 阈值 的行(默认 10, 可配)
        min_sales = self._min_sales(ctx)
        self.log(ctx, f"8. 行清洗(月销量统一补全后筛选: 保留月销量 > {min_sales:g} 的行, 评分空值补 0)")
        before = len(df)
        df = df[df["月销量"].notna() & (df["月销量"] > min_sales)].reset_index(drop=True)
        self.log(ctx, f"8. 删除月销量 ≤{min_sales:g} 行: {before} -> {len(df)}")
        if "评分" in df.columns:
            df["评分"] = self._to_float(df["评分"]).fillna(0.0)
        else:
            df["评分"] = 0.0
        return df

    def _min_sales(self, ctx: StepContext) -> float:
        value = ctx.config.get("MONTHLY_SALES_MIN", 10)
        return float(10 if value is None or value == "" else value)

    def _fill_brand(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "9. 品牌填补(本地推断 -> 网页补抓 -> 兜底'未知')")
        if "品牌" not in df.columns:
            df["品牌"] = ""
        df["品牌"] = df["品牌"].fillna("").astype(str).str.strip()
        if "__jsonl_brand" in df.columns:
            jb = df["__jsonl_brand"].fillna("").astype(str).str.strip()
            jb = jb.replace("nan", "")
            mask = df["品牌"] == ""
            df.loc[mask, "品牌"] = jb[mask]
        missing = df["品牌"] == ""
        for idx in df.index[missing]:
            brand = self._scrape_brand(ctx, str(df.at[idx, "变体ASIN"]))
            if brand:
                df.at[idx, "品牌"] = brand
        df["品牌"] = df["品牌"].replace("", "未知")
        return df

    def _split_sales(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "10. 销量拆分(占比=1 取月销量, 多变体 ceil(月销量 x 流量占比))")
        df["流量占比"] = self._to_float(df["流量占比"])
        if (df["流量占比"].isna() | (df["流量占比"] <= 0) | (df["流量占比"] > 1)).any():
            raise StepDataError("步骤14流量占比缺失或超出(0,1]，禁止拆分销量")
        df["变体月销量"] = (df["月销量"] * df["流量占比"]).apply(math.ceil)
        return df

    def _fill_price(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        """11. 价格补齐(2026-09 变更: 主价=卖家精灵竞品导出价列, 币种智能识别).

        主价列 = 智能识别出的竞品价列(价格($)/价格(CA$)/价格(£)...), 列名随站点币种;
        主价缺失行 -> jsonl 价 -> 网页补抓兜底; **补不到不删行**, 保留交人工待补充表并 log 反馈;
        月销额 = round(变体月销量 × 主价, 2), 无价行留空。
        """
        site = str(ctx.config.get("SITE", "") or "")
        main_col, prime_col, cur = _price_cols_in(df, site)
        if main_col is None:
            df["价格"] = float("nan")  # 无任何价列(竞品缺失等): 建内部兜底列
            main_col = "价格"
        pc = main_col
        _validate_price_currency(site, pc, df[pc])
        if "__jsonl_price" in df.columns:
            _validate_price_currency(site, "JSONL价格", df["__jsonl_price"])
        self.log(
            ctx,
            f"11. 价格补齐: 主价列=[{pc}] 币种={cur}"
            + (f" prime列=[{prime_col}]" if prime_col else "")
            + " (竞品导出价 -> JSONL -> 网页补抓; 补不到保留交人工)",
        )
        df[pc] = df[pc].map(lambda value: _positive_price(value, site)).astype("float64")
        if "__jsonl_price" in df.columns:
            fallback = df["__jsonl_price"].map(lambda value: _positive_price(value, site)).astype("float64")
            df[pc] = df[pc].fillna(fallback)
        missing = df[pc].isna()
        fetched = 0
        for idx in df.index[missing]:
            price = _positive_price(self._scrape_price(ctx, str(df.at[idx, "变体ASIN"])), site)
            if price is not None:
                df.at[idx, pc] = float(price)
                fetched += 1
        still = int(df[pc].isna().sum())
        self.log(
            ctx,
            f"11. 价格补齐完成: 补抓成功 {fetched}, 仍无价 {still} 行"
            "(保留不删, 已计入人工待补充表; 缺失原因多为不可售/下架/风控)",
        )
        self.log(ctx, "11. 价格补齐后重算变体月销额(round(变体月销量 x 价格), 无价行留空)")
        df["变体月销额"] = (df["变体月销量"] * df[pc]).round(2)
        self._price_main_col = pc
        self._price_prime_col = prime_col
        self._price_currency = cur
        return df

    def _fill_logistics(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "12. 卖家/物流补齐: 先补 BuyBox卖家、卖家所属地、上架时间及配送方式，再按 BuyBox卖家校正 AMZ 配送方式")
        buybox_col = _find_buybox_col(df) or "BuyBox卖家"
        for col in (buybox_col, "卖家所属地", "配送方式", "上架时间"):
            if col not in df.columns:
                df[col] = ""
            df[col] = df[col].fillna("").astype(str).str.strip()
        self._mcp_fill_logistics(ctx, df, buybox_col=buybox_col)
        amz_fixed = self._enforce_amazon_fulfillment(df, buybox_col)
        if amz_fixed:
            self.log(ctx, f"12. BuyBox卖家含 amazon(忽略大小写) → 配送方式校正为 AMZ: {amz_fixed} 行")
        by_seller, by_brand = self._sync_seller_nation(df, buybox_col)
        if by_seller or by_brand:
            self.log(
                ctx,
                f"12. 卖家所属地同表同步(跳过 AMZ 自营): 同 BuyBox卖家唯一同步 {by_seller} 行, 同品牌唯一同步 {by_brand} 行",
            )
        self.log(ctx, "13. 兜底: 配送方式为空标'未知'(新老品缺失在步骤 14 标'未知')")
        df["配送方式"] = df["配送方式"].replace("", "未知")
        return df

    @staticmethod
    def _enforce_amazon_fulfillment(df: pd.DataFrame, buybox_col: str) -> int:
        if df.empty or not buybox_col or buybox_col not in df.columns:
            return 0
        mask = df[buybox_col].map(_is_amazon_seller) & (df["配送方式"].str.upper() != "AMZ")
        cnt = int(mask.sum())
        if cnt:
            df.loc[mask, "配送方式"] = "AMZ"
        return cnt

    @staticmethod
    def _sync_seller_nation(df: pd.DataFrame, buybox_col: str) -> tuple[int, int]:
        """表内同步非 Amazon 自营行的缺失卖家所属地:
        1) 优先按同 BuyBox卖家 唯一已知所属地同步;
        2) 再按同 品牌 唯一已知所属地同步(排除 Generic/Unbranded/未知 等无品牌占位词;
           同品牌存在多个不同所属地冲突时跳过不猜测);
        3) BuyBox卖家含 amazon(忽略大小写) 或 配送方式==AMZ 的自营行不参与同步。
        """
        if df.empty or "卖家所属地" not in df.columns:
            return 0, 0
        has_bb = bool(buybox_col and buybox_col in df.columns)
        has_brand = "品牌" in df.columns
        if not has_bb and not has_brand:
            return 0, 0

        bb_s = df[buybox_col].map(_clean_mcp_str) if has_bb else pd.Series("", index=df.index)
        bb_k = bb_s.str.casefold()
        br_s = df["品牌"].map(_clean_mcp_str) if has_brand else pd.Series("", index=df.index)
        br_k = br_s.str.casefold()
        nation_s = df["卖家所属地"].map(_clean_mcp_str)
        ful_u = df["配送方式"].fillna("").astype(str).str.strip().str.upper() if "配送方式" in df.columns else pd.Series("", index=df.index)
        is_amz = bb_s.map(_is_amazon_seller) | (ful_u == "AMZ")

        seller_nations: dict[str, set[str]] = {}
        brand_nations: dict[str, set[str]] = {}
        for idx in df.index:
            if is_amz.at[idx]:
                continue
            nat = nation_s.at[idx]
            if not nat:
                continue
            sk = bb_k.at[idx]
            if sk:
                seller_nations.setdefault(sk, set()).add(nat)
            bk = br_k.at[idx]
            if bk and bk not in _GENERIC_BRANDS:
                brand_nations.setdefault(bk, set()).add(nat)

        seller_unique = {k: next(iter(v)) for k, v in seller_nations.items() if len(v) == 1}
        brand_unique = {k: next(iter(v)) for k, v in brand_nations.items() if len(v) == 1}

        by_seller = 0
        by_brand = 0
        for idx in df.index:
            if is_amz.at[idx] or nation_s.at[idx]:
                continue
            sk = bb_k.at[idx]
            if sk and sk in seller_unique:
                df.at[idx, "卖家所属地"] = seller_unique[sk]
                by_seller += 1
                continue
            bk = br_k.at[idx]
            if bk and bk not in _GENERIC_BRANDS and bk in brand_unique:
                df.at[idx, "卖家所属地"] = brand_unique[bk]
                by_brand += 1
        return by_seller, by_brand

    def _mcp_fill_logistics(self, ctx: StepContext, df: pd.DataFrame, *, buybox_col: str | None = None) -> None:
        """规格 FR-14-12: MCP competitor_lookup(asin.list) 补齐 BuyBox卖家/卖家所属地/上架时间/配送方式。

        顺序: 先补齐 BuyBox卖家(sellerName)、卖家所属地(sellerNation)、上架时间(availableDate)与原始 fulfillment;
              回填完成后由 _fill_logistics 统一按 BuyBox卖家含 amazon(忽略大小写) 将配送方式校正为 AMZ。
        候选: BuyBox卖家为空、非 Amazon 行的卖家所属地为空、配送方式为空(且非已知 Amazon)、或上架时间为空的变体;
        月份: 配置月(趋势/竞品/流量 优先级依次回退)往前共 6 个月(最新在前); 最近30天先查 NEAR_30_DAYS，缺失再往前逐月查最多 6 个自然月;
        分批: 按月由近到远循环, 每月仅汇总仍缺字段的变体重新按 40 个/批打包调 competitor_lookup(全补齐即提前结束), 每月每批结果写临时文件;
        取值: sellerName/sellerNation/fulfillment 仅取非空且 ≠NA; **只取最近月份的首次命中**(更早月份不覆盖);
        兜底: 6 个月查不到→配送方式标"未知"(步骤 13 兜底处理); 结束删临时文件; 单月失败不中断。
        """
        if df.empty:
            return
        if not buybox_col:
            buybox_col = _find_buybox_col(df) or "BuyBox卖家"
        for col in (buybox_col, "卖家所属地", "配送方式", "上架时间"):
            if col not in df.columns:
                df[col] = ""
        month = (
            str(ctx.config.get("TREND_MONTH", "") or "")
            or str(ctx.config.get("COMPETITOR_MONTH", "") or "")
            or str(ctx.config.get("TRAFFIC_MONTH", "") or "")
        )
        if not month:
            self.log(ctx, "12. 缺少历史月份, 跳过MCP补齐(缺失将兜底标'未知')")
            return
        if is_recent30(month):
            now = datetime.now()
            prev_ord = (now.year - 1) * 12 + now.month - 2  # 上一个已完成自然月
            prev_month = f"{prev_ord // 12 + 1:04d}-{prev_ord % 12 + 1:02d}"
            hist_months = _recent_months(prev_month, 6)  # 最新在前，共 6 个月
            months = [""] + hist_months  # 先查 NEAR_30_DAYS，未补齐的继续往前逐月查最多 6 个月
            month_label = f"最近30天 + {hist_months[-1]}~{hist_months[0]}"
        else:
            months = _recent_months(month, 6)  # 最新在前
            month_label = f"{months[-1]}~{months[0]}"

        def _row_needs(idx) -> bool:
            bb = str(df.at[idx, buybox_col] or "").strip()
            nation = str(df.at[idx, "卖家所属地"] or "").strip()
            ful = str(df.at[idx, "配送方式"] or "").strip()
            listed = str(df.at[idx, "上架时间"] or "").strip()
            is_amz = _is_amazon_seller(bb) or ful.upper() == "AMZ"
            return (
                (bb == "")
                or (listed == "")
                or (ful == "" and not _is_amazon_seller(bb))
                or (nation == "" and not is_amz)
            )

        need = [idx for idx in df.index if _row_needs(idx)]
        if not need:
            self.log(ctx, "12. 无待补变体(BuyBox卖家/卖家所属地/配送方式/上架时间均已有值)")
            return
        query = getattr(ctx.mcp(), "competitor_lookup", None)
        if not callable(query):
            self.log(ctx, "12. MCP competitor_lookup 接口不可用, 跳过(缺失将兜底标'未知')")
            return
        batch = 40  # 规格: 每 40 个一批(接口上限)
        tmpdir = os.path.join(ctx.paths.cat_root, "12_查竞品", "_mcp_tmp")
        os.makedirs(tmpdir, exist_ok=True)
        self.log(
            ctx,
            f"12. MCP 按月分批查询: 待补 {len(need)} 行, 每批上限 {batch}, 月份 {month_label}",
        )
        filled = 0
        try:
            for mi, m in enumerate(months, 1):
                pending_rows = [i for i in need if _row_needs(i)]
                if not pending_rows:
                    break
                month_batches = (len(pending_rows) + batch - 1) // batch
                for bi, start in enumerate(range(0, len(pending_rows), batch), 1):
                    chunk = pending_rows[start : start + batch]
                    chunk_asins = list(
                        dict.fromkeys(
                            str(df.at[i, "变体ASIN"]).strip()
                            for i in chunk
                            if str(df.at[i, "变体ASIN"]).strip() and str(df.at[i, "变体ASIN"]).strip() != "nan"
                        )
                    )
                    if not chunk_asins:
                        continue
                    try:
                        items = query(m, chunk_asins) or []
                    except Exception as exc:
                        self.log(ctx, f"12. 月份 {m or '最近30天'} 批次 {bi}/{month_batches} 查询失败(不中断): {str(exc)[:120]}")
                        items = []
                    # 每月每批结果写临时文件(规格)
                    tmp = os.path.join(tmpdir, f"mcp-{m or 'recent30'}-b{bi}.json")
                    try:
                        with open(tmp, "w", encoding="utf-8") as fh:
                            json.dump(items, fh, ensure_ascii=False)
                    except Exception:
                        pass
                    # 回填顺序: 先补 BuyBox卖家 与 卖家所属地, 再补 上架时间 与 配送方式(最近月份优先，不覆盖已有值)
                    for item in items or []:
                        if not isinstance(item, dict):
                            continue
                        asin = str(item.get("asin") or "").strip()
                        if not asin:
                            continue
                        hit = df.index[df["变体ASIN"].astype(str).str.strip() == asin]
                        if len(hit) == 0:
                            continue
                        idx = hit[0]
                        seller_name = _clean_mcp_str(item.get("sellerName"))
                        if df.at[idx, buybox_col] == "" and seller_name:
                            df.at[idx, buybox_col] = seller_name
                            filled += 1
                        cur_is_amz = _is_amazon_seller(df.at[idx, buybox_col]) or _is_amazon_seller(seller_name)
                        nation = _clean_mcp_str(item.get("sellerNation"))
                        if not cur_is_amz and df.at[idx, "卖家所属地"] == "" and nation:
                            df.at[idx, "卖家所属地"] = nation
                            filled += 1
                        ad = item.get("availableDate")
                        if df.at[idx, "上架时间"] == "" and ad not in (None, ""):
                            if isinstance(ad, str) and len(ad.strip()) >= 10 and ad.strip()[4:5] == "-":
                                df.at[idx, "上架时间"] = ad.strip()[:10]
                                filled += 1
                            else:
                                try:
                                    ts = int(ad)
                                    if ts > 0:
                                        df.at[idx, "上架时间"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                                        filled += 1
                                except (TypeError, ValueError, OSError):
                                    pass
                        ful = _clean_mcp_str(item.get("fulfillment"))
                        if df.at[idx, "配送方式"] == "" and ful:
                            df.at[idx, "配送方式"] = ful
                            filled += 1
                remain = sum(1 for i in pending_rows if _row_needs(i))
                self.log(
                    ctx,
                    f"12. MCP 月份 {m or '最近30天'}({mi}/{len(months)}) 完成: 查询 {len(pending_rows)} 行({month_batches} 批), 累计回填 {filled} 个字段值, 仍缺 {remain} 行",
                )
        finally:
            try:
                for f in os.listdir(tmpdir):
                    if f.startswith("mcp-"):
                        os.remove(os.path.join(tmpdir, f))
            except Exception:
                pass
        self.log(ctx, f"12. MCP 补齐完成: 回填 {filled} 个字段值(6 个月查不到的配送方式将由步骤 13 标'未知')")

    def _calc_listed_days(self, ctx: StepContext, df: pd.DataFrame) -> pd.DataFrame:
        self.log(ctx, "14. 上架天数与新老品(<=180 天=新品, >180 天=老品, 缺失=未知)")
        listed = pd.to_datetime(df.get("上架时间"), errors="coerce")
        today = pd.Timestamp(datetime.now().date())
        days = (today - listed).dt.days
        df["上架天数"] = days
        df["新老品"] = "未知"
        df.loc[days.notna() & (days <= 180), "新老品"] = "新品"
        df.loc[days.notna() & (days > 180), "新老品"] = "老品"
        fmt = listed.dt.strftime("%Y-%m-%d")
        df["上架时间"] = fmt.where(listed.notna(), "")
        return df

    def _export(self, ctx: StepContext, raw: pd.DataFrame, detail: pd.DataFrame) -> None:
        self.log(ctx, "15. 输出 3 sheet(原始表/AI已补充/人工待补充表)")
        raw_out = self._finalize_raw(raw)
        auto_out = self._finalize_processed(detail)
        pc = getattr(self, "_price_main_col", None) or "价格"
        price_missing = (
            auto_out[pc].isna()
            if pc in auto_out.columns
            else pd.Series(False, index=auto_out.index)
        )
        manual = auto_out[
            (auto_out["配送方式"] == "未知")
            | (auto_out["新老品"] == "未知")
            | price_missing
        ].copy()
        out_path = ctx.resolve_artifact(self.artifacts[0])
        self._publish_workbook(out_path, {
            "原始表": raw_out,
            "AI已补充": auto_out,
            "人工待补充表": manual,
        })
        self.log(ctx, f"15. 输出完成: {out_path}")
        self.log(
            ctx,
            f"15. 行数统计: 原始表 {len(raw_out)} / AI已补充 {len(auto_out)}"
            f" / 人工待补充表 {len(manual)} (其中价格空 {int(price_missing.sum())} 行)",
        )

    def _publish_workbook(self, out_path: str, sheets: dict[str, pd.DataFrame]) -> None:
        out_dir = os.path.dirname(out_path) or "."
        os.makedirs(out_dir, exist_ok=True)
        fd, pending = tempfile.mkstemp(dir=out_dir, prefix="流量拆分数据-待发布-", suffix=".xlsx")
        os.close(fd)
        try:
            with pd.ExcelWriter(pending, engine="openpyxl") as writer:
                for name, frame in sheets.items():
                    frame.to_excel(writer, sheet_name=name, index=False)
        except BaseException:
            try:
                os.remove(pending)
            except OSError:
                pass
            raise
        try:
            os.replace(pending, out_path)
        except OSError as exc:
            raise StepFailedError(
                f"步骤14无法更新产物 {out_path}（可能被Excel占用或无写入权限）。"
                f"本轮完整结果已保留在待发布文件 {pending}。"
                "请解除占用或检查权限后重跑步骤14；本步未完成，不放行下游。"
            ) from exc

    def _finalize_raw(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._drop_helper(df)
        out = out[[c for c in out.columns if c not in _TRAFFIC_DROP_COLS]]
        if "变体数" not in out.columns:
            out["变体数"] = out.groupby("父ASIN")["变体ASIN"].transform("nunique")
        if "图片链接" in out.columns:
            out["主图"] = out["图片链接"].fillna("").astype(str).str.split(",").str[0].str.strip()
        cols = [c for c in _RAW_COLUMNS if c in out.columns]
        extra = [c for c in out.columns if c not in cols]
        return out[cols + extra].reset_index(drop=True)

    def _finalize_processed(self, df: pd.DataFrame) -> pd.DataFrame:
        """AI已补充(最终处理明细):
        - 保留与原始表一致的列集(来源列一列不丢) + 处理结果列;
        - **删除溯源/过程列**: 来源文件/是否成功/状态/抓取时间;
        - **按来源分组排序**: 明细核心列 → 竞品列(卖家精灵) → 流量列 → 计算列, 便于查看。"""
        from core.traffic_fields import PROCESS_ONLY_TRAFFIC_FIELDS
        remove_cols = {"来源文件", "是否成功", "状态", "抓取时间"} | _TRAFFIC_DROP_COLS | PROCESS_ONLY_TRAFFIC_FIELDS
        out = self._drop_helper(df)
        if "图片链接" in out.columns:
            out["主图"] = out["图片链接"].fillna("").astype(str).str.split(",").str[0].str.strip()
        out = out[[c for c in out.columns if c not in remove_cols]]
        # 分组排序: 明细登记列 → 竞品登记列 → 流量登记列; 未登记的计算列(流量占比/变体月销量等)置尾
        order = [c for c in getattr(self, "_col_order", []) if c in out.columns]
        rest = [c for c in out.columns if c not in order]
        # 主图紧随图片链接(图列相邻)
        if "主图" in rest and "图片链接" in order:
            order.insert(order.index("图片链接") + 1, "主图")
            rest.remove("主图")
        return out[order + rest].reset_index(drop=True)

    def _drop_helper(self, df: pd.DataFrame) -> pd.DataFrame:
        return df[[c for c in df.columns if not str(c).startswith("__")]].copy()

    def _normalize_columns(self, df: pd.DataFrame, aliases: dict) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        rename = {}
        for col in df.columns:
            key = str(col).strip()
            canon = _find_canonical(key, aliases)
            if canon and canon not in rename.values() and canon not in df.columns:
                rename[col] = canon
        if rename:
            df = df.rename(columns=rename)
        return df

    def _to_float(self, series) -> pd.Series:
        if series is None:
            return pd.Series(dtype="float64")
        s = pd.to_numeric(series.map(_clean_number), errors="coerce")
        return s.astype("float64").where(lambda values: values.map(math.isfinite))

    def _resolve_suffixes(self, df: pd.DataFrame, prefer_right=()) -> pd.DataFrame:
        cols = list(df.columns)
        bases = sorted({c[:-2] for c in cols if c.endswith(("_x", "_y"))})
        for base in bases:
            x = f"{base}_x"
            y = f"{base}_y"
            if x in cols and y in cols:
                if base in {"月销量", "评分", "评分数", "流量词数"}:
                    for col in (x, y):
                        values = self._to_float(df[col])
                        valid = values >= 0
                        if base == "评分":
                            valid &= values <= 5
                        elif base == "评分数":
                            valid &= values.mod(1) == 0
                        df[col] = values.where(valid)
                elif _PRICE_RE.match(base) or _PRICE_RE_EN.match(base):
                    for col in (x, y):
                        df[col] = df[col].map(lambda value: _positive_price(value, getattr(self, "_site", "")))
                if base in prefer_right:
                    df[base] = df[y].fillna(df[x])
                elif base in _COALESCE_COLS or _PRICE_RE.match(base) or _PRICE_RE_EN.match(base):
                    df[base] = df[x].fillna(df[y])
                else:
                    df[base] = df[x]
                df = df.drop(columns=[x, y])
        return df

    def _scrape_page(self, ctx: StepContext, asin: str) -> str:
        """登录态网页补抓(规格: 带 COOKIE + 每请求间隔2秒 + 结果缓存)。"""
        cache = getattr(self, "_scrape_cache", None)
        if cache is None:
            cache = {}
            self._scrape_cache = cache
        if asin in cache:
            return cache[asin]
        fetcher = getattr(self, "_scrape_fetcher", None)
        if fetcher is None:
            cookie = str(ctx.config.get("COOKIE", "") or "") or None
            fetcher = HttpFetcher(ctx.config, cookie=cookie)
            self._scrape_fetcher = fetcher
        text = ""
        try:
            resp = fetcher.get(f"https://{ctx.config.market}/dp/{asin}")
            if getattr(resp, "ok", False):
                text = resp.text or ""
        except Exception:
            text = ""
        cache[asin] = text
        time.sleep(2)  # 规格: 每请求间隔 2 秒
        return text

    def _scrape_brand(self, ctx: StepContext, asin: str) -> str:
        if not asin or asin == "nan":
            return ""
        return _parse_brand(self._scrape_page(ctx, asin))

    def _scrape_price(self, ctx: StepContext, asin: str):
        if not asin or asin == "nan":
            return None
        return _parse_price(self._scrape_page(ctx, asin), str(ctx.config.get("SITE", "") or ""))


def _find_canonical(name: str, aliases: dict):
    for canon, names in aliases.items():
        if name == canon or name in names:
            return canon
    return None


def _clean_number(value):
    if isinstance(value, str):
        t = value.strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace(" ", "")
        try:
            return float(t)
        except ValueError:
            return value
    return value


def _positive_price(value, site: str = ""):
    """价格专用解析；数量字段仍使用千位逗号规则。拒绝非正数、非有限数及歧义文本。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = unescape(value).strip()
        if site and currency_conflict(text, site):
            return None
        text = strip_currency(text).strip()
        if re.search(r"[0-9]\s+[0-9]", text):
            if not re.fullmatch(r"[0-9]{1,3}(?:\s+[0-9]{3})+(?:[.,][0-9]{1,2})?", text):
                return None
        text = re.sub(r"\s+", "", text)
        if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)*", text):
            return None
        comma_decimal = site in {"德国", "法国", "意大利", "西班牙", "荷兰", "瑞典", "巴西"}
        if "," in text and "." in text:
            decimal = "," if text.rfind(",") > text.rfind(".") else "."
            grouping = "." if decimal == "," else ","
            integer, fraction = text.rsplit(decimal, 1)
            if len(fraction) not in (1, 2) or not re.fullmatch(rf"[0-9]{{1,3}}(?:\{grouping}[0-9]{{3}})+", integer):
                return None
            text = integer.replace(grouping, "") + "." + fraction
        elif "," in text or "." in text:
            sep = "," if "," in text else "."
            parts = text.split(sep)
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                text = ".".join(parts)
            elif re.fullmatch(rf"[0-9]{{1,3}}(?:\{sep}[0-9]{{3}})+", text):
                # 与站点小数符相同的三位尾数有歧义，不猜测成千位价格。
                if (sep == "," and comma_decimal) or (sep == "." and not comma_decimal):
                    return None
                text = "".join(parts)
            else:
                return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _norm_images(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [u for u in (x.strip() for x in re.split(r"[,;\s]+", raw)) if u]
    items = []
    for it in raw:
        if isinstance(it, str):
            u = it.strip()
        elif isinstance(it, dict):
            u = str(it.get("url") or it.get("link") or it.get("src") or "").strip()
        else:
            u = str(it).strip()
        if u and u not in items:
            items.append(u)
    return items


def _recent_months(month: str, n: int) -> list:
    m = str(month).strip()
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise StepDataError("历史月份数量必须是非负整数")
    if not re.fullmatch(r"[0-9]{4}-?[0-9]{2}", m):
        raise StepDataError(f"无效历史月份 {month!r}，请使用YYYYMM或YYYY-MM")
    digits = m.replace("-", "")
    year, month_number = int(digits[:4]), int(digits[4:])
    if not 1 <= year <= 9999 or not 1 <= month_number <= 12:
        raise StepDataError(f"无效历史月份 {month!r}")
    ordinal = (year - 1) * 12 + month_number - 1
    if n > ordinal + 1:
        raise StepDataError("历史月份回退超出有效年份范围")
    return [f"{(ordinal - i) // 12 + 1:04d}-{(ordinal - i) % 12 + 1:02d}" for i in range(n)]


def _parse_price(html: str, site: str = ""):
    if not html:
        return None
    for pat in _PRICE_PATTERNS:
        for m in re.finditer(pat, html):
            # JSON数值本身采用点小数，与站点展示格式无关。
            machine_number = '"priceAmount"' in pat or "(?:displayPrice|ourPrice)" in pat
            val = _positive_price(m.group(1), "" if machine_number else site)
            if val is not None:
                return val
    # 整数/小数分节点时必须成对读取，不能把整数部分当成完整售价。
    split_pattern = (r'class=[\"\']a-price-whole[\"\'][^>]*>(.*?)</span>\s*'
                     r'<span[^>]*class=[\"\']a-price-fraction[\"\'][^>]*>\s*([0-9]{1,2})\s*</span>')
    for match in re.finditer(split_pattern, html, re.S):
        whole = unescape(re.sub(r"<[^>]*>", "", match.group(1))).strip().rstrip(".,")
        integer = _positive_price(whole, site)
        if whole == "0":
            integer = 0.0
        if integer is not None and integer.is_integer():
            value = _positive_price(integer + int(match.group(2)) / (10 ** len(match.group(2))))
            if value is not None:
                return value
    return None


def _parse_brand(html: str) -> str:
    if not html:
        return ""
    m = re.search(r'id="bylineInfo"[^>]*>\s*(.*?)\s*</a>', html, re.S)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^(Visit the|Brand:|品牌[:：]?)\s*", "", text)
        text = text.replace(" Store", "").strip()
        if text and len(text) < 80:
            return text
    m = re.search(r"Brand</th>\s*<td[^>]*>\s*(.*?)\s*</td>", html, re.S)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) < 80:
            return text
    return ""


_GENERIC_BRANDS = frozenset({
    "", "generic", "unbranded", "unknown", "未知", "n/a", "na", "none", "null", "-", "--",
})


def _clean_mcp_str(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    s = str(value).strip()
    if s.casefold() in ("", "na", "n/a", "null", "none", "nan", "-", "--", "未知"):
        return ""
    return s


def _is_amazon_seller(value) -> bool:
    s = _clean_mcp_str(value)
    return bool(s) and ("amazon" in s.casefold())


def _find_buybox_col(df: pd.DataFrame) -> str | None:
    lookup = {str(c).strip().casefold(): c for c in df.columns}
    for name in ("BuyBox卖家", "buybox seller", "购物车卖家"):
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    return None
