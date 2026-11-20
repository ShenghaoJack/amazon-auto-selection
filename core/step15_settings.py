"""Workspace-wide Step 15 standards, separate from category config.yaml."""
from __future__ import annotations
import hashlib
import json
import math
import threading
from pathlib import Path
from tools.attr_stability.spec_io import write_json_atomic
from tools.step15_indicator_assessment import SPECS
from tools import step15_market_assessment as market

LIMITS = {"类目GMV估算": (100000.0, 0.0, 1e15, "样本GMV ≥ 此值可达标；低于不能推断全市场"),
          "老品占比": (70.0, 0.0, 100.0, "样本中超过1095天的销量占比 > 此百分比降级"),
          "平均评分数": (4.2, 0.0, 5.0, "样本变体平均星级 < 此值不达标")}
FORMULAS = [
 "逐词读取步骤8的站内月搜索量；保留前20词，其余见关键词表。不选主词，不对冲突阈值判定。",
 "逐词读取步骤8 CPC；成交客单价缺失，不对冲突标准判定。",
 "参考值 = 有效变体价格之和 ÷ 有效变体数；非成交客单价，订单金额和订单数缺失时不能判定。",
 "尚未计算；总销量与平均销量口径冲突，待确认。",
 "样本GMV = 步骤14全部变体月销额之和；仅美元、ASIN唯一且销额完整时计算；≥门槛可达标，低于不能证明全市场不足。",
 "参考值 = 样本销量前10个变体销量之和 ÷ 样本变体销量之和 × 100%；仅ASIN唯一及销量完整时计算，冲突待确认。",
 "尚未计算连续3季同比；原表与备注的趋势口径冲突。",
 "尚未计算；需按月份的完整销量，再计算最大季度销量 ÷ 全年销量。",
 "尚未计算；需确定淡旺季口径，再计算淡季销量 ÷ 旺季销量。",
 "尚未计算；需两年月度数据，并确认峰谷比或波动系数定义。",
 "参考值 = 销量最高的前⌈变体数×20%⌉个变体销量 ÷ 样本总销量 × 100%；备注口径冲突，不判定。",
 "尚未计算；样本BuyBox卖家数不能直接等同全市场卖家数，且备注口径冲突。",
 "样本CR5 = 样本中销额前5个BuyBox卖家销额 ÷ 样本总销额 × 100%；卖家和销额完整才展示，尚不能代表全市场。",
 "尚未计算；缺12个月卖家生存轨迹，且与新品卖家数/销额的备注口径冲突。",
 "尚未计算；‘头部’范围与评论数定义尚未确认。",
 "样本老品占比 = 上架天数>1095的变体销量之和 ÷ 全部变体销量之和 × 100%；ASIN唯一、天数与销量完整时计算。",
 "尚未计算；卖家数与销额作为分母尚未确定。",
 "尚未计算；VC归类和按卖家计数的判定方向尚未确认。",
 "尚未计算；需类目周期内退款订单数及相应订单总数。",
 "样本平均评分 = 全部变体评分之和 ÷ 变体数量；要求ASIN唯一、评分齐全且在0–5之间，不按评论量加权。",
]
FILENAME = "15_选品标准设置.json"
STANDARDS_DIR = Path(__file__).resolve().parents[1] / "workbench-settings"
_LOCK = threading.Lock()


def defaults():
    result = {name: {"enabled": True, "standard": threshold, **({"limit": LIMITS[name][0]} if name in LIMITS else {})}
              for _, name, threshold, _, _ in SPECS}
    result.update(market.defaults())
    return result


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def global_path(root):
    return STANDARDS_DIR / FILENAME


def _read(path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict) or obj.get("version") != 1: raise ValueError("版本无效")
        return validate(obj.get("rules"))
    except (OSError, TypeError, ValueError) as e:
        raise ValueError("选品标准设置损坏，请在工作台检查：" + str(e)) from e


def load_global(root):
    p = global_path(root)
    return _read(p) if p.exists() else defaults()


def load(root):
    return load_global(root)


