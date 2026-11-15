"""LLM 价格表: 实时抓取官网 + 缓存 + 三级兜底 (Step 1)。

## 为什么要抓官网(2026-09-28 实测结论)

- 官方**没有**机器可读接口: `…/pricing.md` 与 `/llms.txt` 都 SPA 回落到首页,
  返回的是 "Your First API Call" 内容 —— 只能解析 HTML。
- 好消息是结构极规整: 恰好 1 个 `<table>`、21 行, 价格块在 r14-r19。
- **币种陷阱**: 中文页是「元」、英文页是「$」, 数字完全不同
  (flash 输出 4元 vs $0.6)。不校验币种就会把美元当人民币报 → 必须校验。
- **rowspan 陷阱**: 价格行单元格数不一致(`【价格,度量名,空闲时段,v1,v2】` 是 5 格,
  `【高峰时段,v1,v2】` 只有 3 格) → 必须按"当前度量"状态机走, 不能按固定列下标取。

## 兜底链(优先级从高到低)

1. `config.LLM_PRICES` 手填  → 显式覆盖, 永远最高优先
2. 磁盘缓存(未过期)
3. 实时抓取官网 → 校验通过 → 写缓存
4. 磁盘缓存(已过期也照用)
5. 代码内置快照 `_BUILTIN_PRICES`(最后已知价)

**抓取/解析失败绝不能让流水线失败, 也绝不能把费用静默算成 0。**
每次兜底都打 warning, 并把来源写进 `PriceTable.source`(会出现在日志与用量摘要里),
这样"看到的数是几号的价"永远是可追溯的。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

# 注: `atomic_write_text` 刻意**延迟导入** —— core/io_utils 顶层 import pandas,
# 而 core/llm.py 会 import 本模块(埋点)。放在顶层会让"只想解析 JSON 的测试"
# 也被迫加载 pandas, 白白拖慢导入。这里只在真正写缓存时才引。

# 北京时间(中国无夏令时, 固定 UTC+8 —— 不引入 tzdata 依赖)
CN_TZ = timezone(timedelta(hours=8))

# 高峰时段(北京时间, 分钟精度): 周一~周五 9:00-12:00 与 14:00-18:00。
# 决定(2026-09-28): **忽略中国法定节假日** → 节假日会被当高峰算, 费用略微高估。
# 口径选择: 宁可高估, 不低估(高估会提醒你留意成本, 低估会让你以为很便宜)。
PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((9, 12), (14, 18))

DEFAULT_PRICES_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
DEFAULT_TIMEOUT = 15.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 官方明文(定价页脚注 1): 旧模型名 deepseek-v4-flash / deepseek-v4-flash-vision-exp
# 已下线但仍可调用, 请求由 DeepSeek-V4.1-Flash 提供服务, **按 Flash 价格计费**。
# 项目里 12 个类目配置用的正是旧名 → 别名映射是必须的, 否则全部算成"未定价"。
_MODEL_ALIASES: dict[str, str] = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
}

_BUILTIN_SNAPSHOT_DATE = "2026-09-28"

# 内置兜底价(元/百万 token)。快照来源:
# https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ (2026-09-28)
# 官方调价后此表会过期 —— 但只要 mode=live 能联网, 它只在抓取失败时短暂生效,
# 且来源会明确标成「内置快照」, 不会伪装成实时价。
_BUILTIN_PRICES: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "off": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0},
        "peak": {"in_hit": 0.04, "in_miss": 2.0, "out": 8.0},
    },
    "deepseek-v4-pro": {
        "off": {"in_hit": 0.15, "in_miss": 4.5, "out": 13.5},
        "peak": {"in_hit": 0.30, "in_miss": 9.0, "out": 27.0},
    },
}

_PERIODS = ("off", "peak")
_UNITS = ("in_hit", "in_miss", "out")


class PriceError(Exception):
    """价格获取失败(抓取/解析/校验); 调用方负责兜底, 不要让它打断流水线。"""


class PriceParseError(PriceError):
    """价格页解析或校验失败。"""


# ------------------------------------------------------------------ 时间口径


def is_peak(at: datetime | None = None) -> bool:
    """该时刻是否处于官网定义的**高峰时段**(决定用哪一档价)。

    - 北京时间周一~周五 9:00-12:00 / 14:00-18:00 → 高峰; 其余(含周末)空闲。
    - 忽略中国法定节假日(见模块 docstring 的口径说明) → 节假日算高峰, 略微高估。
    - `at` 为 naive 时**按北京时间解释**(本项目跑在国内机器上); 带 tzinfo 则换算到北京时间。
    """
    if at is None:
        dt = datetime.now(CN_TZ)
    elif at.tzinfo is None:
        dt = at.replace(tzinfo=CN_TZ)
    else:
        dt = at.astimezone(CN_TZ)
    if dt.weekday() >= 5:        # 5=周六 6=周日
        return False
    minutes = dt.hour * 60 + dt.minute
    return any(s * 60 <= minutes < e * 60 for s, e in PEAK_WINDOWS)


def _fmt_ts(epoch: float | None = None) -> str:
    dt = datetime.fromtimestamp(epoch, CN_TZ) if epoch else datetime.now(CN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ 模型名归一


def normalize_model_name(model: str) -> str:
    """归一化模型名: 去空白/小写; 去掉 litellm 风格的 `provider/` 前缀。"""
    m = str(model or "").strip().lower()
    if "/" in m:
        m = m.rsplit("/", 1)[-1].strip()
    return m


def canonical_model(model: str) -> str | None:
    """别名 → 官网价目表里的规范模型名; 未收录返回 None(视为未定价, 不猜)。"""
    return _MODEL_ALIASES.get(normalize_model_name(model))


# ------------------------------------------------------------------ 价格表


@dataclass
class PriceTable:
    """一张价表 + 它的来源(来源必须外显, 否则不知道看到的是几号的价)。"""

    prices: dict[str, dict[str, dict[str, float]]]
    source: str
    currency: str = "CNY"
    fetched_at: float = 0.0
    stale: bool = False
    url: str = ""

    def canonical(self, model: str) -> str | None:
        return canonical_model(model)

    def unit_prices(self, model: str, *, peak: bool) -> tuple[float, float, float] | None:
        """(命中输入价, 未命中输入价, 输出价), 单位 元/百万 token; 未定价返回 None。"""
        canon = canonical_model(model)
        if canon is None:
            return None
        entry = self.prices.get(canon)
        if not entry:
            return None
        row = entry.get("peak" if peak else "off")
        if not row:
            return None
        try:
            return (float(row["in_hit"]), float(row["in_miss"]), float(row["out"]))
        except (KeyError, TypeError, ValueError):
            return None

    def cost(self, model: str, *, in_hit: int, in_miss: int, out: int,
             peak: bool) -> tuple[float, bool]:
        """返回 (费用元, 是否已定价)。未定价 → (0.0, False), 由调用方显式标记未定价。"""
        unit = self.unit_prices(model, peak=peak)
        if unit is None:
            return 0.0, False
        hit_p, miss_p, out_p = unit
        total = (in_hit * hit_p + in_miss * miss_p + out * out_p) / 1_000_000.0
        return total, True

    def as_dict(self) -> dict[str, Any]:
        return {
            "prices": self.prices,
            "source": self.source,
            "currency": self.currency,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
            "url": self.url,
        }


def builtin_table(source: str | None = None) -> PriceTable:
    src = source or f"内置快照({_BUILTIN_SNAPSHOT_DATE}, CNY)"
    return PriceTable(
        prices=json.loads(json.dumps(_BUILTIN_PRICES)),   # 深拷贝, 防调用方改坏常量
        source=src,
        currency="CNY",
        fetched_at=0.0,
    )


def table_from_manual(raw: Any) -> PriceTable | None:
    """把 config.LLM_PRICES 手填价表转成 PriceTable; 不合法返回 None(不抛)。

    两种写法:
      - 两档: `{deepseek-flash: {off: {...}, peak: {...}}}`
      - 一组: `{deepseek-flash: {in_hit: 0.02, in_miss: 1.0, out: 4.0}}`
        → **视为不分时段**(高峰/空闲同价), 而不是拿它当空闲价再猜一个高峰价。
          要区分时段请显式写 off/peak。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    prices: dict[str, dict[str, dict[str, float]]] = {}
    for model, entry in raw.items():
        canon = canonical_model(str(model)) or normalize_model_name(str(model))
        if not isinstance(entry, dict):
            return None
        row: dict[str, dict[str, float]] = {}
        flat = all(u in entry for u in _UNITS)
        for period in _PERIODS:
            if period in entry and isinstance(entry[period], dict):
                src = entry[period]
            elif flat:
                src = entry
            else:
                return None
            try:
                row[period] = {u: float(src[u]) for u in _UNITS}
            except (KeyError, TypeError, ValueError):
                return None
        prices[canon] = row
    return PriceTable(prices=prices, source="配置手填 LLM_PRICES", currency="CNY")


