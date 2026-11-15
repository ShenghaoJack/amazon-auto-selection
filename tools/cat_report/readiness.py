# -*- coding: utf-8 -*-
"""Sheet0 数据就绪与口径 —— 通用规范的"铁律①": 字段没核实, 不得进入决策。

三件事:
  1. 字段可信度表(可用 / 仅参考 / 禁用) + 缺失率;
  2. 物理合理性校验(尺寸三值顺序等, 复用 scripts/audit_spec_order.py);
  3. 遗留项分类计数(复用 scripts/residual_issues.py) + 独立性统计(变体/父体/品牌)。
"""
from __future__ import annotations

import collections
import os
import sys

from .engine import MISSING, UNKNOWN, g, share

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import audit_spec_order as ASO      # noqa: E402  尺寸三值物理校验(已复扫 0 违规)
import residual_issues as RI        # noqa: E402  遗留项分类(10 类, ASIN 级)

# 字段可信度分级(砧板口径来源: 厚度与主体材质-核实报告 + 尺寸规格重测)
TRUST = [
    ("变体ASIN", "可用", "唯一键", "抓取阶段"),
    ("父ASIN", "可用", "独立性统计的分组键", "抓取阶段"),
    ("变体月销量", "可用", "全局唯一销量口径", "卖家精灵"),
    ("变体月销额", "可用", "全局唯一销额口径", "卖家精灵"),
    ("价格($)", "可用", "平均价/中位价/价格带", "卖家精灵"),
    ("品牌", "可用", "品牌集中度", "卖家精灵"),
    ("新老品", "可用", "生命周期(不做推断)", "卖家精灵"),
    ("配送方式", "可用", "履约结构", "卖家精灵"),
    ("上架天数", "可用", "竞争门槛", "卖家精灵"),
    ("评分数", "可用", "竞争门槛", "卖家精灵"),
    ("评分", "可用(排除0)", "评分均值必须排除 评分=0", "卖家精灵"),
    ("主体材质", "可用", "经两面核实 + 词表标准化", "17/18 步"),
    ("产品类型", "可用", "经与材质交叉校验", "17/18 步"),
    ("套装数量", "可用", "形态维度", "17/18 步"),
    # 2026-10-09：列名由「厚度」（档位名）改为「厚」（精确值）。
    #   用户口径：档位名没必要、这一列可以删掉 —— 厚度信息走 `尺寸拆列` 的精确值。
    ("厚", "可用", "经文本+视觉+OCR 逐行核实; 已剥离叠加值", "17/18 步 + 核实批次"),
    ("尺寸规格", "可用(已修正)", "经真实尺寸图重测 + 三值顺序修正", "17/18 步 + 重测批次"),
    ("形状/功能属性列", "可用", "仅「明确为是/否」参与统计, 未明确不计为否", "17/18 步"),
    ("商品尺寸", "禁用", "卖家精灵包装/商品尺寸, 与真实板体尺寸常不一致; 本项目实测不可作为规格依据", "—"),
    ("包装尺寸/包装重量", "禁用", "同上, 只在物流环节参考", "—"),
    ("五点描述/标题", "仅参考", "卖家自述, 只用于佐证, 不直接入指标", "—"),
    ("Q&A", "仅参考", "非结构化, 本类目无有效样本", "—"),
    ("AC关键词/流量词数/父体总流量/流量占比", "仅参考", "站内流量口径, 本次未接入需求评分", "卖家精灵"),
]


def field_trust(recs, header, ev=None):
    n = len(recs)
    rows = []
    for name, level, use, src in TRUST:
        if name in header:
            vals = [r for r in recs if str(_get(r, name) or "").strip() not in ("", "None")]
            missing = share(n - len(vals), n)
        elif name.startswith("形状/功能") or name.startswith("包装") or name.startswith("五点"):
            missing = None
        else:
            missing = None
        rows.append({"字段": name, "可信度": level, "用途": use, "缺失率": missing, "判定来源": src})
    if ev is not None:
        disabled = [r["字段"] for r in rows if r["可信度"] == "禁用"]
        ev.add("数据就绪与口径", "禁用字段", "、".join(disabled), "项目核实结论",
               "禁用列不得作为任何结论的依据")
    return rows


def _get(rec, name):
    m = {"变体ASIN": "asin", "父ASIN": "parent", "变体月销量": "sales", "变体月销额": "revenue",
         "价格($)": "price", "品牌": "brand", "新老品": "lifecycle", "配送方式": "fulfilment",
         "上架天数": "listing_days", "评分数": "reviews", "评分": "rating",
         "主体材质": "material", "产品类型": "product_type", "套装数量": "set_count",
         "厚": "thickness", "尺寸规格": "spec"}
    k = m.get(name)
    return rec.get(k) if k else None


