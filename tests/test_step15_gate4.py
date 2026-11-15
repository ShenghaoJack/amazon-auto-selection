# 单元测试: FR-15 类目门禁④「新品BuyBox卖家数」口径 + 阈值可配
#
# 背景(2026-09-11): 旧实现误用 "卖家数" 列(该 listing 的卖家总数)并对新品行取**最大值**,
# 与字段名/业务口径不符 → 改为 "新老品=新品 的行对 BuyBox卖家 去重后的个数";
# 同时把阈值从写死的模块常量改成 config 键 GATE_NEW_SELLERS(默认 10)。
import os

import pandas as pd

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step15_category_report import (
    GATE_NEW_SELLERS,
    Step15CategoryReport,
    _gate_new_sellers,
)


class _Cfg:
    """最小 config 替身(只实现 get)。"""

    def __init__(self, data: dict):
        self._d = data

    def get(self, key, default=None):
        return self._d.get(key, default)


class _CtxStub:
    """最小 ctx 替身(只实现 .config, 供 _gate_new_sellers 使用)。"""

    def __init__(self, data: dict):
        self.config = _Cfg(data)


def _ctx(tmp_dir: str, extra: dict | None = None) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": tmp_dir, "DRY_RUN": True, "MARKET": "amazon.com"}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _split_df(rows: list[dict]) -> pd.DataFrame:
    """按 FR-14 输出契约造最小数据(列名用真实名字, 便于 _find_col 命中)。"""
    return pd.DataFrame(rows)


def _row(asin, price="20", sales="10", newold="新品", bb="SellerA", sellers="3"):
    return {
        "变体ASIN": asin,
        "价格($)": price,
        "变体月销量": sales,
        "变体月销额": "200",
        "新老品": newold,
        "BuyBox卖家": bb,
        "卖家数": sellers,
    }


def _gate4(ctx: StepContext, df: pd.DataFrame):
    st = Step15CategoryReport()
    st.bind_cat(ctx)
    d, meta = st._clean_variants(ctx, df)
    gates = {g[0]: g for g in st._compute_gates(ctx, d, meta)}
    return [g for k, g in gates.items() if k.startswith("门禁④")][0]


def test_counts_unique_buybox_sellers_of_new_rows(ws_tmp):
    """口径 = 新品行的唯一 BuyBox卖家数(去重); 老品/未知行不计入。"""
    ctx = _ctx(ws_tmp)
    df = _split_df(
        [
            _row("B01", newold="新品", bb="SellerA", sellers="1"),
            _row("B02", newold="新品", bb="SellerB", sellers="1"),
            _row("B03", newold="新品", bb="SellerB", sellers="12"),   # 同名 → 不重复计数
            _row("B04", newold="老品", bb="SellerC", sellers="9"),    # 老品 → 排除
            _row("B05", newold="未知", bb="SellerD", sellers="9"),    # 未知 → 排除
        ]
    )
    label, value, verdict = _gate4(ctx, df)
    assert value == "2"                       # SellerA + SellerB
    assert label == f"门禁④: 新品BuyBox卖家数>{GATE_NEW_SELLERS}"
    assert verdict == "不达标"                 # 2 不大于 10


def test_threshold_from_config(ws_tmp):
    """阈值来自 config GATE_NEW_SELLERS: 同一个数据在不同阈值下判定可不同。"""
    ctx_low = _ctx(ws_tmp, {"GATE_NEW_SELLERS": 1})
    ctx_high = _ctx(ws_tmp, {"GATE_NEW_SELLERS": 5})
    df = _split_df([_row("B01", bb="A"), _row("B02", bb="B"), _row("B03", bb="C")])
    assert _gate4(ctx_low, df)[2] == "达标"    # 3 > 1
    assert _gate4(ctx_low, df)[0].endswith(">1")
    assert _gate4(ctx_high, df)[2] == "不达标"  # 3 不大于 5
    assert _gate4(ctx_high, df)[0].endswith(">5")


def test_gate_new_sellers_helper_fallbacks():
    assert _gate_new_sellers(_CtxStub({})) == GATE_NEW_SELLERS
    assert _gate_new_sellers(_CtxStub({"GATE_NEW_SELLERS": 25})) == 25
    assert _gate_new_sellers(_CtxStub({"GATE_NEW_SELLERS": "30"})) == 30
    assert _gate_new_sellers(_CtxStub({"GATE_NEW_SELLERS": 0})) == GATE_NEW_SELLERS
    assert _gate_new_sellers(_CtxStub({"GATE_NEW_SELLERS": -3})) == GATE_NEW_SELLERS
    assert _gate_new_sellers(_CtxStub({"GATE_NEW_SELLERS": "abc"})) == GATE_NEW_SELLERS


def test_insufficient_when_no_new_rows(ws_tmp):
    ctx = _ctx(ws_tmp)
    df = _split_df([_row("B01", newold="老品", bb="A"), _row("B02", newold="老品", bb="B")])
    _label, value, verdict = _gate4(ctx, df)
    assert value == "无数据" and verdict == "数据不足"


def test_insufficient_when_seller_names_missing(ws_tmp):
    """BuyBox卖家 列存在但全是空值(_clean_label → '未知') → 数据不足。"""
    ctx = _ctx(ws_tmp)
    df = _split_df([_row("B01", bb=""), _row("B02", bb="nan")])
    _label, value, verdict = _gate4(ctx, df)
    assert value == "无数据" and verdict == "数据不足"


def test_insufficient_when_buybox_column_absent(ws_tmp):
    ctx = _ctx(ws_tmp)
    df = _split_df([
        {"变体ASIN": "B01", "价格($)": "20", "变体月销量": "10", "变体月销额": "200", "新老品": "新品"}
    ])
    _label, value, verdict = _gate4(ctx, df)
    assert value == "无数据" and verdict == "数据不足"


def test_old_seller_count_column_not_used(ws_tmp):
    """回归: 旧实现用的 '卖家数' 列不得再影响门禁④(即使它很大)。"""
    ctx = _ctx(ws_tmp)
    df = _split_df([_row("B01", bb="A", sellers="99"), _row("B02", bb="B", sellers="99")])
    _label, value, verdict = _gate4(ctx, df)
    assert value == "2" and verdict == "不达标"
