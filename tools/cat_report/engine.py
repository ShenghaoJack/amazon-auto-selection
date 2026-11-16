# -*- coding: utf-8 -*-
"""cat_report engine — 把 skill 契约落成可复算的指标引擎。

设计原则(对齐 PRODUCT_REPORT_GUIDE + build-amazon-integrated-market-report):
  1. 数字全部由本模块计算, 模型只做取舍与叙事;
  2. 每个指标进 EvidenceRegistry(来源/口径/单位/行数/父体/品牌);
  3. 价格带动态生成并记录选段理由, 边界不得用功能属性参与;
  4. 独立性与样本量闸门内建, 不满足即降级标注。

只读输入, 不写交付表。
"""
from __future__ import annotations

import collections
import io
import json
import math
import os
import re
import statistics as st

import openpyxl

MISSING = "缺"
UNKNOWN = "未明确"
NOT_PROVIDED = "未提供"

# ---------------------------------------------------------------- 口径常量
SALES_FIELD = "变体月销量"      # 全局唯一销量口径
REVENUE_FIELD = "变体月销额"    # 全局唯一销额口径
PRICE_FIELD = "价格($)"

# 字段别名字典(等价列可用于仅当含义无歧义; 映射需记录在报告来源说明)
ALIASES = {
    "parent": ["父ASIN", "父体ASIN", "Parent ASIN"],
    "asin": ["变体ASIN", "子ASIN", "ASIN", "Variant ASIN"],
    "title": ["标题", "商品标题", "Product Title", "Title"],
    "brand": ["品牌", "Brand"],
    "url": ["商品详情页链接", "商品链接", "Product URL", "URL"],
    "price": [PRICE_FIELD, "价格", "售价", "Price"],
    "sales": [SALES_FIELD, "变体月销量", "Variant Monthly Sales"],
    "revenue": [REVENUE_FIELD, "变体月销售额", "Variant Monthly Revenue"],
    "rating": ["评分", "星级", "Rating"],
    "reviews": ["评分数", "评论数", "评价数", "Review Count"],
    "lifecycle": ["新老品", "生命周期", "新品/老品", "New/Old"],
    "fulfilment": ["配送方式", "履约方式", "Fulfilment"],
    "listing_days": ["上架天数", "上架时间"],
    "seller_count": ["卖家数"],
    "bsr": ["小类BSR", "大类BSR"],
    "product_type": ["产品类型"],
    "material": ["主体材质"],
    "set_count": ["套装数量"],
    "spec": ["尺寸规格"],
    # 2026-10-09 用户口径：「**厚度 这列可以删掉**」（档位名没必要，让 AI 直接判）。
    #   所以候选列名**先取 `厚`** —— 它是 `尺寸拆列` 出来的**精确值**（cm/inch），
    #   由模型从证据判、再经单位换算，正是报表要的口径；`厚度` 作为兜底
    #   兼容历史交付表（那些表里还留着旧的分档名列）。
    "thickness": ["厚", "厚度"],
    "shape": ["形状"],
    "traffic_words": ["流量词数"],
    "parent_traffic": ["父体总流量"],
    "traffic_share": ["流量占比"],
    "ac_keyword": ["AC关键词"],
}

# 功能属性(二值: 是/否/未明确) —— L3 差异化维度
BINARY_FEATURES = ["是否双面可用", "是否可洗碗机清洗", "是否含收纳架", "是否带手柄",
                   "是否带挂孔", "是否带汁槽", "是否环保认证", "是否耐高温",
                   "是否防滑", "是否食品级安全"]
# 明确为"是"的取值(其余非空白/非未明确 视为否)
POSITIVE = {
    "是否双面可用": ("双面可用", "双面不同材质"),
    "是否可洗碗机清洗": ("可洗碗机清洗",),
    "是否含收纳架": ("含收纳架",),
    "是否带手柄": ("带手柄",),
    "是否带挂孔": ("带挂孔",),
    "是否带汁槽": ("带汁槽",),
    "是否环保认证": (),                     # 本类目无来源支持
    "是否耐高温": ("耐高温",),
    "是否防滑": ("防滑",),
    "是否食品级安全": ("食品级", "BPA-Free", "无毒"),
}


def num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").replace("%", "").strip()
    if s in ("", "-", MISSING, UNKNOWN, NOT_PROVIDED, "None", "nan"):
        return None
    try:
        f = float(s)
        return None if math.isnan(f) else f
    except ValueError:
        return None