def missing_rates(recs, ev=None):
    n = len(recs)
    out = []
    for name, key, must in [("价格", "price", True), ("变体月销量", "sales", True),
                            ("变体月销额", "revenue", True), ("品牌", "brand", True),
                            ("新老品", "lifecycle", True), ("配送方式", "fulfilment", True),
                            ("上架天数", "listing_days", False), ("评分数", "reviews", False),
                            ("主体材质", "material", True), ("套装数量", "set_count", True),
                            ("尺寸规格", "spec", True), ("厚", "thickness", False)]:
        miss = sum(1 for r in recs if not _present(r.get(key)))
        out.append({"关键字段": name, "缺失数": miss, "缺失率": share(miss, n),
                    "闸门": "关键字段" if must else "非关键",
                    "判定": "通过" if (not must or share(miss, n) <= 30) else "拒绝出建议(需先补数)"})
    if ev is not None:
        worst = max(out, key=lambda x: x["缺失率"])
        ev.add("数据就绪与口径", "关键字段缺失率(最高)", round(worst["缺失率"], 2),
               "产品源字段非空计数", "缺失≠0; 关键字段>30% 时拒绝出建议",
               "%", n, None, None, note="最高项=%s" % worst["关键字段"])
    return out


def _present(v):
    if v is None:
        return False
    s = str(v).strip()
    return s not in ("", "None", "nan", MISSING, UNKNOWN)


def physical_checks(cat_dir, ev=None):
    """尺寸三值物理校验: 长≥宽≥厚。

    A(宽>长) 与 B(厚不是最小) 是**违规**; C(长=宽) 是圆形/方形砧板的正常形态, 只作提示。
    复用 audit_spec_order(只读扫描)。
    """
    path, sheets = ASO.load(cat_dir)
    rows = sheets["属性标注"] if "属性标注" in sheets else list(sheets.values())[0]
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    iz, ia = hdr.index("尺寸规格"), hdr.index("变体ASIN")
    viol, square, blank, ok = [], 0, 0, 0
    for k in range(1, len(rows)):
        val = str(rows[k][iz] or "").strip()
        if not val or val == MISSING:
            blank += 1
            continue
        hits = ASO.scan(val)
        bad = "".join(sorted({c for h in hits for c in h["bad"] if c in "AB"}))
        if bad:
            viol.append({"xl_row": k + 1, "变体ASIN": str(rows[k][ia] or ""),
                         "尺寸规格": val, "问题": bad})
        else:
            ok += 1
            if any("C" in h["bad"] for h in hits):
                square += 1
    if ev is not None:
        ev.add("数据就绪与口径", "尺寸三值顺序违规行(A/B)", len(viol),
               "scripts/audit_spec_order.py 只读扫描",
               "长≥宽≥厚; A=宽>长, B=厚不是最小; C=长=宽(圆形/方形, 不算违规)", "行")
    return {"ok": ok, "blank": blank, "violations": viol, "square": square,
            "total": len(rows) - 1}


def backlog(cat_dir, ev=None):
    """遗留项分类计数(ASIN 级清单已在 18_标准化 单独交付)。"""
    rows = RI.load(cat_dir)
    hits = RI.classify(rows, cat_dir)
    out = []
    total = 0
    union = set()
    for code, name in RI.CATS:
        out.append({"分类": "%s %s" % (code, name), "行数": len(hits[code])})
        total += len(hits[code])
        union |= {d["xl_row"] for d in hits[code]}
    if ev is not None:
        ev.add("数据就绪与口径", "遗留项(去重行数)", len(union), "scripts/residual_issues.py 分类",
               "10 类问题去重后覆盖的行数; 明细见 遗留项ASIN清单.xlsx", "行", len(rows))
    return {"rows": out, "union": len(union), "sum": total, "total_rows": len(rows)}


def independence(recs, ev=None):
    """独立性统计: 行数 ≠ 样本量。父体/品牌必须并列。"""
    key = lambda r: (r["material"] or UNKNOWN, r["set_count"] or UNKNOWN)
    by = collections.defaultdict(list)
    for r in recs:
        by[key(r)].append(r)
    rows = []
    for k, sub in by.items():
        rows.append({"材质": k[0], "套装": k[1], "变体行数": len(sub),
                     "父体数": len({x["parent"] for x in sub}),
                     "品牌数": len({x["brand"] for x in sub if x["brand"]}),
                     "行数/父体": round(len(sub) / max(1, len({x["parent"] for x in sub})), 2),
                     "销额": sum(x["revenue"] or 0 for x in sub)})
    rows.sort(key=lambda x: -x["销额"])
    overall = {
        "变体行数": len(recs), "父体数": len({r["parent"] for r in recs}),
        "品牌数": len({r["brand"] for r in recs if r["brand"]}),
        "行数/父体": round(len(recs) / max(1, len({r["parent"] for r in recs})), 2),
    }
    if ev is not None:
        ev.add("数据就绪与口径", "独立性(整体)", "行 %d / 父体 %d / 品牌 %d" % (
            overall["变体行数"], overall["父体数"], overall["品牌数"]),
            "产品源 变体ASIN/父ASIN/品牌",
            "同父体多变体重复计入会虚高样本量; 结论格必须并列父体数与品牌数", "—")
    return rows, overall
