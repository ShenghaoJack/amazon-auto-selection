# -*- coding: utf-8 -*-
"""VOC 工作簿读取 + 「VOC问题价值(15 分)」计算。

契约来源:
  · 工作簿结构 —— skill `analyze-amazon-voc-reviews` 的 V3 14 表;
  · 分项口径 —— `scoring.RULE_NOTES["VOC问题价值"]`:
      "来自 VOC 工作簿的**问题频次 / 优先级 / 严重度 / 方案相关性**"。

设计要点:
  1. **缺失 ≠ 0**: 找不到工作簿或解析失败 → 返回 available=False, 由 scoring 留空并披露;
  2. **可追溯**: 保留每个问题的代表评论ID, 并与证据库 ID 集合求交, 报告只展示可追溯的 ID;
  3. **方案相关性**是 cat_report 侧特有的一层: 只有"我们的产品定义能解决"的问题才计入价值,
     否则只是风险(例如客服/物流/二手/降价类问题与产品定义无关, 相关性上限 0.2);
  4. **覆盖折扣**: 页数上限样本 + 证据条数不足时, 分数不能打满(下限 0.5)。
"""
from __future__ import annotations

import os
import re

import openpyxl

# 问题所属属性 → (可解决性上限, [方案关键词])
# 关键词命中说明"本次产品定义里有对应手段"; 未命中则该问题对本次机会的贡献打折。
SOLUTION_RELEVANCE: dict[str, tuple[float, list[str]]] = {
    "核心效果/净化能力": (1.0, ["cadr", "hepa", "净化", "过滤", "过滤层", "h13", "h14", "洁净"]),
    "噪音/舒适/环境影响": (1.0, ["静音", "低噪", "噪音", "db", "分贝", "睡眠档"]),
    "异味/宠物尿味去除无效": (1.0, ["活性炭", "除味", "异味", "炭", "臭味", "氨"]),
    "毛发吸附无效": (1.0, ["预过滤", "毛发", "进风", "毛屑", "皮屑"]),
    "精准度/传感一致性": (1.0, ["传感", "pm2.5", "监测", "激光", "空气质量"]),
    "软件/App/连接/数据": (1.0, ["app", "wifi", "wi-fi", "智能", "连接", "语音"]),
    "滤芯寿命短/耗材贵": (1.0, ["滤芯", "耗材", "寿命", "可洗", "更换提醒"]),
    "安装/操控/易用": (1.0, ["一键", "操控", "遥控", "触控", "易用", "童锁", "儿童锁"]),
    "安全风险(过热/冒烟/漏电)": (0.8, ["认证", "零臭氧", "carb", "etl", "阻燃", "过热保护"]),
    "虚假或夸大宣传": (0.8, ["真实标注", "第三方", "实测", "认证", "aham"]),
    "整机停止工作/损坏/寿命短": (0.4, ["质保", "品控", "寿命", "可靠性"]),
    "客服售后差": (0.2, ["质保", "售后"]),
    "物流包装破损": (0.2, ["包装"]),
    "退货退款困难": (0.2, []),
    "收到二手或翻新": (0.2, []),
    "价格波动或降价": (0.2, ["价格"]),
    "价格/性价比不满": (0.3, ["性价比", "价格"]),
}
DEFAULT_RELEVANCE = 0.3        # 未匹配到任何方案关键词时的基线(问题仍是市场信息)
PRIORITY_WEIGHT = {"P0": 4.0, "P1": 3.0, "P2": 2.0, "P3": 1.0}
MAX_EVIDENCE_IDS = 400


def _s(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _f(v, default=0.0) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", _s(v))
    try:
        return float(m.group(0)) if m else default
    except ValueError:
        return default


def _find_header(ws, needed: set[str], max_rows: int = 12) -> tuple[int, list[str]]:
    """在前 max_rows 行里找包含最多所需列名的表头行。"""
    best = (0, 1, [])
    for n, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row or max_rows, max_rows),
                                        values_only=True), start=1):
        headers = [_s(c) for c in row]
        score = sum(1 for term in needed if term in headers)
        if score > best[0]:
            best = (score, n, headers)
    return best[1], best[2]