def validate(rules):
    baseline = defaults()
    if not isinstance(rules, dict): raise ValueError("指标清单格式无效")
    # Existing v1 files contain only legacy indicators. Migrate in memory without
    # overwriting the user's file; the next explicit save persists both sets.
    old_keys = {name for _, name, _, _, _ in SPECS}
    if set(rules) == old_keys:
        migrated = market.defaults()
        # Only carry limits whose units and meaning remain the same. In
        # particular, old-product share changed from units sold to revenue.
        for previous, current in (("类目GMV估算", market.key("市场需求", "类目GMV估算")),
                                  ("平均评分数", market.key("竞争格局", "评分"))):
            if isinstance(rules[previous], dict):
                for field in ("enabled", "limit"):
                    if field in rules[previous]:
                        migrated[current][field] = rules[previous][field]
        rules = {**rules, **migrated}
    if set(rules) != set(baseline): raise ValueError("指标清单不完整或包含未知指标")
    result = {}
    market_specs = {market.key(s, n): (s, n, formula, limit, template, impact)
                    for s, n, formula, limit, template, impact in market.SPECS}
    for name, default in baseline.items():
        item = rules[name]
        if not isinstance(item, dict) or not isinstance(item.get("enabled"), bool): raise ValueError(name + "启用状态无效")
        text = item.get("standard")
        if not isinstance(text, str) or not text.strip() or len(text) > 300: raise ValueError(name + "标准须为1至300字")
        record = {"enabled": item["enabled"], "standard": text.strip()}
        if name in market_specs:
            section, label, formula, limit, template, impact = market_specs[name]
            if limit is not None:
                val = item.get("limit")
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val): raise ValueError(name + "阈值须为有效数字")
                lo, hi = (-100, 10000) if "趋势方向" in label else (0, 1e15)
                if label in ("头部链接销售额占比", "头部品牌集中度", "卖家集中度", "大卖占比", "VC卖家占比", "本土卖家占比", "老品占比"): hi = 100
                if label == "评分": hi = 5
                if not lo <= val <= hi: raise ValueError(name + "阈值超出允许范围")
                record["limit"] = float(val)
            if label == "新卖家占比":
                val = item.get("limit2")
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 0 <= val <= 100:
                    raise ValueError(name + "新品销额占比阈值须为0–100")
                record["limit2"] = float(val)
            record["standard"] = market.standard(template, record)
        elif name in LIMITS:
            val = item.get("limit")
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val): raise ValueError(name + "阈值须为有效数字")
            _, lo, hi, _ = LIMITS[name]
            if not lo <= val <= hi: raise ValueError(name + "阈值超出允许范围")
            record["limit"] = float(val)
            if name == "类目GMV估算": record["standard"] = f"类目GMV < ${val:g}/月否决"
            elif name == "老品占比": record["standard"] = f"3年以上老品销量占比 > {val:g}% 降级"
            else: record["standard"] = f"平均评分数 < {val:g}分"
        result[name] = record
    return result


def describe(root, scope="global"):
    if scope != "global": raise ValueError("选品标准仅支持全类目通用")
    data = load_global(root)
    return {"revision": digest(data), "rules": [{"name": market.key(section, name), "priority": section, "note": "样本口径；数据不足时不自动判定",
            "original_standard": market.standard(template, market.defaults()[market.key(section, name)]), "formula": formula,
            "automatic": limit is not None, "second_limit": name == "新卖家占比",
            "automatic_note": "阈值直接参与计算；数据缺失、币种不符或口径未确认时标记数据不足。" if limit is not None else "仅展示，不参与达标判定。",
            **data[market.key(section, name)]} for section, name, formula, limit, template, impact in market.SPECS],
            "scope": scope}


def save(root, rules, expected, scope="global"):
    if scope != "global": raise ValueError("选品标准仅支持全类目通用")
    with _LOCK:
        current = load_global(root)
        if expected != digest(current): raise ValueError("设置已被修改，请刷新后重试")
        if isinstance(rules, dict) and set(rules) == set(market.defaults()):
            rules = {**{k: v for k, v in current.items() if k not in market.defaults()}, **rules}
        cleaned = validate(rules)
        write_json_atomic(global_path(root), {"version": 1, "rules": cleaned})
        return digest(cleaned)
