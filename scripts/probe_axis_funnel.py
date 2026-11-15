# -*- coding: utf-8 -*-
"""分层体检漏斗: 96 列 → 各层剩多少 → 最终几根轴。

目的: 证明"跑 96 列" ≠ "用 96 列", 并给出每层淘汰依据(尽量用数据形状, 而非列名词表)。
"""
import argparse
import collections
import io
import os
import re

import openpyxl

RE_TRIPLE = re.compile(r"\d+(?:\.\d+)?\s*[xX×*]\s*\d+(?:\.\d+)?")
RE_SCALAR_UNIT = re.compile(r"^\s*\d+(?:\.\d+)?\s*[A-Za-z%]{1,6}\s*$")
RE_COUNT = re.compile(r"^\s*\d+\s*(件套|件|只|支|枚|片|条|个|入|PCS|pcs|Pack|pack)\s*$")
UNKNOWN = {"未明确", "缺", "", "None", "nan"}
# 仅用于"识别度量", 不用于识别轴
METRIC_NAME = re.compile(r"(销[量额]|销售额|价格|BSR|评论|评分|留评|上架|毛利|FBA|运费|LQS|卖家数|流量|变体数|Coupon)")
OPS_NAME = re.compile(r"(配送|物流|FBA|FBM|BuyBox|新老品|类目路径|小类目|大类目|标识|页面|视频|广告|秒杀|品牌故事)")


def num(v):
    try:
        float(str(v).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="Cutting Boards-US-202608")
    args = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "data", args.cat, "18_标准化", "%s-属性标注-标准化交付.xlsx" % args.cat)
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    ws = wb["属性标注"]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    body = rows[1:]
    n = len(body)

    L = collections.OrderedDict()
    dropped = collections.defaultdict(list)
    survived = list(range(len(hdr)))

    # ---- L1 便宜层: 类型 / 唯一值率 / 覆盖率 / 文本长度
    keep = []
    for j in survived:
        vals = [str(r[j]).strip() for r in body if j < len(r) and r[j] is not None
                and str(r[j]).strip() not in UNKNOWN]
        cov = len(vals) / n * 100 if n else 0
        uniq = len(set(vals))
        u_rate = uniq / len(vals) * 100 if vals else 0
        avg_len = (sum(len(v) for v in vals) / len(vals)) if vals else 0
        if cov < 60:
            dropped["L1 覆盖率<60%"].append(hdr[j])
        elif uniq <= 1:
            dropped["L1 常量(唯一值≤1)"].append(hdr[j])
        elif avg_len > 60:
            dropped["L1 长文本(平均长度>60字符)"].append(hdr[j])
        elif u_rate > 90:
            dropped["L1 唯一值率>90%(身份/近唯一)"].append(hdr[j])
        else:
            keep.append(j)
    L["L1 后剩余"] = keep
    survived = keep

    # ---- L2 形态层: 有没有可解析的形态
    keep2, shapes = [], {}
    for j in survived:
        vals = [str(r[j]).strip() for r in body if j < len(r) and r[j] is not None
                and str(r[j]).strip() not in UNKNOWN]
        if not vals:
            continue
        m = len(vals)
        triple = sum(1 for v in vals if RE_TRIPLE.search(v)) / m
        scalar = sum(1 for v in vals if RE_SCALAR_UNIT.match(v)) / m
        cnt = sum(1 for v in vals if RE_COUNT.match(v)) / m
        multi = sum(1 for v in vals if ("," in v or "，" in v)) / m
        uniq = len(set(vals))
        numeric = sum(1 for v in vals if num(v)) / m
        if triple >= 0.6:
            sh = "A3 三元规格"
        elif scalar >= 0.6 and uniq > 5:
            sh = "A3 标量+单位"
        elif cnt >= 0.6 and uniq <= 15:
            sh = "A1 装量/件数"
        elif multi >= 0.3 and uniq > 5:
            sh = "A5 多值"
        elif 2 <= uniq <= 20:
            sh = "A2/A5 低基数枚举"
        elif numeric >= 0.6:
            sh = "M 数值指标"
        else:
            sh = "X 无可解析形态"
        shapes[hdr[j]] = (sh, uniq)
        if sh.startswith("X"):
            dropped["L2 无可解析形态"].append(hdr[j])
        else:
            keep2.append(j)
    L["L2 后剩余"] = keep2
    survived = keep2

    # ---- L3 轴资格层: 排除指标与运营/结构维度
    keep3 = []
    for j in survived:
        name = hdr[j]
        sh = shapes[name][0]
        if sh == "M 数值指标" or METRIC_NAME.search(name):
            dropped["L3 指标列(不做产品定义轴)"].append(name)
        elif OPS_NAME.search(name):
            dropped["L3 运营/结构维度(只用于对标)"].append(name)
        else:
            keep3.append(j)
    L["L3 后剩余(轴候选)"] = keep3
    survived = keep3

    # ---- L4 判别力 D
    def disc(j):
        by = collections.defaultdict(float)
        tot = 0.0
        for r in body:
            if j >= len(r) or r[j] is None:
                continue
            rev = 0.0
            for k in range(len(hdr) - 1, -1, -1):
                if hdr[k] == "变体月销额":
                    try:
                        rev = float(str(r[k]).replace(",", ""))
                    except (TypeError, ValueError):
                        rev = 0.0
                    break
            by[str(r[j]).strip()] += rev
            tot += rev
        if not tot:
            return 0.0
        sh = [v / tot for v in by.values()]
        H = len(sh)
        if H < 2:
            return 0.0
        mean = sum(sh) / H
        return sum(abs(x - mean) for x in sh) / (2 * (1 - 1.0 / H))

    scored = sorted(((disc(j), hdr[j], shapes[hdr[j]][1]) for j in survived), reverse=True)
    L["L4 判别力排序"] = scored

    out = ["== 分层体检漏斗 =="]
    counts = [("L0 全部列", len(hdr))]
    for k, v in L.items():
        if k == "L4 判别力排序":
            continue
        counts.append((k, len(v)))
    for k, v in counts:
        out.append("  %-26s %3d 列" % (k, v))
    out.append("")
    out.append("== 各层淘汰依据与列名 ==")
    for k in sorted(dropped):
        out.append("  %-32s %2d 列: %s" % (k, len(dropped[k]), "、".join(dropped[k][:12])))
    out.append("")
    out.append("== 存活列的角色 ==")
    for j in survived:
        out.append("  %-22s %-16s 基数%d" % (hdr[j], shapes[hdr[j]][0], shapes[hdr[j]][1]))
    out.append("")
    out.append("== 判别力 D 排序(前 12) ==")
    for d, name, uniq in scored[:12]:
        out.append("  D=%.3f  %-22s 基数%d" % (d, name, uniq))
    text = "\n".join(out)
    print(text)
    io.open("_funnel.txt", "w", encoding="utf-8").write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