def _rows_by_header(ws, needed: set[str], limit: int = 2000) -> list[dict]:
    """按表头名取数据行(容忍表头不在首行、容忍缺列)。

    2026-10-09 修: 原实现从第 1 行开始扫, 于是**表头之前的说明行**(如
    "负向优先级分 = 20×(0.25F+…)" 这类口径提示)会被当成一条数据读进来。
    现在只从 **表头行之后** 开始取。
    """
    header_row, headers = _find_header(ws, needed)
    if not headers:
        return []
    idx = {h: i for i, h in enumerate(headers) if h}
    out = []
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if not any(_s(row[i]) for i in idx.values() if i < len(row)):
            continue
        out.append({h: (row[i] if i < len(row) else None) for h, i in idx.items()})
        if len(out) >= limit:
            break
    return out


def _evidence_ids(wb) -> set[str]:
    if "评论证据库" not in wb.sheetnames:
        return set()
    ws = wb["评论证据库"]
    _hr, headers = _find_header(ws, {"评论ID", "星级", "完整正文"})
    if "评论ID" not in headers:
        return set()
    i = headers.index("评论ID")
    ids = set()
    for row in ws.iter_rows(min_row=1, values_only=True):
        if i < len(row):
            v = _s(row[i]).upper()
            if re.fullmatch(r"R[A-Z0-9]{5,}", v):
                ids.add(v)
    return ids


def _parse_overview(wb) -> dict:
    out = {}
    if "VOC分析_总览" not in wb.sheetnames:
        return out
    for row in wb["VOC分析_总览"].iter_rows(values_only=True):
        cells = [_s(c) for c in row]
        if len(cells) >= 2 and cells[0]:
            out[cells[0]] = cells[1]
    return out


def _parse_audit(wb) -> dict:
    """抓取审计: 请求数/页数/积分/来源 + 覆盖率子表。

    注意: 该 sheet 里**有多张表**(请求表 / 去重审计 / 覆盖率表), 子表各有自己的表头,
    不能只用首行表头解析(2026-10-09 修)。
    """
    out = {"requests": 0, "pages": 0, "points": 0, "sources": {}, "coverage": []}
    if "数据质量_抓取审计" not in wb.sheetnames:
        return out
    rows = [list(r) for r in wb["数据质量_抓取审计"].iter_rows(values_only=True)]
    header_at: dict[str, int] = {}
    for n, row in enumerate(rows):
        names = [_s(c) for c in row]
        if "来源" in names and "请求页数" in names:
            header_at.setdefault("requests", n)
        if names and names[0] == "父ASIN/查询ASIN":
            header_at["coverage"] = n
    # 请求表
    n = header_at.get("requests")
    if n is not None:
        idx = {h: i for i, h in enumerate(_s(c) for c in rows[n]) if h}
        for row in rows[n + 1:]:
            def cell(name):
                i = idx.get(name)
                return row[i] if i is not None and i < len(row) else None
            src = _s(cell("来源"))
            if src in ("pangolinfo", "sellersprite"):
                out["requests"] += 1
                out["pages"] += int(_f(cell("请求页数")))
                out["points"] += int(_f(cell("消耗积分")))
                out["sources"][src] = out["sources"].get(src, 0) + 1
    # 覆盖率表
    n = header_at.get("coverage")
    if n is not None:
        idx = {h: i for i, h in enumerate(_s(c) for c in rows[n]) if h}
        for row in rows[n + 1:]:
            def cell(name):
                i = idx.get(name)
                return row[i] if i is not None and i < len(row) else None
            asin = _s(cell("父ASIN/查询ASIN"))
            total = int(_f(cell("该ASIN评论总数(totalReviews)")))
            if not asin or not total:
                break                     # 子表结束
            out["coverage"].append({"asin": asin, "total": total,
                                    "fetched": int(_f(cell("本次抓取条数"))),
                                    "rate": _s(cell("覆盖率"))})
    return out