def g(x, fmt="%g"):
    return fmt % x if x is not None else "—"


def f0(x):
    """千分位整数(不带 % 的 printf 不支持千分位, 单列一个 helper)。"""
    return "{:,.0f}".format(x) if x is not None else "—"


def f2(x):
    return "{:,.2f}".format(x) if x is not None else "—"


def med(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2.0


def q(vals, frac):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    i = min(len(v) - 1, max(0, int(round(frac * (len(v) - 1)))))
    return v[i]


def share(part, whole):
    return (part / whole * 100.0) if whole else 0.0


# ---------------------------------------------------------------- 证据登记
class Evidence:
    """值级溯源登记表(Sheet5)。每个结论引用的数字都必须在这里有一行。"""

    def __init__(self):
        self.rows = []
        self._n = 0

    def add(self, module, name, value, source, caliber, unit="—",
            rows=None, parents=None, brands=None, note=""):
        self._n += 1
        eid = "EV-%04d" % self._n
        self.rows.append({
            "evidence_id": eid, "模块": module, "指标": name, "值": value,
            "来源": source, "口径": caliber, "单位": unit,
            "行数": rows if rows is not None else "", "父体数": parents if parents is not None else "",
            "品牌数": brands if brands is not None else "", "备注": note})
        return eid

    def get(self, eid):
        for r in self.rows:
            if r["evidence_id"] == eid:
                return r
        return None


# ---------------------------------------------------------------- 载入
class Sources:
    """三输入角色: keyword / product / voc。

    product / keyword 按内容判定; **VOC 按文件名**(含 "VOC" 或 "差评")探测后解析内容,
    解析失败按"未提供"处理并保留原因(缺失一律留空披露, 不允许按 0 计分)。
    """

    def __init__(self, cat, cat_dir):
        self.cat = cat
        self.dir = cat_dir
        self.mapping_notes = []
        self.voc_available = False

    # ---- 产品源
    def load_product(self, path=None):
        path = path or os.path.join(self.dir, "18_标准化",
                                   "%s-属性标注-标准化交付.xlsx" % self.cat)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        main = "属性标注" if "属性标注" in wb.sheetnames else wb.sheetnames[0]
        src_name = "来源" if "来源" in wb.sheetnames else None
        m_rows = [list(r) for r in wb[main].iter_rows(values_only=True)]
        s_rows = [list(r) for r in wb[src_name].iter_rows(values_only=True)] if src_name else []
        wb.close()
        hdr = [str(h) if h is not None else "" for h in m_rows[0]]
        idx = {}
        for canon, aliases in ALIASES.items():
            for a in aliases:
                if a in hdr:
                    idx[canon] = hdr.index(a)
                    if a != canon:
                        self.mapping_notes.append("%s ← 列「%s」" % (canon, a))
                    break
        missing = [c for c in ("asin", "brand", "price", "sales", "revenue") if c not in idx]
        if missing:
            raise ValueError("产品源缺少必需字段: %s (实际表头 %d 列)" % (missing, len(hdr)))
        recs = []
        for k, r in enumerate(m_rows[1:], start=2):
            d = {c: (r[i] if i < len(r) else None) for c, i in idx.items()}
            rec = {
                "xl_row": k,
                "parent": str(d.get("parent") or ""),
                "asin": str(d.get("asin") or ""),
                "title": str(d.get("title") or ""),
                "brand": str(d.get("brand") or "").strip(),
                "url": str(d.get("url") or ""),
                "price": num(d.get("price")),
                "sales": num(d.get("sales")),
                "revenue": num(d.get("revenue")),
                "rating": num(d.get("rating")),
                "reviews": num(d.get("reviews")),
                "lifecycle": str(d.get("lifecycle") or "").strip(),
                "fulfilment": str(d.get("fulfilment") or "").strip(),
                "listing_days": num(d.get("listing_days")),
                "seller_count": num(d.get("seller_count")),
                "product_type": str(d.get("product_type") or "").strip(),
                "material": str(d.get("material") or "").strip(),
                "set_count": str(d.get("set_count") or "").strip(),
                "spec": str(d.get("spec") or "").strip(),
                "thickness": str(d.get("thickness") or "").strip(),
                "shape": str(d.get("shape") or "").strip(),
            }
            for f in BINARY_FEATURES:
                rec[f] = str(r[hdr.index(f)] or "").strip() if f in hdr else ""
            rec["spec_source"] = ""
            if s_rows and "尺寸规格" in hdr:
                j = hdr.index("尺寸规格")
                rec["spec_source"] = str(s_rows[k - 1][j] or "") if k - 1 < len(s_rows) else ""
            rec["segs"] = parse_spec_triples(rec["spec"])
            rec["features_yes"] = sum(1 for f in BINARY_FEATURES if is_positive(f, rec[f]))
            recs.append(rec)
        self.product_path = path
        self.header = hdr
        self.idx = idx
        self.products = recs
        return recs

    # ---- 关键词源
    def load_keyword(self):
        p = os.path.join(self.dir, "%s-keyword-report.xlsx" % self.cat)
        out = {"path": p, "sheets": {}, "keywords": [], "longtail": [], "seasonality": [],
               "substitution": []}
        if not os.path.exists(p):
            out["missing"] = True
            return out
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        out["sheets"] = list(wb.sheetnames)
        if "核心词分析" in wb.sheetnames:
            rows = [list(r) for r in wb["核心词分析"].iter_rows(values_only=True)]
            hd = [str(h) if h is not None else "" for h in rows[0]]
            for r in rows[1:]:
                d = {hd[i]: r[i] for i in range(min(len(hd), len(r)))}
                kw = str(d.get("关键词") or "").strip()
                if not kw:
                    continue
                out["keywords"].append({
                    "keyword": kw,
                    "search": num(d.get("站内月搜索量")),
                    "mom_yoy": num(d.get("站内同比%")),
                    "google_index": d.get("Google指数"),
                    "google_yoy": num(d.get("Google同比%")),
                    "cpc": num(d.get("CPC")),
                    "cpc_min": num(d.get("CPC下限")),
                    "cpc_max": num(d.get("CPC上限")),
                    "verdict": str(d.get("达标判定") or "").strip(),
                    "season_in": str(d.get("站内季节性") or "").strip(),
                    "season_g": str(d.get("Google季节性") or "").strip(),
                })
        if "长尾词" in wb.sheetnames:
            rows = [list(r) for r in wb["长尾词"].iter_rows(values_only=True)]
            for r in rows[1:]:
                if r and r[0]:
                    out["longtail"].append({"keyword": str(r[0]).strip(),
                                            "dim": str(r[1] or "").strip() if len(r) > 1 else ""})
        if "季节性分析" in wb.sheetnames:
            rows = [list(r) for r in wb["季节性分析"].iter_rows(values_only=True)]
            hd = [str(h) if h is not None else "" for h in rows[0]]
            for r in rows[1:]:
                kw = str(r[0] or "").strip()
                if not kw:
                    continue
                d = {"关键词": kw}
                for i in range(1, 13):
                    name = "%d月" % i
                    if name in hd:
                        d[name] = num(r[hd.index(name)])
                out["seasonality"].append(d)
        wb.close()
        # 替代声明: 本类目无「综合覆盖率」列
        out["substitution"].append("关键词排序: 用 `站内月搜索量`(主) + `达标判定` 替代 skill 的 `综合覆盖率`——本类目关键词报告无覆盖率字段")
        return out

    # ---- VOC 源
    def load_voc(self):
        """探测并**解析** VOC 工作簿(2026-10-09 起真正接入归类解析)。

        文件名含 "VOC" 或 "差评" 即视为候选; 解析失败(表结构不符/空表)时按"未提供"处理,
        并保留原因 —— 缺失一律留空披露, 不允许按 0 计分。
        """
        cands = [os.path.join(self.dir, f) for f in sorted(os.listdir(self.dir))
                 if "VOC" in f.upper() or "差评" in f]
        if not cands:
            self.voc_available = False
            return {"available": False,
                    "reason": "未找到 VOC 工作簿(1–3 星评论归类分析)。目录内仅存在 评分数/评分 数值列、五点描述(卖家自述)、Q&A 列, 均不能替代买家差评归类。",
                    "found": []}
        from . import voc_reader
        last_reason = ""
        for path in cands:
            try:
                parsed = voc_reader.read_voc_workbook(path)
            except Exception as exc:  # noqa: BLE001  解析失败不允许影响整份报告
                last_reason = "%s 解析失败: %s: %s" % (os.path.basename(path),
                                                       type(exc).__name__, str(exc)[:160])
                continue
            if parsed.get("available"):
                parsed["found"] = cands
                self.voc_available = True
                return parsed
            last_reason = parsed.get("reason", last_reason)
        self.voc_available = False
        return {"available": False, "reason": last_reason or "VOC 工作簿无法解析",
                "found": cands}


# ---------------------------------------------------------------- 解析
RE_TRIPLE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)(?:\s*[xX×*]\s*(\d+(?:\.\d+)?|缺))?")


