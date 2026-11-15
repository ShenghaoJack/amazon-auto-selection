# -*- coding: utf-8 -*-
"""VOC 报告: 聚合评分 + V3 14 表工作簿 + 调用 skill 校验脚本。

从 `_voc_test/step3_report.py` 移植, 保持**与 skill `validate_voc_workbook.py::V3_SHEETS`
完全一致的 14 张表名与顺序**(表名/顺序错误会被 `--strict-v3` 判 FAIL)。

移植差异:
  · 不复刻图表 —— VOC 工作簿的校验口径只认表/公式/ID 交叉, 图表无校验价值;
  · 公式加**空表保护**: 分母为 0 或区间倒置时不再写入公式(空区间 `SUM($B$5:$B$4)`
    在 Excel 里是错误区间, 会被校验脚本按错误令牌扫出来);
  · 多出 `load_audit` / `load_dedupe_audit` / `score_summary_rows` / `write_summary_csv`:
    把原先散在 `step3_report.main()` 里的"从 raw 重建审计/覆盖率"与 CSV 落盘收进模块,
    供 `pipeline.run_voc` 直接调用。
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from core.io_utils import ensure_dir, read_jsonl
from tools.voc.normalize import row_id as review_row_id
from tools.voc.scoring import (breadth_level, conf_level, lvl5, mean_strength,
                               negative_priority_score, positive_driver_score,
                               positive_verdict, priority_of, recommend_share, safety_h)
from tools.voc.taxonomy import (ATTRIBUTES, NEUTRAL_ISSUE,
                                ROLES, ROLE_STRENGTH, SAFETY_GRADES, SEVERITY_LEVELS)

# skill 校验脚本(默认本机路径; 调用方可显式覆盖)
DEFAULT_VALIDATOR = (r"C:\Users\Jack_New\.dsh\skills\analyze-amazon-voc-reviews"
                     r"\scripts\validate_voc_workbook.py")

HDR_FILL = PatternFill("solid", fgColor="DCE6F1")
HDR_FONT = Font(bold=True)

# 总览里的页数口径(与 tools/voc/acquire 的默认计划一致, 仅作文案)
PAGE_CAP_NOTE = "每父体 5 页(critical 3 / four_star 1 / five_star 1); 数据集为『页数上限内评论合集』"


# ---------------------------------------------------------------- 聚合(阈值/公式见 tools/voc/scoring)
def aggregate(reviews: list[dict], labels: list[dict]) -> dict:
    """正负双向聚合: 正向价值驱动 / 负向问题优先级 / 属性象限 / 场景 / 安全。

    `labels` 与 `reviews` **同序同长**, 缺标注的位置是 `{}`(按"无标签"处理, 不猜测)。
    分母口径: 正向=4–5★集合, 负向=1–3★集合(不得混用, 见 skill data-contract)。
    """
    pos = [i for i, r in enumerate(reviews) if r["star"] >= 4]
    neg = [i for i, r in enumerate(reviews) if r["star"] <= 3]
    total_parents = len({r["parent_asin"] for r in reviews})
    by_attr_pos: dict[str, list[int]] = defaultdict(list)
    by_attr_neg: dict[str, list[int]] = defaultdict(list)
    by_issue: dict[str, list[int]] = defaultdict(list)
    by_scene: dict[str, list[int]] = defaultdict(list)
    safety_rows: list[int] = []
    neutral3: list[int] = []
    for i in pos:
        lab = labels[i] or {}
        by_attr_pos[lab.get("attribute") or "其他/未分类"].append(i)
    for i in neg:
        lab = labels[i] or {}
        by_attr_neg[lab.get("attribute") or "其他/未分类"].append(i)
        issue = str(lab.get("issue_canon") or lab.get("issue") or "未分类").strip()[:24] or "未分类"
        if issue == NEUTRAL_ISSUE:
            neutral3.append(i)               # 3★中评: 计入负向分母, 但不计入问题统计
        else:
            by_issue[issue].append(i)
        if str(lab.get("safety", "none")).upper() in ("A", "B", "C", "D"):
            safety_rows.append(i)
    for i, lab in enumerate(labels):
        sc = str((lab or {}).get("scene_canon") or "").strip()
        if sc in ("未提及", "其他"):
            sc = ""
        if sc:
            by_scene[sc].append(i)

    pos_rows = []
    for attr, idxs in sorted(by_attr_pos.items(), key=lambda x: -len(x[1])):
        share = len(idxs) / max(1, len(pos))
        role_list = [(labels[i] or {}).get("role") or "experienced_benefit" for i in idxs]
        roles = Counter(role_list)
        parents = len({reviews[i]["parent_asin"] for i in idxs})
        P, B, Rr, V, Cc = (lvl5(share), breadth_level(parents, total_parents), lvl5(recommend_share(role_list)),
                           mean_strength(role_list), conf_level(len(idxs)))
        score = positive_driver_score(P, B, Rr, V, Cc)
        pos_rows.append({
            "属性": attr, "正向评论数": len(idxs), "正向提及率": share,
            "P": P, "B": B, "R": Rr, "V": V, "C": Cc, "正向驱动分": round(score, 1),
            "解读": positive_verdict(score),
            "证据角色分布": "、".join("%s×%d" % (k, v) for k, v in roles.most_common()),
            "代表评论ID": "、".join(review_row_id(reviews[i]) for i in idxs[:3]),
            "证据摘录": " / ".join(str((labels[i] or {}).get("quote") or reviews[i]["review_body"])[:60]
                                   for i in idxs[:2]),
        })

    neg_rows = []
    for issue, idxs in sorted(by_issue.items(), key=lambda x: -len(x[1])):
        share = len(idxs) / max(1, len(neg))
        sev = sum(int((labels[i] or {}).get("severity") or 3) for i in idxs) / max(1, len(idxs))
        grades = [g for g in (str((labels[i] or {}).get("safety", "none")).upper() for i in idxs)
                  if g in ("A", "B", "C", "D")]
        H = safety_h(grades)
        F, S, Cc = lvl5(share), int(round(sev)), conf_level(len(idxs))
        T = 3                       # 趋势数据不足(需 ≥30 条/期且 ≥120 天)
        score = negative_priority_score(F, S, H, T, Cc)
        pri = priority_of(score, grades)      # 含 A/B 不低于 P1、仅 D 不得 P0/P1 的硬性规则
        neg_rows.append({
            "问题": issue, "所属属性": (labels[idxs[0]] or {}).get("attribute") or "其他/未分类",
            "负向评论数": len(idxs), "问题占比": share,
            "F": F, "S": S, "H": H, "T": T, "C": Cc, "负向优先级分": round(score, 1),
            "优先级": pri, "安全等级": "、".join(sorted(set(grades))) or "无",
            "代表评论ID": "、".join(review_row_id(reviews[i]) for i in idxs[:3]),
            "证据摘录": " / ".join(str((labels[i] or {}).get("quote") or reviews[i]["review_body"])[:60]
                                   for i in idxs[:2]),
            "根因假设": "需人工确认(基于评论描述, 非因果结论)",
        })

    matrix = []
    pos_shares = {a: len(v) / max(1, len(pos)) for a, v in by_attr_pos.items()}
    neg_shares = {a: len(v) / max(1, len(neg)) for a, v in by_attr_neg.items()}
    nz_pos = [v for v in pos_shares.values() if v > 0]
    nz_neg = [v for v in neg_shares.values() if v > 0]
    med_pos = sorted(nz_pos)[len(nz_pos) // 2] if nz_pos else 0
    med_neg = sorted(nz_neg)[len(nz_neg) // 2] if nz_neg else 0
    for attr in sorted(set(pos_shares) | set(neg_shares)):
        p, n = pos_shares.get(attr, 0.0), neg_shares.get(attr, 0.0)
        cnt = len(by_attr_pos.get(attr, [])) + len(by_attr_neg.get(attr, []))
        if cnt < 5:
            quad = "证据不足(<5 条)"
        elif p >= med_pos and n < med_neg:
            quad = "稳定优势—必须保留"
        elif p >= med_pos and n >= med_neg:
            quad = "争议属性—分场景优化"
        elif p < med_pos and n >= med_neg:
            quad = "明显短板—优先整改"
        else:
            quad = "低感知—监测或降复杂度"
        ids = (by_attr_pos.get(attr, []) + by_attr_neg.get(attr, []))[:3]
        matrix.append({"属性": attr, "正向评论数": len(by_attr_pos.get(attr, [])),
                       "负向评论数": len(by_attr_neg.get(attr, [])), "证据合计": cnt,
                       "正向提及率": p, "负向问题率": n, "证据平衡(pp)": round((p - n) * 100, 1),
                       "象限": quad, "代表评论ID": "、".join(review_row_id(reviews[i]) for i in ids)})
    return {"pos_reviews": pos, "neg_reviews": neg, "pos_rows": pos_rows, "neg_rows": neg_rows,
            "matrix": matrix, "scenes": by_scene, "safety": safety_rows, "neutral3": neutral3,
            "by_attr_pos": by_attr_pos, "by_attr_neg": by_attr_neg,
            "total_parents": total_parents}


# ---------------------------------------------------------------- 工作簿
def write_sheet(wb, title: str, headers: list[str], rows: list[list], note: str | None = None,
                formulas: dict[int, str] | None = None):
    """写一张表: 可选说明行 + 表头样式 + 数据 + 公式列 + 冻结首行 + 列宽。

    `formulas` = {列号: 含 `%(r)s` 的公式模板}; 写公式**不覆盖**已有值, 且空表时跳过
    (空表写公式既无意义, 又容易产生错误区间)。
    """
    ws = wb.create_sheet(title)
    r0 = 1
    if note:
        ws.cell(row=1, column=1, value=note).font = Font(italic=True, color="555555")
        ws.cell(row=2, column=1, value="数据口径: 样本为页数上限内评论合集; "
                                       "占比分母为各自评论集合(正向=4–5星, 负向=1–3星, 全体=全部)")
        r0 = 4
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=r0, column=c, value=h)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(vertical="center")
    for i, row in enumerate(rows, r0 + 1):
        for c, v in enumerate(row, 1):
            ws.cell(row=i, column=c, value=v)
    if rows:
        for col, tpl in (formulas or {}).items():
            for i in range(r0 + 1, r0 + 1 + len(rows)):
                ws.cell(row=i, column=col, value=tpl % {"r": i})
    ws.freeze_panes = ws.cell(row=r0 + 1, column=1)
    for c, h in enumerate(headers, 1):
        # 列宽: 表头与内容抽样取较大者(上限 46, 下限 10), 避免长内容列过窄
        sample = 0
        for row in rows[:40]:
            if c - 1 < len(row):
                sample = max(sample, len(str(row[c - 1])))
        width = max(10, min(46, max(len(str(h)) * 2.1, sample * 1.3) + 4))
        ws.column_dimensions[ws.cell(row=r0, column=c).column_letter].width = width
    return ws


def _dedupe_pairs(dedupe_audit) -> list[tuple[str, object]]:
    """把去重审计统一成 [(名称, 值)] —— 兼容 `dedupe.dedupe_reviews` 的统计 dict、
    `tools/voc/pipeline.write_dedupe_csv` 的扁平 dict 与旧版 CSV 的 [(k, v)] 列表。"""
    if not dedupe_audit:
        return []
    if isinstance(dedupe_audit, dict):
        pairs: list[tuple[str, object]] = []
        for k, v in dedupe_audit.items():
            if isinstance(v, dict):
                pairs += [("%s - %s" % (k, kk), vv) for kk, vv in v.items()]
            else:
                pairs.append((str(k), v))
        return pairs
    return [(str(a), b) for a, b in dedupe_audit]


def build_workbook(reviews: list[dict], labels: list[dict], agg: dict, audit_rows: list[dict],
                   dedupe_audit, coverage: dict, path: str) -> str:
    """构建 V3 14 表工作簿并返回落盘路径。

    表名与顺序与 skill 校验脚本的 `V3_SHEETS` 严格一致(改动前先看校验脚本)。
    校验脚本会做三件事: ①14 表齐 + 无错误令牌 + 至少 1 个公式;
    ②证据库必须有 `评论ID/星级/完整正文` 且评论ID唯一、星级∈1–5;
    ③各分析表「代表评论ID」必须能在证据库里检索到 —— 因此代表性列里出现的 ID
    只能来自 `normalize.row_id()`(有原生 reviewId 用它, 否则 `R`+9 位大写)。
    """
    wb = Workbook()
    wb.remove(wb.active)
    n_pos, n_neg = len(agg["pos_reviews"]), len(agg["neg_reviews"])
    stars = Counter(r["star"] for r in reviews)
    srcs = Counter(s for r in reviews for s in (r.get("sources") or [r.get("source")]))
    n_scene = len(agg["scenes"])

    # 1 总览
    ov_rows = [
        ["采集来源", "、".join("%s:%d" % (k, v) for k, v in srcs.items())],
        ["父ASIN 数", agg["total_parents"]],
        ["去重后唯一评论", len(reviews)],
        ["1–3★(负向)评论", n_neg], ["4–5★(正向)评论", n_pos],
        ["星级分布", "、".join("%d★:%d" % (s, stars[s]) for s in sorted(stars))],
        ["原始出现次数(去重前)", sum(int(r.get("raw_occurrences") or 1) for r in reviews)],
        ["页数上限", PAGE_CAP_NOTE],
        ["未提供项", "价格/销量分母/历史趋势(趋势 T 固定 3=数据不足); 不得把样本占比当作转化率或故障率"],
        ["Top 正向驱动", "、".join("%s(%s分)" % (r["属性"], r["正向驱动分"]) for r in agg["pos_rows"][:3]) or "无"],
        ["Top 负向问题", "、".join("%s(%s)" % (r["问题"], r["优先级"]) for r in agg["neg_rows"][:3]) or "无"],
        ["安全证据条数", len(agg["safety"])],
        ["3★中评(无明确负向问题)", "%d 条(占负向 %d 条的 %.0f%%) — 计入负向分母但不计入问题统计, 已单列披露"
         % (len(agg.get("neutral3", [])), n_neg, 100.0 * len(agg.get("neutral3", [])) / max(1, n_neg))],
        ["标注失败", "%d 条(占 %.1f%%) — LLM 批失败, 未获得标签, 不计入任何属性/问题统计"
         % (agg.get("annotation", {}).get("failed_reviews", 0),
            100.0 * agg.get("annotation", {}).get("failed_reviews", 0) / max(1, len(reviews)))],
    ]
    if agg.get("admission"):
        admission = agg["admission"]
        ov_rows.extend([["步骤18准入前唯一评论", admission["input_reviews"]],
                        ["步骤18准入后分析评论", admission["included_reviews"]],
                        ["准入排除评论（未进入LLM）", admission["excluded_reviews"]],
                        ["分析范围", "仅步骤18中可确认变体；按标准化变体关联父ASIN及品牌，排除原因见评论准入记录"]])
    write_sheet(wb, "VOC分析_总览", ["项目", "值"], ov_rows,
                note="Amazon 1–5 星正负双向 VOC 分析(V3 口径)")

    # 2 抓取审计(数据行 = 每次 MCP 请求; 尾部追加去重审计与覆盖率)
    write_sheet(wb, "数据质量_抓取审计",
                ["来源", "父ASIN", "实际查询ASIN", "是否子体", "星级层", "请求页数", "返回条数",
                 "消耗积分", "状态", "调用时间", "错误"],
                [[r.get("source"), r.get("parent_asin"), r.get("queried_asin"),
                  "是" if r.get("is_child") else "否", r.get("stratum"), r.get("pages"),
                  r.get("rows"), r.get("pointCost"), r.get("status"), r.get("called_at"),
                  str(r.get("error") or "")[:80]] for r in audit_rows],
                note="成功行按请求记录（卖家精灵逐页），失败按任务记录，不重复计算整组断点汇总，也不代表底层网络重试次数；样本不是全部历史评论")
    ws = wb["数据质量_抓取审计"]
    ws.append([])
    ws.append(["去重审计"])
    for k, v in _dedupe_pairs(dedupe_audit):
        ws.append([k, v])
    ws.append([])
    ws.append(["父ASIN/查询ASIN", "该ASIN评论总数(totalReviews)", "本次抓取条数", "覆盖率"])
    for key, cov in (coverage or {}).items():
        total, fetched = int(cov.get("total") or 0), int(cov.get("fetched") or 0)
        ws.append([key, total, fetched, ("%.1f%%" % (100.0 * fetched / total)) if total else "n/a"])

    # 3 好评价值驱动(第 2 列「评论数」+ 第 4 列「正向提及率」公式 = 校验脚本要的"评论数"口径列)
    write_sheet(wb, "好评价值驱动",
                ["属性", "评论数", "正向评论数", "正向提及率", "P", "B", "R", "V", "C", "正向驱动分", "解读",
                 "证据角色分布", "代表评论ID", "证据摘录"],
                [[r["属性"], r["正向评论数"], r["正向评论数"], None, r["P"], r["B"], r["R"], r["V"], r["C"],
                  r["正向驱动分"], r["解读"], r["证据角色分布"], r["代表评论ID"], r["证据摘录"]]
                 for r in agg["pos_rows"]],
                note="正向驱动分 = 20×(0.30P+0.20B+0.20R+0.20V+0.10C); 分母=4–5★评论集合",
                formulas={4: '=IF(C%(r)d=0,0,C%(r)d/' + str(n_pos) + ')'})

    # 4 差评问题分析
    write_sheet(wb, "差评问题分析",
                ["问题", "评论数", "所属属性", "负向评论数", "问题占比", "F", "S", "H", "T", "C",
                 "负向优先级分", "优先级", "安全等级", "代表评论ID", "证据摘录", "根因假设"],
                [[r["问题"], r["负向评论数"], r["所属属性"], r["负向评论数"], None, r["F"], r["S"], r["H"],
                  r["T"], r["C"], r["负向优先级分"], r["优先级"], r["安全等级"], r["代表评论ID"],
                  r["证据摘录"], r["根因假设"]] for r in agg["neg_rows"]],
                note="负向优先级分 = 20×(0.25F+0.30S+0.25H+0.10T+0.10C); T=3 表示趋势数据不足(需≥30条/期且≥120天); "
                     "%d 条『%s』已单列, 不计入本表统计(见 VOC分析_总览)" % (len(agg.get("neutral3", [])), NEUTRAL_ISSUE),
                formulas={5: '=IF(D%(r)d=0,0,D%(r)d/' + str(n_neg) + ')'})

    # 5 正负属性对照(第 8 列「证据平衡」公式; 第 10 列「代表评论ID」供校验脚本核对)
    write_sheet(wb, "正负属性对照",
                ["属性", "评论数", "正向评论数", "负向评论数", "证据合计", "正向提及率", "负向问题率",
                 "证据平衡(pp)", "象限", "代表评论ID"],
                [[r["属性"], r["证据合计"], r["正向评论数"], r["负向评论数"], r["证据合计"], r["正向提及率"],
                  r["负向问题率"], None, r["象限"], r["代表评论ID"]] for r in agg["matrix"]],
                note="证据平衡 = 正向提及率 − 负向问题率(百分点), 不是满意度; 象限按非零占比中位数划分, 最少 5 条证据",
                formulas={8: '=ROUND((F%(r)d-G%(r)d)*100,1)'})

    # 6 安全与合规风险(「评论ID」列同样参与代表ID交叉校验)
    write_sheet(wb, "安全与合规风险",
                ["评论ID", "评论数", "星级", "安全等级", "属性", "证据摘录", "机制/风险", "处理建议"],
                [[review_row_id(reviews[i]), 1, reviews[i]["star"],
                  (labels[i] or {}).get("safety", "none"), (labels[i] or {}).get("attribute", ""),
                  str((labels[i] or {}).get("quote") or reviews[i]["review_body"])[:160],
                  (labels[i] or {}).get("issue", ""),
                  "核实是否为确认事故; D 级(传闻)不得单独升级为 P0/P1"]
                 for i in agg["safety"]],
                note="安全分级: A=确认伤害/起火/漏电; B=直接观察到的危险事件; C=可信危险机制; D=传闻/第三方警告")
    if not agg["safety"]:
        wb["安全与合规风险"].append(["", "", "", "none", "", "样本内未标注出 A–D 级安全证据", "", ""])

    # 7 品牌_ASIN对比
    per_parent: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(reviews):
        per_parent[(r["parent_asin"], r["brand"])].append(i)
    rows7 = []
    for (parent, brand), idxs in sorted(per_parent.items(), key=lambda x: -len(x[1])):
        neg_i = [i for i in idxs if reviews[i]["star"] <= 3]
        issues = Counter((labels[i] or {}).get("issue") for i in neg_i if (labels[i] or {}).get("issue"))
        drivers = Counter((labels[i] or {}).get("attribute") for i in idxs
                          if reviews[i]["star"] >= 4 and (labels[i] or {}).get("attribute"))
        rows7.append([parent, brand, len(idxs), len(neg_i), round(len(neg_i) / max(1, len(idxs)), 3),
                      (issues.most_common(1)[0][0] if issues else ""),
                      (drivers.most_common(1)[0][0] if drivers else ""),
                      round(sum(reviews[i]["star"] for i in idxs) / max(1, len(idxs)), 2)])
    write_sheet(wb, "品牌_ASIN对比", ["父ASIN", "品牌", "评论数", "1–3★数", "负向占比", "主要问题",
                                     "主要驱动", "平均星级"], rows7,
                note="注释: 负面占比的样本量 <10 条时仅作方向参考, 不作品牌结论")

    # 8 用户场景_人群(占比公式的分母区间依赖行数, 空表时不写公式)
    scene_formulas = {}
    if n_scene:
        scene_formulas = {3: '=IF(SUM($B$5:$B$' + str(4 + n_scene) + ')=0,0,B%(r)d/SUM($B$5:$B$'
                            + str(4 + n_scene) + '))'}
    write_sheet(wb, "用户场景_人群", ["场景(评论明确写出)", "评论数", "占比", "代表评论ID"],
                [[s, len(v), None, "、".join(review_row_id(reviews[i]) for i in v[:3])]
                 for s, v in sorted(agg["scenes"].items(), key=lambda x: -len(x[1]))],
                note="仅统计评论中明确写出的场景, 不做人口/用途推断",
                formulas=scene_formulas)

    # 9 配件耗材_服务
    cons = [r for r in agg["neg_rows"] + agg["pos_rows"]
            if any(k in str(r.get("属性", "")) for k in ("耗材", "客服", "包装"))]
    write_sheet(wb, "配件耗材_服务",
                ["属性", "方向", "评论数", "得分/优先级", "代表评论ID", "证据摘录"],
                [[r["属性"], "负向" if "问题" in r else "正向",
                  r.get("负向评论数") or r.get("正向评论数"),
                  r.get("负向优先级分") or r.get("正向驱动分"),
                  r["代表评论ID"], r["证据摘录"]] for r in cons],
                note="配件/耗材/服务类属性的独立视图(持有成本与售后风险)")

    # 10 产品机会矩阵
    write_sheet(wb, "产品机会矩阵", ["属性", "象限", "正向提及率", "负向问题率", "机会类型", "建议动作"],
                [[r["属性"], r["象限"], r["正向提及率"], r["负向问题率"],
                  ("保留并放大" if "稳定优势" in r["象限"] else "分场景优化" if "争议" in r["象限"]
                   else "优先整改" if "短板" in r["象限"] else "监测/降复杂度" if "低感知" in r["象限"] else "补证据"),
                  ("在 Listing 与详情页突出该优势并提供可验证证据" if "稳定优势" in r["象限"]
                   else "针对不同使用场景给出差异化说明与选择指引" if "争议" in r["象限"]
                   else "立项整改并设定可验收指标" if "短板" in r["象限"]
                   else "低成本监测, 不在营销中强调" if "低感知" in r["象限"] else "先补足评论证据再决策")]
                 for r in agg["matrix"]],
                note="机会矩阵由正/负向证据象限推导; 不使用销量因果推断")

    # 11 改进与保留路线图
    road = []
    for r in agg["neg_rows"][:8]:
        road.append([r["优先级"], "整改", r["问题"], r["负向评论数"], r["证据摘录"],
                     "以第三方舱测/自测对比验证"])
    for r in agg["pos_rows"][:8]:
        road.append(["保留", "强化", r["属性"], r["正向评论数"], r["证据摘录"],
                     "在 Listing 用可验证证据强化; 量产一致性抽检"])
    write_sheet(wb, "改进与保留路线图", ["优先级", "类型", "对象", "证据条数", "证据摘录", "验证方式"], road,
                note="每条动作都绑定证据条数与验证方式; 不输出无证据支撑的动作")

    # 12 Listing卖点_风险(第 4 列「负向证据条数」参与代表ID抽检的列名匹配)
    claims = [("静音/低噪", "噪音/舒适/环境影响"), ("覆盖面积", "速度/风量/覆盖"),
              ("除味/异味", "核心效果/净化能力"), ("易用/自动", "软件/App/连接/数据")]
    neg_by_attr: Counter = Counter()
    for r in agg["neg_rows"]:
        neg_by_attr[r["所属属性"]] += r["负向评论数"]
    write_sheet(wb, "Listing卖点_风险",
                ["Listing 宣称方向", "评论数", "对应属性", "负向证据条数", "风险等级", "建议表述调整"],
                [[c, neg_by_attr.get(a, 0), a, neg_by_attr.get(a, 0),
                  ("高" if neg_by_attr.get(a, 0) >= max(3, 0.15 * n_neg) else "中" if neg_by_attr.get(a, 0) else "低"),
                  ("补充具体工况与档位数据, 避免绝对化表述" if neg_by_attr.get(a, 0) else "可保留, 但需有第三方测试支撑")]
                 for c, a in claims],
                note="宣称 vs 证据: 负向证据集中在某属性时, 对应宣称需加限定条件(如档位/面积口径/ACH)")

    # 13 标签字典_参数
    write_sheet(wb, "标签字典_参数", ["类别", "取值", "定义/阈值"],
                [["GitHub参考大类", a, "22维参考，允许新增；小类由LLM根据原文和大类生成"] for a in ATTRIBUTES] +
                [["LLM新增大类", a, "当前评论中生成，非封闭清单"] for a in sorted({label.get("attribute") for label in labels if label and label.get("attribute")} - set(ATTRIBUTES))] +
                [["正向角色(role)", r, "strength=%d" % ROLE_STRENGTH.get(r, 3)] for r in ROLES] +
                [["严重度(severity)", str(s), d] for s, d in sorted(SEVERITY_LEVELS.items(), reverse=True)] +
                [["安全(safety)", g, d] for g, d in sorted(SAFETY_GRADES.items())] +
                [["频次阈值(P/F)", "5/4/3/2/1", "≥20% / 10–19.99% / 5–9.99% / 2–4.99% / <2%"],
                 ["广度(B)", "5/4/3/2/1", "≥10父体或≥50%样本 / 6–9 / 3–5 / 2 / 1"],
                 ["强度(V)", "1–5", "按证据角色取值后**组内取平均并四舍五入**(repeat/switching/delight/recommendation=5, "
                                  "experienced_benefit=4, stated_purchase_reason=3, qualified_praise=2, vague_praise=1)"],
                 ["推荐强度(R)", "5/4/3/2/1", "该属性正向证据中含推荐/复购/换机信号的比例, 阈值同 P"],
                 ["置信(C)", "5/4/3/2/1", "≥30 / 15–29 / 8–14 / 3–7 / <3 条证据"],
                 ["正向驱动分", "0–100", "20×(0.30P+0.20B+0.20R+0.20V+0.10C)"],
                 ["负向优先级分", "0–100", "20×(0.25F+0.30S+0.25H+0.10T+0.10C)"],
                 ["优先级", "P0–P3", "≥80 / 65–79.99 / 50–64.99 / <50; **可信 A/B 级安全证据不得低于 P1**; "
                                     "仅 D 级(传闻)不得单独产生 P0/P1"],
                 ["趋势(T)", "1–5", "需≥30条/期且≥120天, 否则 T=3(数据不足); 本批全部为 3"],
                 ["去重键", "review_id / fallback_key", "主键=Amazon 原生 reviewId; 兜底键=sha1(作者|标题|正文前400字) "
                                                        "**不含日期**(实测两源日期可差 1 天); 另存含日期的严格键供审计"],
                 ["占比分母", "分池", "正向提及率分母=4–5★集合; 负向问题率分母=1–3★集合; 全体显著度=全部集合"],
                 ["问题小类", "LLM自主生成", "根据原文和所属大类生成；同义归并，不强塞其他"],
                 ["场景", "原文明示", "允许保留参考清单外的具体场景，不推断"],
                 ["不可推断项", "禁止", "不得由评论占比推断转化率/故障率/销量因果; 场景仅取评论明写内容"]],
                note="标签与阈值口径(对齐 skill taxonomy-and-scoring)")

    # 14 评论证据库(校验脚本的核心表: 必须有 评论ID/星级/完整正文)
    write_sheet(wb, "评论证据库",
                ["评论ID", "星级", "完整正文", "评论标题", "父ASIN", "变体ASIN", "品牌", "评论日期",
                 "作者", "来源", "核实购买", "Vine", "有用数", "变体属性", "国家", "评论链接",
                 "抓取日期", "属性", "极性", "证据角色", "问题", "严重度", "安全等级", "场景", "去重键", "LLM细分类"],
                [[review_row_id(r), r["star"], r["review_body"], r["review_title"], r["parent_asin"],
                  r["variant_asin"], r["brand"], r["review_date"], r["author"],
                  "、".join(r.get("sources") or [r.get("source")]), "是" if r["verified"] else "否",
                  "是" if r["vine"] else "否", r["helpful"], r["variant_attributes"], r["country"],
                  r["review_url"], r["crawl_date"], (labels[i] or {}).get("attribute", ""),
                  (labels[i] or {}).get("polarity", ""), (labels[i] or {}).get("role", ""),
                  (labels[i] or {}).get("issue", ""), (labels[i] or {}).get("severity", ""),
                  (labels[i] or {}).get("safety", ""), (labels[i] or {}).get("scene", ""),
                  r.get("fallback_key", ""), (labels[i] or {}).get("subcategory", "")] for i, r in enumerate(reviews)],
                note="证据库: 每条唯一评论一行(已按 reviewId 去重); 代表评论ID 均可在本表检索到")

    ensure_dir(os.path.dirname(os.path.abspath(path)))
    wb.save(path)
    return path


# ---------------------------------------------------------------- 审计输入
def load_audit(raw_paths: list[str]) -> tuple[list[dict], dict]:
    """审计以**原始记录(raw JSONL)**为准 —— 它是 append-only 的唯一真值(不是 CSV)。

    返回 `(审计行, 覆盖率)`; 覆盖率按 `queried_asin` 汇总:
    `total` 取该 ASIN 多次请求 `totalReviews` 的最大值, `fetched` 累加实际抓取条数。
    """
    rows: list[dict] = []
    for p in raw_paths:
        for r in read_jsonl(p):
            if r.get("status") == "ok" and r.get("page_checkpointed"):
                continue
            if r.get("status") == "page_ok":
                r = dict(r, status="ok", pages=1)
            if not (r.get("parent_asin") or r.get("queried_asin")):
                asin = str(r.get("asin") or "").upper()      # 兼容首轮旧格式
                r = dict(r, parent_asin=asin, queried_asin=asin, is_child=None, _legacy=True)
            for k in ("pages", "rows", "pointCost", "totalReviews"):
                try:
                    r[k] = int(float(r.get(k) or 0))
                except (TypeError, ValueError):
                    r[k] = 0
            rows.append(r)
    coverage: dict[str, dict] = {}
    for rec in rows:
        if rec.get("status") != "ok":
            continue
        key = rec.get("queried_asin") or rec.get("asin") or ""
        if not key:
            continue
        cov = coverage.setdefault(key, {"total": 0, "fetched": 0})
        cov["total"] = max(cov["total"], int(rec.get("totalReviews") or 0))
        cov["fetched"] += int(rec.get("rows") or 0)
    return rows, coverage


def load_dedupe_audit(path: str) -> list[tuple[str, str]]:
    """读去重审计 CSV(两列: 指标/值); 文件不存在返回空列表。"""
    if not path or not os.path.exists(path):
        return []
    out: list[tuple[str, str]] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and str(row[0]).strip():
                out.append((row[0], row[1]))
    return out


def score_summary_rows(agg: dict) -> list[list]:
    """评分汇总(机器可读)行: 表头 + 正向驱动 + 负向优先级。

    为什么另出 CSV: 工作簿里的占比/平衡列是**Excel 公式**, `data_only` 读取会看到空值,
    下游步骤/人工核对需要一个不走 Excel 计算的数值口径(见 CODE_REVIEW #22)。
    """
    rows: list[list] = [["类型", "对象", "证据数", "得分", "档位/优先级"]]
    for r in agg["pos_rows"]:
        rows.append(["正向驱动", r["属性"], r["正向评论数"], r["正向驱动分"], r["解读"]])
    for r in agg["neg_rows"]:
        rows.append(["负向优先级", r["问题"], r["负向评论数"], r["负向优先级分"], r["优先级"]])
    return rows


def write_summary_csv(path: str, agg: dict) -> str:
    """写评分汇总 CSV(utf-8-sig, 便于 Excel 直接双击打开)。"""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh).writerows(score_summary_rows(agg))
    return path


def write_analysis_jsonl(path: str, reviews: list[dict], labels: list[dict]) -> str:
    """写《分析明细》JSONL: 每条评论一行(评论 + 标签), 供追溯与断点续跑。"""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as fh:
        for i, r in enumerate(reviews):
            rec = {"i": i, "review_id": review_row_id(r), "star": r.get("star"),
                   "parent_asin": r.get("parent_asin"), "label": labels[i] or {}}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------- 校验
def _emit(msg: str, log=None) -> None:
    """安全输出(2026-10-09): 有 log 回调就交给它(步骤日志→文件), 否则安全 print。

    为什么必须容错: 本流程常由 webui 以 stdout=PIPE 起子进程; 看板重启/控制台关闭后管道读端
    消失, 此时任何 print 都会抛 `OSError: [Errno 22] Invalid argument` —— 不加保护会因
    "打印不出一行进度"而让已花掉数千积分的采集结果整步失败(实测踩到过)。
    """
    try:
        (log or print)(msg)
    except OSError:                      # 断管/无效句柄: 丢这一行输出即可, 不影响流程
        pass


def validate_workbook(path: str, validator_py: str = DEFAULT_VALIDATOR,
                      json_path: str | None = None) -> dict:
    """调用 skill 校验脚本: `python <validator> <xlsx> --strict-v3 --json <json>`。

    返回 `{"returncode", "status", "json", "stdout"}`:
      · `status` = 校验脚本 JSON 里的 `status`(PASS/FAIL); 脚本未产出 JSON 时按
        returncode 推断(`0 → PASS`, 否则 `FAIL`), 绝不因"解析不到"就报成功;
      · `json` = 校验脚本的完整 JSON(解析失败为 None), 内含 errors/warnings/公式数/证据条数。
    校验脚本缺失或执行异常不抛错(返回 returncode=-1 + stdout 里的原因), 由调用方决定是否阻断。
    """
    if not validator_py or not os.path.exists(validator_py):
        msg = "校验脚本不存在: %s" % validator_py
        _emit("  " + msg)
        return {"returncode": -1, "status": "SKIP", "json": None, "stdout": msg}
    # 未显式给 json_path 时用临时文件, 读完即删(否则会在产物目录留下校验 JSON 垃圾)
    tmp_json: str
    if json_path:
        tmp_json = json_path
        ephemeral = False
    else:
        tmp_json = os.path.join(os.path.dirname(os.path.abspath(path)), "~voc校验结果.tmp.json")
        ephemeral = True
    cmd = [sys.executable, validator_py, path, "--strict-v3", "--json", tmp_json]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    except OSError as exc:
        msg = "校验脚本执行失败: %s: %s" % (type(exc).__name__, exc)
        _emit("  " + msg)
        return {"returncode": -1, "status": "SKIP", "json": None, "stdout": msg}
    stdout = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    report = None
    for src in (tmp_json, None):
        if src and os.path.exists(src):
            try:
                with open(src, encoding="utf-8-sig") as fh:
                    report = json.load(fh)
            except (json.JSONDecodeError, ValueError, OSError):
                report = None
        if report:
            break
    if report is None:
        # 校验脚本把 JSON 同时打到 stdout; 这里兜底解析, 避免只因 --json 未落盘就判 SKIP
        start = stdout.find("{")
        end = stdout.rfind("}")
        if 0 <= start < end:
            try:
                report = json.loads(stdout[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                report = None
    status = str((report or {}).get("status") or ("PASS" if proc.returncode == 0 else "FAIL"))
    if ephemeral and os.path.exists(tmp_json):
        try:
            os.remove(tmp_json)          # 临时文件: 结果已读入内存
        except OSError:
            pass
    return {"returncode": proc.returncode, "status": status,
            "json": report if isinstance(report, dict) else None, "stdout": stdout}


__all__ = ["DEFAULT_VALIDATOR", "aggregate", "build_workbook", "write_sheet", "validate_workbook",
           "load_audit", "load_dedupe_audit", "score_summary_rows", "write_summary_csv",
           "write_analysis_jsonl"]