def read_voc_workbook(path: str) -> dict:
    """解析 V3 VOC 工作簿 → 结构化结果(不可用时 available=False + reason)。"""
    if not path or not os.path.exists(path):
        return {"available": False, "reason": "VOC 工作簿路径不存在: %s" % path, "found": []}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        issues = []
        if "差评问题分析" in wb.sheetnames:
            for r in _rows_by_header(wb["差评问题分析"],
                                     {"问题", "负向评论数", "问题占比", "负向优先级分", "优先级",
                                      "安全等级", "代表评论ID"}):
                name = _s(r.get("问题"))
                if not name:
                    continue
                issues.append({
                    "问题": name, "所属属性": _s(r.get("所属属性")), "负向评论数": int(_f(r.get("负向评论数"))),
                    "问题占比": _f(r.get("问题占比")), "F": int(_f(r.get("F"))), "S": int(_f(r.get("S"))),
                    "H": int(_f(r.get("H"))), "T": int(_f(r.get("T"))), "C": int(_f(r.get("C"))),
                    "负向优先级分": _f(r.get("负向优先级分")), "优先级": _s(r.get("优先级")) or "P3",
                    "安全等级": _s(r.get("安全等级")), "代表评论ID": _s(r.get("代表评论ID")),
                    "证据摘录": _s(r.get("证据摘录")),
                })
        drivers = []
        if "好评价值驱动" in wb.sheetnames:
            for r in _rows_by_header(wb["好评价值驱动"],
                                     {"属性", "正向评论数", "正向驱动分", "解读", "代表评论ID"}):
                attr = _s(r.get("属性"))
                if not attr:
                    continue
                drivers.append({"属性": attr, "正向评论数": int(_f(r.get("正向评论数"))),
                                "正向驱动分": _f(r.get("正向驱动分")), "解读": _s(r.get("解读")),
                                "代表评论ID": _s(r.get("代表评论ID")), "证据摘录": _s(r.get("证据摘录"))})
        matrix = []
        if "正负属性对照" in wb.sheetnames:
            for r in _rows_by_header(wb["正负属性对照"],
                                     {"属性", "象限", "正向提及率", "负向问题率", "证据合计"}):
                if _s(r.get("属性")):
                    matrix.append({"属性": _s(r.get("属性")), "象限": _s(r.get("象限")),
                                   "正向提及率": _f(r.get("正向提及率")),
                                   "负向问题率": _f(r.get("负向问题率")),
                                   "证据合计": int(_f(r.get("证据合计")))})
        claims = []
        if "Listing卖点_风险" in wb.sheetnames:
            for r in _rows_by_header(wb["Listing卖点_风险"],
                                     {"Listing 宣称方向", "对应属性", "负向证据条数", "风险等级"}):
                if _s(r.get("Listing 宣称方向")):
                    claims.append({"宣称方向": _s(r.get("Listing 宣称方向")),
                                   "对应属性": _s(r.get("对应属性")),
                                   "负向证据条数": int(_f(r.get("负向证据条数"))),
                                   "风险等级": _s(r.get("风险等级"))})
        ev_ids = _evidence_ids(wb)
        overview = _parse_overview(wb)
        audit = _parse_audit(wb)
    finally:
        wb.close()

    if not issues and not drivers:
        return {"available": False, "path": path,
                "reason": "工作簿缺少「差评问题分析 / 好评价值驱动」内容, 无法作为 VOC 源",
                "found": [path]}

    limitations = ["样本为页数上限内评论合集, 非全部历史评论",
                   "趋势项 T 固定为 3(需 ≥30 条/期且 ≥120 天)"]
    neutral3 = overview.get("3★中评(无明确负向问题)", "")
    if neutral3:
        limitations.append("3★中评(无明确负向问题): " + neutral3)
    return {"available": True, "path": path, "issues": issues, "drivers": drivers,
            "matrix": matrix, "claims": claims, "overview": overview, "audit": audit,
            "evidence_ids": ev_ids, "limitations": limitations,
            "counts": {"issues": len(issues), "drivers": len(drivers),
                       "evidence": len(ev_ids), "requests": audit.get("requests", 0),
                       "pages": audit.get("pages", 0)}}


# ---------------------------------------------------------------- 评分
def solution_relevance(attribute: str, hypothesis: dict) -> tuple[float, str]:
    """方案相关性 0–1: 本次产品定义里是否有对应手段解决该属性上的问题。"""
    cap, keys = SOLUTION_RELEVANCE.get(attribute, (DEFAULT_RELEVANCE, []))
    if not keys:
        return cap, "与产品定义无关(非产品侧可解决), 相关性上限 %.1f" % cap
    # 只拿"我们准备怎么做"的字段做匹配 —— 不含 名称/核心任务(它们描述品类而非差异化,
    # 例如任何空气净化器的名称都含"净化", 否则所有假设都会被判为高相关)。
    text = " ".join(_s(hypothesis.get(k)) for k in
                    ("差异化配置", "目标规格", "基准配置")).lower()
    hit = [k for k in keys if k in text]
    if hit:
        return cap, "命中方案关键词「%s」→ 相关性 %.1f" % ("、".join(hit[:3]), cap)
    return min(cap, DEFAULT_RELEVANCE), "未命中方案关键词 → 相关性 %.1f(基线)" % min(cap, DEFAULT_RELEVANCE)


