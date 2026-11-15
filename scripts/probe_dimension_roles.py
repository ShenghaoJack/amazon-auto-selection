# -*- coding: utf-8 -*-
"""维度角色探测器 —— 只靠数据形状推断每一列在「分区分区」里扮演什么角色。

这是"候选自动枚举"跨类目的关键: 枚举逻辑只认**角色**, 不认类目词汇。
角色体系(与类目无关):
  轴候选   A1 装量/件数   A2 层级/材质   A3 规格(结构化值)   A4 兼容/范围   A5 多值(场景等)
  非轴     M 指标(销额/BSR/评分/上架…)   D 二值功能(差异化)   I 身份(ASIN/标题/URL)
           U 禁用(来源侧不可信)          ? 高基数需归并

用法:
  python scripts/probe_dimension_roles.py --cat "Cutting Boards-US-202608"
"""
from __future__ import annotations

import argparse
import collections
import io
import os
import re

import openpyxl

# ---- 人工声明(跨类目通用、与类目语义无关的最小输入) ----
METRIC_NAME = re.compile(
    r"(销[量额]|销量|销售额|价格|BSR|评论|评分|留评|上架|毛利|FBA|运费|LQS|卖家数|流量|变体数|Coupon)")
IDENT_NAME = re.compile(r"(ASIN|SKU|链接|标题|主图|图片|描述|五点|参数|标签|卖家|品牌|关键词|Buyer|Q&A|说明)")
UNTRUSTED_NAME = re.compile(r"(商品尺寸|包装尺寸|包装重量|商品重量)")          # 来源侧不可信(每类目实测)
BINARY_NAME = re.compile(r"^(是否|有无|可否|能否|带|含|支持|可)")
YESNO = {"是", "否", "Y", "N", "有", "无", "支持", "不支持", "true", "false"}
UNKNOWN = {"未明确", "缺", "", "None", "nan"}

RE_TRIPLE = re.compile(r"\d+(?:\.\d+)?\s*[xX×*]\s*\d+(?:\.\d+)?(?:\s*[xX×*]\s*(?:\d+(?:\.\d+)?|缺))?")
RE_SCALAR_UNIT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z%]{1,6}|inch|cm|mm|kg|g|lb|oz|ml|L|W|V|Nm)\s*$")
RE_COUNT = re.compile(r"^\s*(\d+)\s*(件套|件|只|支|枚|片|条|个|入|PCS|pcs|Pack|pack)\s*$")


def num(v):
    try:
        float(str(v).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def shape(vals):
    nonnull = [str(v).strip() for v in vals if v is not None and str(v).strip() not in UNKNOWN]
    n = len(vals)
    cov = len(nonnull) / n * 100 if n else 0
    uniq = len(set(nonnull))
    numeric = sum(1 for v in nonnull if num(v)) / len(nonnull) * 100 if nonnull else 0
    triple = sum(1 for v in nonnull if RE_TRIPLE.search(v)) / len(nonnull) * 100 if nonnull else 0
    scalar = sum(1 for v in nonnull if RE_SCALAR_UNIT.match(v)) / len(nonnull) * 100 if nonnull else 0
    cnt = sum(1 for v in nonnull if RE_COUNT.match(v)) / len(nonnull) * 100 if nonnull else 0
    multi = sum(1 for v in nonnull if ("," in v or "，" in v)) / len(nonnull) * 100 if nonnull else 0
    dom = collections.Counter(nonnull).most_common(1)[0][0][:18] if nonnull else ""
    return {"col": "", "cov": cov, "uniq": uniq, "num%": numeric, "triple%": triple,
            "scalar%": scalar, "count%": cnt, "multi%": multi, "dom": dom}


def role(name, s):
    if UNTRUSTED_NAME.search(name):
        return "U 禁用(来源侧不可信)"
    if IDENT_NAME.search(name):
        return "I 身份/文本"
    if METRIC_NAME.search(name) and (s["num%"] >= 60):
        return "M 指标"
    if BINARY_NAME.match(name) or (s["uniq"] <= 5 and s["dom"] in YESNO):
        return "D 二值功能"
    if s["count%"] >= 60 and s["uniq"] <= 15:
        return "A1 装量/件数轴"
    if s["triple%"] >= 60:
        return "A3 规格轴(长x宽x高)"
    if s["scalar%"] >= 60 and s["uniq"] > 5:
        return "A3 规格轴(数值+单位)"
    if s["multi%"] >= 30 and s["uniq"] > 5:
        return "A5 多值(场景/标签)"
    if 2 <= s["uniq"] <= 20 and s["cov"] >= 60:
        return "A2 层级/枚举轴"
    if s["uniq"] <= 40 and s["cov"] >= 60:
        return "A5 多值(场景/标签)"
    return "? 高基数需归并/不作为轴"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="Cutting Boards-US-202608")
    ap.add_argument("--file", default=None, help="直接指定 xlsx")
    ap.add_argument("--sheet", default=None, help="直接指定 sheet")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.file:
        path = args.file
    else:
        path = os.path.join(root, "data", args.cat, "18_标准化",
                           "%s-属性标注-标准化交付.xlsx" % args.cat)
        if not os.path.exists(path):
            path = os.path.join(root, "data", args.cat, "%s-产品属性标注.xlsx" % args.cat)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if args.sheet and args.sheet in wb.sheetnames:
        ws = wb[args.sheet]
    else:
        ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    # 去掉「XXX_来源」台账列(它们是溯源字符串, 不是属性)
    drop = {j for j, h in enumerate(hdr) if h.endswith("_来源")}
    hdr = [h for j, h in enumerate(hdr) if j not in drop]
    body = [[v for j, v in enumerate(r) if j not in drop] for r in rows[1:]]

    out, byrole = [], collections.defaultdict(list)
    for j, name in enumerate(hdr):
        s = shape([r[j] if j < len(r) else None for r in body])
        s["col"] = name
        rl = role(name, s)
        byrole[rl].append(s)
        out.append("%-30s 覆盖%6.1f%% 基数%5d 数值%5.1f%% 三元%5.1f%% 标量%5.1f%% 多值%5.1f%% → %s" % (
            name, s["cov"], s["uniq"], s["num%"], s["triple%"], s["scalar%"], s["multi%"], rl))

    lines = ["== 角色分布 =="]
    for rl in sorted(byrole, key=lambda k: -len(byrole[k])):
        items = byrole[rl]
        lines.append("%-24s %2d 列: %s" % (rl, len(items), "、".join(x["col"] for x in items[:10])))
    lines.append("")
    lines.append("== 可作分区轴的列(角色前缀 A) ==")
    for rl in sorted(k for k in byrole if k.startswith("A")):
        for x in byrole[rl]:
            lines.append("  %-24s %-22s 覆盖%.1f%% 基数%d 主导=%-18s" % (
                x["col"], rl, x["cov"], x["uniq"], x["dom"]))
    lines.append("")
    lines.append("== 明细 ==")
    lines += out
    text = "\n".join(lines)
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(text)
    print(text[:2000])
    print("... 共 %d 列, 轴候选 %d 列" % (len(hdr), sum(len(v) for k, v in byrole.items()
                                                   if k.startswith("A"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