# ------------------------------------------------------------------ HTML 解析

_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.I)
_ROW_RE = re.compile(r"<tr[\s\S]*?</tr>", re.I)
_CELL_RE = re.compile(r"<t[hd][\s\S]*?</t[hd]>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_FOOTNOTE_RE = re.compile(r"\(\d+\)\s*$")
_MONEY_RE = re.compile(r"^\s*(?P<cur>[$¥￥])?\s*(?P<num>[0-9]+(?:\.[0-9]+)?)\s*(?P<cur2>元|[$¥￥])?\s*$")

_CURRENCY_MAP = {"元": "CNY", "¥": "CNY", "￥": "CNY", "$": "USD"}


def _strip_html(html: str) -> str:
    html = re.sub(r"<br\s*/?>", " ", html, flags=re.I)
    text = _TAG_RE.sub("", html)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


def _cells(row_html: str) -> list[str]:
    return [_strip_html(c) for c in _CELL_RE.findall(row_html)]


def _parse_money(cell: str) -> tuple[float, str] | None:
    """解析价格单元格 → (数值, 币种符号)。

    **必须带币种标记才算价格** —— 否则「并发限制 2500」「最大 384K」这类纯数字会被误当价格。
    页面上价格形如 `0.02元`(中文页) / `$0.003`(英文页)。
    """
    m = _MONEY_RE.match(cell or "")
    if not m:
        return None
    cur = m.group("cur") or m.group("cur2") or ""
    if not cur:
        return None
    try:
        return float(m.group("num")), _CURRENCY_MAP.get(cur, cur)
    except ValueError:
        return None


def _classify_period(text: str) -> str | None:
    """空闲/高峰。**必须先判 off** —— "off-peak" 里也含 "peak"。"""
    t = (text or "").lower()
    if "空闲" in t or "off-peak" in t or "off peak" in t:
        return "off"
    if "高峰" in t or "peak" in t:
        return "peak"
    return None


def _classify_metric(text: str) -> str | None:
    """度量名。**必须先判 in_miss**: "缓存未命中" 不含子串 "缓存命中"(中间夹了"未"),
    但反过来若先判 in_hit 再判 in_miss, 未来措辞微调就可能误判, 顺序固定更稳。
    """
    t = (text or "").lower()
    if "缓存未命中" in t or "cache miss" in t:
        return "in_miss"
    if "缓存命中" in t or "cache hit" in t:
        return "in_hit"
    if "输出" in t or "output" in t:
        return "out"
    return None


def parse_pricing_html(html: str, *, want_currency: str = "CNY",
                       url: str = "") -> PriceTable:
    """解析官网定价页 HTML → PriceTable。任何结构不符都抛 PriceParseError(交给兜底)。"""
    tables = _TABLE_RE.findall(html or "")
    if not tables:
        raise PriceParseError("定价页未找到 <table>")

    best: PriceParseError | None = None
    for table_html in tables:            # 页面将来若新增表格, 取第一张能解析出价的
        try:
            return _parse_one_table(table_html, want_currency=want_currency, url=url)
        except PriceParseError as exc:
            best = best or exc
    raise best or PriceParseError("定价页无可用价格表")


def _parse_one_table(table_html: str, *, want_currency: str, url: str) -> PriceTable:
    rows = [_cells(r) for r in _ROW_RE.findall(table_html)]
    rows = [r for r in rows if r]
    if len(rows) < 2:
        raise PriceParseError("定价表行数不足")

    # 列头: 首格含 模型/MODEL 的那一行 → 其余单元格是模型名(带脚注标记, 要剥掉)
    models: list[str] = []
    header_idx = -1
    for i, cells in enumerate(rows):
        head = cells[0].lower()
        if "模型" in cells[0] or head in ("model", "models"):
            models = [_FOOTNOTE_RE.sub("", c).strip() for c in cells[1:]]
            header_idx = i
            break
    if not models:
        raise PriceParseError("定价表未找到模型列头")
    models = [m for m in models if m]
    if not models:
        raise PriceParseError("定价表模型列为空")

    grid: dict[str, dict[str, dict[str, float]]] = {
        m: {p: {} for p in _PERIODS} for m in models
    }
    currency_seen: set[str] = set()
    metric: str | None = None

    for cells in rows[header_idx + 1:]:
        # ① rowspan 续行: 首格就是时段标签, 值从第 2 格起, 沿用当前度量
        if metric is not None:
            period = _classify_period(cells[0])
            if period is not None:
                _assign(grid, models, metric, period, cells[1:], currency_seen)
                continue

        # ② 新度量行: 必须在同一行里同时找到度量名 + 时段标签才算
        #    (否则「输出长度 / 最大 384K」会被误判成 out 度量)
        mk = next((k for k in (_classify_metric(c) for c in cells) if k), None)
        pidx = next((i for i, c in enumerate(cells) if _classify_period(c)), None)
        if mk is None or pidx is None:
            continue
        metric = mk
        _assign(grid, models, metric, _classify_period(cells[pidx]),
                cells[pidx + 1:], currency_seen)

    if not currency_seen:
        raise PriceParseError("定价表未解析出任何带币种的价格")
    if len(currency_seen) > 1:
        raise PriceParseError(f"定价表出现多种币种 {sorted(currency_seen)}, 无法判定")
    currency = next(iter(currency_seen))
    if want_currency and currency != want_currency:
        # 关键防呆: 英文页是美元、数字不同, 拿美元当人民币会把费用算错一个数量级
        raise PriceParseError(
            f"定价页币种为 {currency}, 与要求的 {want_currency} 不符(是否配了英文页 URL?)"
        )

    _validate(grid, models)
    # 来源在**解析处**就写死 —— 只要是从官网 HTML 解析出来的就是"实时",
    # 不依赖调用方补写(否则任何绕过 fetch 的调用都会给出空来源, 日志里就不知道价的出处)。
    fetched_at = time.time()
    return PriceTable(
        prices=grid,
        source=f"官网实时({_fmt_ts(fetched_at)}, {currency})",
        currency=currency, fetched_at=fetched_at, url=url,
    )


def _assign(grid, models, metric, period, values, currency_seen) -> None:
    if period is None:
        return
    if len(values) != len(models):
        raise PriceParseError(
            f"度量 {metric}/{period} 的值个数 {len(values)} 与模型列数 {len(models)} 不符"
        )
    for model, cell in zip(models, values):
        parsed = _parse_money(cell)
        if parsed is None:
            raise PriceParseError(f"{model} {metric}/{period} 的价格无法解析: {cell!r}")
        value, currency = parsed
        currency_seen.add(currency)
        grid[model][period][metric] = value


def _validate(grid: dict, models: list[str]) -> None:
    """结构 + 合理性校验。页面改版导致错位时, 这一步是最后一道闸。"""
    for model in models:
        for period in _PERIODS:
            row = grid[model][period]
            for unit in _UNITS:
                if unit not in row:
                    raise PriceParseError(f"{model}.{period}.{unit} 缺失")
                v = row[unit]
                if not (0 < v < 10000):
                    raise PriceParseError(f"{model}.{period}.{unit} 数值越界: {v}")
        off, peak = grid[model]["off"], grid[model]["peak"]
        if not off["in_hit"] < off["in_miss"]:
            raise PriceParseError(f"{model} 空闲价 命中输入 >= 未命中输入, 疑似列错位")
        if not off["in_hit"] < off["out"]:
            raise PriceParseError(f"{model} 空闲价 命中输入 >= 输出, 疑似列错位")
        for unit in _UNITS:
            if peak[unit] < off[unit]:
                raise PriceParseError(f"{model}.{unit} 高峰价低于空闲价, 疑似上下行错位")


# ------------------------------------------------------------------ 抓取 + 缓存


def fetch_official_prices(url: str = DEFAULT_PRICES_URL, *,
                          timeout: float = DEFAULT_TIMEOUT,
                          want_currency: str = "CNY") -> PriceTable:
    """抓取并解析官网定价页。

    `trust_env=False` 沿用 core/llm.py 的既有经验: 国内直连, 不读系统代理
    (系统代理设了 localhost 时 httpx 会卡死)。
    """
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True) as client:
            resp = client.get(url, headers={"user-agent": _UA,
                                            "accept-language": "zh-CN,zh;q=0.9"})
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:                     # noqa: BLE001 — 任何失败都交给兜底
        raise PriceError(f"定价页抓取失败: {type(exc).__name__}: {exc}") from exc
    return parse_pricing_html(html, want_currency=want_currency, url=url)