def coverage_discount(voc: dict) -> tuple[float, str]:
    """覆盖折扣 0.5–1.0: 页数上限样本 + 证据条数 + 抓取覆盖率。"""
    ev = int(voc.get("counts", {}).get("evidence") or 0)
    factor = 1.0
    why = []
    if ev < 30:
        factor *= 0.7
        why.append("证据 %d 条(<30)" % ev)
    elif ev < 60:
        factor *= 0.85
        why.append("证据 %d 条(<60)" % ev)
    cov = voc.get("audit", {}).get("coverage") or []
    rates = []
    for c in cov:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", _s(c.get("rate")))
        if m:
            rates.append(float(m.group(1)))
    if rates:
        avg = sum(rates) / len(rates)
        if avg < 5:
            factor *= 0.8
            why.append("平均抓取覆盖率 %.1f%%(<5%%)" % avg)
        elif avg < 15:
            factor *= 0.9
            why.append("平均抓取覆盖率 %.1f%%(<15%%)" % avg)
    d = max(0.5, min(1.0, factor))
    return d, ("覆盖折扣 %.2f(%s)" % (d, "、".join(why))) if why else "覆盖折扣 1.00(样本充足)"


def voc_issue_value(voc: dict, hypothesis: dict) -> tuple[float | None, str]:
    """VOC问题价值(0–100)。VOC 不可用 → (None, 说明), 由调用方留空并披露。"""
    if not voc or not voc.get("available"):
        return None, "VOC 源未提供 → 该分项留空"
    issues = voc.get("issues") or []
    if not issues:
        return None, "VOC 工作簿无「差评问题分析」行 → 该分项留空"

    total_w = 0.0
    total_v = 0.0
    top = []
    for it in issues:
        prio = PRIORITY_WEIGHT.get(_s(it.get("优先级")).upper() or "P3", 1.0)
        freq = max(0.0, min(1.0, float(it.get("问题占比") or 0.0)))
        sev = max(0.0, min(1.0, (float(it.get("S") or 3) - 1) / 4.0))
        safe = _s(it.get("安全等级"))
        safe_bonus = 1.5 if ("A" in safe or "B" in safe) else 1.25 if "C" in safe else 1.0
        w = prio * (0.5 + 0.5 * freq) * (0.5 + 0.5 * sev) * safe_bonus
        rel, _why = solution_relevance(_s(it.get("所属属性")), hypothesis)
        total_w += w
        total_v += w * rel
        top.append((it.get("问题"), w * rel, rel))
    if total_w <= 0:
        return None, "VOC 问题权重合计为 0 → 该分项留空"
    base = total_v / total_w                       # 0–1
    disc, disc_note = coverage_discount(voc)
    score = round(100.0 * base * disc, 1)
    # 追回可追溯性: 只把存在于证据库的 ID 计入说明
    ev_ids = voc.get("evidence_ids") or set()
    traceable = sum(1 for i in issues
                    for rid in re.findall(r"\bR[A-Z0-9]{5,}\b", _s(i.get("代表评论ID")))
                    if not ev_ids or rid in ev_ids)
    top.sort(key=lambda x: -x[1])
    note = ("VOC问题价值 = 100 × Σ权重×方案相关性 / Σ权重 × %s; 问题 %d 类(证据中可追溯代表ID %d 个); "
            "贡献最大: %s; 相关性口径: 仅「产品定义能解决」的问题计入价值"
            % (disc_note, len(issues), traceable,
               "、".join("%s(相关性%.1f)" % (t[0], t[2]) for t in top[:3])))
    return score, note


def voc_plan_lines(voc: dict) -> list[str]:
    """给报告用的 VOC 行动项(修复/保留), 按优先级与驱动分排序。"""
    out = []
    for it in (voc.get("issues") or [])[:6]:
        out.append("修复: [%s] %s(%d 条, 占比 %.1f%%, 属性 %s)"
                   % (it.get("优先级"), it.get("问题"), it.get("负向评论数"),
                      100 * float(it.get("问题占比") or 0), it.get("所属属性")))
    for d in (voc.get("drivers") or [])[:4]:
        out.append("保留/强化: %s(正向驱动 %.1f, %d 条, %s)"
                   % (d.get("属性"), d.get("正向驱动分"), d.get("正向评论数"), d.get("解读")))
    return out