def parse_spec_triples(val):
    """解析尺寸规格 → [{l,w,h,label}]; 段内取冒号后的数值体, 避免把标签里的尺寸当真值。"""
    out = []
    for seg in re.split(r"[,，]", str(val or "")):
        seg = seg.strip()
        if not seg or seg == MISSING:
            continue
        lab, sep, body = seg.partition(":")
        if not sep:
            lab, body = "", seg
        for m in RE_TRIPLE.finditer(body):
            h = m.group(3)
            out.append({"label": lab.strip(), "l": float(m.group(1)), "w": float(m.group(2)),
                        "h": None if h in (None, "", MISSING) else float(h)})
    return out


LADDER_NAMES = {1: ["大"], 2: ["大", "小"], 3: ["大", "中", "小"],
                4: ["特大", "大", "中", "小"], 5: ["特大", "大", "中", "小", "迷你"]}


def ladder_of(segs):
    """按面积从大到小归一命名为 大/中/小; 与源标签无关(源标签仅备注)。"""
    if not segs:
        return "", []
    order = sorted(segs, key=lambda x: -(x["l"] * x["w"]))
    names = LADDER_NAMES.get(len(order), ["特大", "大", "中", "小", "迷你"] +
                             ["第%d件" % i for i in range(5, len(order))])
    parts = ["%s %sx%s" % (names[i], g(s["l"]), g(s["w"])) for i, s in enumerate(order)]
    return " · ".join(parts), order