def cache_path(data_root: str) -> str:
    """价格缓存路径(全局共享, 与类目无关 → 放 <数据根>/.cache/, 不污染类目目录)。"""
    return os.path.join(str(data_root or "data"), ".cache", "llm-prices.json")


def load_cache(path: str) -> PriceTable | None:
    """读缓存; 文件缺失/损坏/结构不对一律返回 None(不抛)。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    prices = obj.get("prices")
    if not isinstance(prices, dict) or not prices:
        return None
    try:
        return PriceTable(
            prices=prices,
            source=str(obj.get("source") or "磁盘缓存"),
            currency=str(obj.get("currency") or "CNY"),
            fetched_at=float(obj.get("fetched_at") or 0.0),
            url=str(obj.get("url") or ""),
        )
    except (TypeError, ValueError):
        return None


def save_cache(path: str, table: PriceTable, log=None) -> None:
    """原子写缓存; 失败只告警不管(缓存写不进去不该影响流水线)。

    告警是必须的: 静默失败会让人以为"有缓存", 实际每次都在重抓或走兜底(实测踩过)。
    """
    try:
        from .io_utils import atomic_write_text   # 延迟导入, 见模块顶部说明

        atomic_write_text(path, json.dumps(table.as_dict(), ensure_ascii=False, indent=2))
    except Exception as exc:                      # noqa: BLE001
        _warn(log, f"LLM 价格缓存写入失败({path}): {type(exc).__name__}: {exc}")


def cache_age_hours(table: PriceTable, now: float | None = None) -> float:
    if not table.fetched_at:
        return float("inf")
    return max(0.0, ((now or time.time()) - table.fetched_at) / 3600.0)


def _fresh(table: PriceTable, ttl_hours: float, now: float | None = None) -> bool:
    return ttl_hours > 0 and cache_age_hours(table, now) < ttl_hours


# ------------------------------------------------------------------ 对外入口

_MEMO: dict[tuple, PriceTable] = {}
_MEMO_LOCK = threading.Lock()


def _memo_key(config) -> tuple:
    return (
        str(config.get("LLM_PRICES_MODE", "live") or "live").strip().lower(),
        str(config.get("LLM_PRICES_URL", DEFAULT_PRICES_URL) or DEFAULT_PRICES_URL).strip(),
        float(config.get("LLM_PRICES_TTL_HOURS", 24) or 0),
        str(config.get("LLM_PRICES_CURRENCY", "CNY") or "CNY").strip().upper(),
        float(config.get("LLM_PRICES_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
        cache_path(config.resolve_path(str(config.data_root or "data"))),
        json.dumps(config.get("LLM_PRICES") or {}, sort_keys=True, ensure_ascii=False, default=str),
    )


def reset_memo() -> None:
    """清空进程内记忆(测试用)。"""
    with _MEMO_LOCK:
        _MEMO.clear()


def resolve_prices(config, *, log=None, now: float | None = None) -> PriceTable:
    """按配置解析价格表。**永不抛异常** —— 任何失败都退到内置快照。"""
    key = _memo_key(config)
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None:
        return hit

    table = _resolve_uncached(config, log=log, now=now)
    with _MEMO_LOCK:
        _MEMO[key] = table
    return table


def _warn(log, msg: str) -> None:
    if log is not None:
        try:
            log.warning(msg)
        except Exception:                        # noqa: BLE001
            pass


def _resolve_uncached(config, *, log=None, now: float | None = None) -> PriceTable:
    mode = str(config.get("LLM_PRICES_MODE", "live") or "live").strip().lower()

    manual = table_from_manual(config.get("LLM_PRICES"))
    if manual is not None:
        return manual
    if config.get("LLM_PRICES"):
        _warn(log, "LLM_PRICES 手填价表不合法(需含 in_hit/in_miss/out), 已忽略")

    if mode == "manual":
        _warn(log, "LLM_PRICES_MODE=manual 但 LLM_PRICES 未配置, 回退内置快照")
        return builtin_table("内置快照(manual 模式缺 LLM_PRICES)")
    if mode == "builtin":
        return builtin_table()

    path = cache_path(config.resolve_path(str(config.data_root or "data")))
    cached = load_cache(path)

    if mode == "cache":
        if cached is not None:
            cached.source = f"磁盘缓存({_fmt_ts(cached.fetched_at)}, {cached.currency})"
            return cached
        _warn(log, "LLM_PRICES_MODE=cache 但无可用缓存, 回退内置快照")
        return builtin_table("内置快照(cache 模式无缓存)")

    # live
    ttl = float(config.get("LLM_PRICES_TTL_HOURS", 24) or 24)
    if cached is not None and _fresh(cached, ttl, now):
        cached.source = f"磁盘缓存({_fmt_ts(cached.fetched_at)}, {cached.currency})"
        return cached

    url = str(config.get("LLM_PRICES_URL", DEFAULT_PRICES_URL) or DEFAULT_PRICES_URL).strip()
    currency = str(config.get("LLM_PRICES_CURRENCY", "CNY") or "CNY").strip().upper()
    timeout = float(config.get("LLM_PRICES_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    try:
        table = fetch_official_prices(url, timeout=timeout, want_currency=currency)
    except Exception as exc:                     # noqa: BLE001 — 兜底, 绝不打断流水线
        if cached is not None:
            cached.source = (f"磁盘缓存({_fmt_ts(cached.fetched_at)}, {cached.currency}, "
                             f"已过期 {cache_age_hours(cached, now):.0f} 小时)")
            cached.stale = True
            _warn(log, f"LLM 价格获取失败({exc}), 回退过期缓存: {cached.source}")
            return cached
        _warn(log, f"LLM 价格获取失败({exc}), 且无磁盘缓存 → 回退内置快照, 费用为估算值")
        return builtin_table("内置快照(抓取失败且无缓存)")

    save_cache(path, table, log)
    return table
