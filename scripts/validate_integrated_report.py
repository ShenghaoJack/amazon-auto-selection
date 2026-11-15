# -*- coding: utf-8 -*-
"""校验整合市场报告(制品级 + 数据一致性)。

对齐 build-amazon-integrated-market-report/references/qa-checklist.md 的可自动判定项,
并补上本项目的扩展项(数据就绪/证据溯源/独立性/物理校验)。

用法:
  python scripts/validate_integrated_report.py "<xlsx 路径>" [--pack 指标包.json] [--cat CAT]
退出码: 0 = 全部通过; 1 = 有需注意项。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import openpyxl

ERROR_TOKENS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A")
CORE = ["关键词分析", "市场大盘分析", "VOC差评分析", "选品建议"]
EXT = ["数据就绪与口径", "证据与溯源", "QA自检"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--pack", default=None)
    ap.add_argument("--cat", default="Cutting Boards-US-202608")
    ap.add_argument("--out", default=None, help="把校验结果(JSON)写到该路径")
    args = ap.parse_args()

    issues, warns, info = [], [], {}
    wb = openpyxl.load_workbook(args.workbook, data_only=False)

    # --- 1 结构
    names = wb.sheetnames
    if [n for n in names if n in CORE] != CORE:
        issues.append("核心 sheet 缺失或相对顺序错: %s" % names)
    for n in EXT:
        if n not in names:
            issues.append("扩展 sheet 缺失: %s" % n)

    for n in names:
        ws = wb[n]
        charts = len(getattr(ws, "_charts", []))
        tables = len(ws.tables)
        freeze = str(ws.freeze_panes) if ws.freeze_panes else None
        pct = any("%" in (c.number_format or "")
                  for row in ws.iter_rows() for c in row if c.value is not None)
        pie_bad = []
        for ch in getattr(ws, "_charts", []):
            if type(ch).__name__ in ("PieChart", "DoughnutChart"):
                if getattr(getattr(ch, "dLbls", None), "showPercent", None) is not True:
                    pie_bad.append(type(ch).__name__)
        info[n] = {"rows": ws.max_row, "cols": ws.max_column, "charts": charts,
                   "tables": tables, "freeze": freeze, "percent": pct}
        if tables < 1:
            issues.append("%s: 无筛选表" % n)
        if freeze not in ("A2", "B2"):
            warns.append("%s: 未冻结首行(%s)" % (n, freeze))
        if pie_bad:
            issues.append("%s: 饼图缺百分比标签 %s" % (n, pie_bad))
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and any(t in c.value for t in ERROR_TOKENS):
                    issues.append("%s!%s 公式错误 token" % (n, c.coordinate))

    need = {"关键词分析": 3, "市场大盘分析": 5, "VOC差评分析": 2}
    for n, k in need.items():
        if n not in info:
            continue
        if n == "VOC差评分析":
            # VOC 源未提供时豁免, 但必须写明
            txt = " ".join(str(c.value) for row in wb[n].iter_rows(max_row=30)
                           for c in row if c.value)
            if "未提供" in txt:
                warns.append("VOC差评分析: 源未提供 → 契约要求 %d 张图表无法满足(已在表内声明)" % k)
                if not info[n]["percent"]:
                    warns.append("VOC差评分析: 无百分比格式(源缺失豁免)")
            elif info[n]["charts"] < k:
                issues.append("VOC差评分析: 图表 %d < %d 且未声明源缺失" % (info[n]["charts"], k))
        elif info[n]["charts"] < k:
            issues.append("%s: 图表 %d < %d" % (n, info[n]["charts"], k))

    # 百分比格式: 核心 sheet 除「选品建议」外必须有(VOC 源缺失时豁免)
    for n in CORE:
        if n in info and n != "选品建议" and not info[n]["percent"]:
            if n == "VOC差评分析":
                continue
            issues.append("%s: 无百分比格式(契约要求)" % n)

    # --- 2 与指标包对账
    if args.pack and os.path.exists(args.pack):
        pack = json.load(io.open(args.pack, encoding="utf-8"))
        bp = pack["band_profiles"]
        tot = sum(b["销额占比"] for b in bp)
        info["价格带销额占比合计"] = round(tot, 2)
        if abs(tot - 100) > 0.5:
            issues.append("价格带销额占比合计 %.2f != 100" % tot)
        else:
            info["价格带对账"] = "通过(%.2f%%)" % tot
        k = pack["kpi"]
        info["KPI"] = {kk: k[kk] for kk in ("有效变体", "父体数", "品牌数", "平均价", "中位价",
                                            "成交加权价", "评分0分排除数")}
        if k["有效价格样本"] > k["总行数"]:
            issues.append("有效价格样本 > 总行数")
        labels = {s["id"]: s["标签"] for s in pack["scores"]}
        ex = [i for i, l in labels.items() if l in ("优先开发", "小批验证")]
        info["通过门槛(可执行机会)"] = ex
        info["标签分布"] = labels
        conf_ids = [s["id"] for s in pack["scores"] if (s["分项"]["证据置信度"] or 0) < 50
                    and s["标签"] in ("优先开发", "小批验证")]
        if conf_ids:
            issues.append("证据置信度<50 却进入可执行机会: %s" % conf_ids)
        voc = pack.get("voc_available")
        if not voc:
            # 权重归一化必须披露
            found = any("85" in str(c.value) or "归一化" in str(c.value)
                        for row in wb["选品建议"].iter_rows() for c in row if c.value)
            if not found:
                issues.append("VOC 缺失但选品建议未披露权重归一化")
            else:
                info["VOC缺失披露"] = "通过"
        if pack.get("evidence_count", 0) < 20:
            warns.append("证据行数偏少(%d)" % pack.get("evidence_count", 0))
    else:
        warns.append("未提供指标包, 跳过对账")

    # --- 3 证据 sheet 检查
    if "证据与溯源" in names:
        ws = wb["证据与溯源"]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        hi = None
        for i, r in enumerate(rows):
            if r and "evidence_id" in [str(x) for x in r]:
                hi = i
                break
        if hi is None:
            issues.append("证据与溯源: 未找到表头行(evidence_id)")
        else:
            hdr = [str(x) for x in rows[hi]]
            idx = {h: i for i, h in enumerate(hdr)}
            body = [r for r in rows[hi + 1:] if r and r[idx["evidence_id"]]]
            ids = [r[idx["evidence_id"]] for r in body]
            if len(ids) != len(set(ids)):
                issues.append("证据 evidence_id 重复")
            miss = [r[idx["evidence_id"]] for r in body if not r[idx["来源"]]]
            if miss:
                issues.append("证据缺「来源」的行: %s" % miss[:5])
            miss2 = [r[idx["evidence_id"]] for r in body if not r[idx["口径"]]]
            if miss2:
                issues.append("证据缺「口径」的行: %s" % miss2[:5])
            info["证据行数"] = len(ids)

    result = {"ok": not issues, "issues": issues, "warnings": warns, "details": info}
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(text)
    print(text)
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