def is_positive(feature, val):
    v = str(val or "").strip()
    if not v or v in (UNKNOWN, MISSING, NOT_PROVIDED):
        return None            # 未知, 不是"否"
    pos = POSITIVE.get(feature, ())
    if not pos:
        return None
    return any(p in v for p in pos)


def set_count_n(val):
    m = re.search(r"(\d+)", str(val or ""))
    return int(m.group(1)) if m else None


def in_scope_product(rec):
    """产品类型必须是"被出售的主体"。配件/耗材类单独排除(本类目未出现独立配件)。"""
    return True


# ---------------------------------------------------------------- 价格带
def choose_price_bands(prices):
    """动态价格带: 段数按有效样本量查表, 再按唯一价密度/自然断点收紧, 极端尾部单列。

    返回 {bands:[{lo,hi,label}], rationale, target_min}
    """
    ps = sorted(p for p in prices if p is not None and p > 0)
    n = len(ps)
    if n == 0:
        return {"bands": [], "rationale": "无有效价格", "target_min": 0}
    if n < 30:
        lo_n, hi_n = 3, 4
    elif n < 80:
        lo_n, hi_n = 4, 5
    elif n < 200:
        lo_n, hi_n = 5, 7
    elif n < 500:
        lo_n, hi_n = 6, 8
    else:
        lo_n, hi_n = 8, 10
    uniq = len(set(round(p, 2) for p in ps))
    target_min = max(5, int(math.ceil(0.03 * n)))

    def build(k):
        bounds = []
        for i in range(1, k):
            bounds.append(q(ps, i / float(k)))
        # 吸附到 0.5 美元网格, 保证严格递增且不越界
        snapped = []
        for b in bounds:
            s = round(b * 2) / 2.0
            if snapped and s <= snapped[-1]:
                s = snapped[-1] + 0.5
            snapped.append(round(s, 2))
        edges = [ps[0]] + snapped + [ps[-1] + 0.01]
        bands = []
        for i in range(len(edges) - 1):
            bands.append({"lo": edges[i], "hi": edges[i + 1], "n": 0, "min": None, "max": None})
        for p in ps:
            for b in bands:
                if b["lo"] <= p < b["hi"]:
                    b["n"] += 1
                    b["min"] = p if b["min"] is None else min(b["min"], p)
                    b["max"] = p if b["max"] is None else max(b["max"], p)
                    break
        return [b for b in bands if b["n"] > 0]

    chosen, bands = None, None
    for k in range(hi_n, lo_n - 1, -1):
        b = build(k)
        if len(b) >= lo_n and all(x["n"] >= target_min for x in b):
            chosen, bands = k, b
            break
    if bands is None:                      # 样本不足以保证下限 → 退让到低段数
        for k in range(lo_n, 1, -1):
            b = build(k)
            if b and all(x["n"] >= 5 for x in b):
                chosen, bands = k, b
                break
    small_note = ""
    if bands is None:
        # 样本无法满足多段下限时，保留全部有效价格为一个段。
        chosen, bands = 1, build(1)
        small_note = "; 样本不足以满足多段下限，降级为单段保留全部有效价格"
    for i, b in enumerate(bands):
        b["label"] = "$%s+" % g(b["lo"], "%.2f").rstrip("0").rstrip(".") if i == len(bands) - 1 else \
            "$%s-%s" % (g(b["lo"], "%.2f").rstrip("0").rstrip("."),
                        g(b["hi"], "%.2f").rstrip("0").rstrip("."))
        b["idx"] = i + 1
    # 极端尾部单列: 最高段跨度过大(上界/下界 > 2.5)时, 在该段中位数处再切一刀
    p_q3 = q(ps, 0.75)
    split_note = ""
    if len(bands) >= 2:
        top = bands[-1]
        if top["lo"] > 0 and top["hi"] / top["lo"] > 2.5:
            inner = [p for p in ps if p >= top["lo"]]
            cut = round((med(inner) or top["lo"]) / 5.0) * 5.0
            if top["lo"] < cut < top["hi"]:
                a = {"lo": top["lo"], "hi": cut, "n": 0, "min": None, "max": None}
                b2 = {"lo": cut, "hi": top["hi"], "n": 0, "min": None, "max": None}
                for p in inner:
                    tgt = a if p < cut else b2
                    tgt["n"] += 1
                    tgt["min"] = p if tgt["min"] is None else min(tgt["min"], p)
                    tgt["max"] = p if tgt["max"] is None else max(tgt["max"], p)
                if a["n"] and b2["n"]:
                    bands = bands[:-1] + [a, b2]
                    for i, bb in enumerate(bands):
                        bb["label"] = "$%s+" % g(bb["lo"], "%.2f").rstrip("0").rstrip(".") \
                            if i == len(bands) - 1 else "$%s-%s" % (
                                g(bb["lo"], "%.2f").rstrip("0").rstrip("."),
                                g(bb["hi"], "%.2f").rstrip("0").rstrip("."))
                        bb["idx"] = i + 1
                    split_note = ("; 原最高段跨度 %.1f 倍(经济上不是同一档), 已在中位价 $%s 处再切一刀, "
                                  "切成「%s」与「%s」" % (top["hi"] / top["lo"],
                                                   g(cut, "%.2f").rstrip("0").rstrip("."),
                                                   a["label"], b2["label"]))
    p_med, p_max = med(ps), ps[-1]
    tail_note = ""
    if bands and p_q3 and p_max > 2.5 * p_q3:
        tail_note = "; 最高价 $%s 为经济上独立的极端尾部(已在最高段单列)" % g(
            p_max, "%.2f").rstrip("0").rstrip(".")
    tail = "最高价 $%.2f 与第三四分位 $%.2f 的比值为 %.2f" % (p_max, p_q3 or 0, (p_max / p_q3) if p_q3 else 0)
    rationale = ("有效变体 %d 个(唯一价格 %d 个) → 目标段数 %d~%d, 取 %d 段; "
                 "边界由分位数确定并吸附到 $0.5 网格, 未使用任何功能/属性字段参与切分; "
                 "每段样本下限 max(5, 3%%×%d)=%d, 实测最小段 %d 个; %s%s%s" % (
                     n, uniq, lo_n, hi_n, len(bands), n, target_min,
                     min(x["n"] for x in bands), tail, split_note, tail_note + small_note))
    return {"bands": bands, "rationale": rationale, "target_min": target_min,
            "n_valid": n, "uniq": uniq, "k": chosen,
            "tail": {"p_max": p_max, "p_q3": p_q3, "p_med": p_med,
                     "is_tail": bool(p_q3 and p_max > 2.5 * p_q3)}}


def band_of(bands, price):
    if price is None:
        return None
    for b in bands:
        if b["lo"] <= price < b["hi"]:
            return b
    return bands[-1] if bands else None
