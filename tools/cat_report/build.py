# -*- coding: utf-8 -*-
"""Sheet 装配: 7 个 sheet + 图表 + 样式 + Excel 表 + 冻结首行。

sheet 顺序: 数据就绪与口径 / 关键词分析 / 市场大盘分析 / VOC差评分析 /
           选品建议 / 证据与溯源 / QA自检
前 4 个是 skill 契约的核心 sheet(相对顺序不变), 后 3 个是本项目扩展
(数据就绪闸门、值级溯源、claim 级自检)。
"""
from __future__ import annotations

import collections
import os

from openpyxl import Workbook
from openpyxl.chart import BarChart, DoughnutChart, LineChart, PieChart, Reference, Series
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .engine import MISSING, NOT_PROVIDED, UNKNOWN, f0, f2, g, share

NAVY = "17365D"
TBLUE = "1F4E78"
LBLUE = "D9EAF7"
ACCENT = "5B9BD5"
GREEN = "70AD47"
ORANGE = "C55A11"
RED = "C00000"
BORDER = "C9D6E2"

F_TITLE = Font(name="微软雅黑", size=16, bold=True, color="FFFFFF")
F_MOD = Font(name="微软雅黑", size=13, bold=True, color=NAVY)
F_HDR = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
F_BODY = Font(name="微软雅黑", size=10)
F_NUM = Font(name="Calibri", size=10)
F_RED = Font(name="微软雅黑", size=10, color=RED, bold=True)
F_ORANGE = Font(name="微软雅黑", size=10, color=ORANGE, bold=True)
FILL_TITLE = PatternFill("solid", fgColor=NAVY)
FILL_MOD = PatternFill("solid", fgColor=LBLUE)
FILL_HDR = PatternFill("solid", fgColor=TBLUE)
FILL_KPI = PatternFill("solid", fgColor="F2F7FC")
FILL_WARN = PatternFill("solid", fgColor="FCE4D6")
THIN = Side(style="thin", color=BORDER)
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


class Sheet:
    def __init__(self, wb, name, title, subtitle=""):
        self.ws = wb.create_sheet(name)
        self.name = name
        self.r = 1
        ws = self.ws
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=12)
        c = ws.cell(row=1, column=1, value=title)
        c.font, c.fill = F_TITLE, FILL_TITLE
        c.alignment = Alignment(vertical="center", horizontal="left", indent=1)
        ws.row_dimensions[1].height = 26
        self.r = 2
        if subtitle:
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=12)
            c = ws.cell(row=2, column=1, value=subtitle)
            c.font = Font(name="微软雅黑", size=9, color="595959")
            self.r = 3
        ws.freeze_panes = "A2"
        self._tables = 0

    def blank(self, n=1):
        self.r += n

    def module(self, text):
        ws = self.ws
        ws.merge_cells(start_row=self.r, start_column=1, end_row=self.r, end_column=12)
        c = ws.cell(row=self.r, column=1, value=text)
        c.font, c.fill = F_MOD, FILL_MOD
        c.alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[self.r].height = 20
        self.r += 1

    def note(self, text, warn=False):
        ws = self.ws
        ws.merge_cells(start_row=self.r, start_column=1, end_row=self.r, end_column=12)
        c = ws.cell(row=self.r, column=1, value=text)
        c.font = F_ORANGE if warn else Font(name="微软雅黑", size=9, color="595959")
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[self.r].height = max(14, 13 * (len(str(text)) // 90 + 1))
        if warn:
            c.fill = FILL_WARN
        self.r += 1

    def kv(self, pairs, per_row=4):
        ws = self.ws
        col = 1
        for label, value in pairs:
            c = ws.cell(row=self.r, column=col, value=label)
            c.font = Font(name="微软雅黑", size=9, color=TBLUE, bold=True)
            c.fill, c.border = FILL_KPI, BOX
            v = ws.cell(row=self.r + 1, column=col, value=value)
            v.font = Font(name="Calibri", size=13, bold=True, color=NAVY)
            v.fill, v.border = FILL_KPI, BOX
            col += 1
            if (col - 1) % per_row == 0:
                self.r += 2
                col = 1
        if col != 1:
            self.r += 2

    def table(self, headers, rows, name=None, pct_cols=(), widths=None, warn_rows=(),
              max_cols=None):
        ws = self.ws
        start = self.r
        for j, h in enumerate(headers, 1):
            c = ws.cell(row=self.r, column=j, value=h)
            c.font, c.fill, c.border = F_HDR, FILL_HDR, BOX
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        self.r += 1
        for i, row in enumerate(rows):
            for j, h in enumerate(headers, 1):
                v = row.get(h, "") if isinstance(row, dict) else (row[j - 1] if j - 1 < len(row) else "")
                is_pct = False
                if h in pct_cols:
                    try:
                        v = float(v) / 100.0
                        is_pct = True
                    except (TypeError, ValueError):
                        pass          # 非数值(如「数据不足」)保留原文
                c = ws.cell(row=self.r, column=j, value=v)
                c.font = F_NUM if isinstance(v, (int, float)) else F_BODY
                c.border = BOX
                c.alignment = Alignment(vertical="top", wrap_text=not isinstance(v, (int, float)))
                if is_pct:
                    c.number_format = "0.0%"
                elif isinstance(v, float):
                    c.number_format = "#,##0.00"
            if i in warn_rows:
                for j in range(1, len(headers) + 1):
                    ws.cell(row=self.r, column=j).fill = FILL_WARN
            self.r += 1
        if name:
            self._tables += 1
            ref = "A%d:%s%d" % (start, get_column_letter(len(headers)), self.r - 1)
            t = Table(displayName="T_%s_%d" % (self.name.replace(" ", "")[:8], self._tables), ref=ref)
            t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
            try:
                ws.add_table(t)
            except Exception:
                pass
        if widths:
            for j, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(j)].width = w
        self.r += 1

    def add(self, ch, anchor=None, w=16, h=8):
        ch.width, ch.height = w, h
        self.ws.add_chart(ch, anchor or ("A%d" % self.r))
        return ch


# ---------------------------------------------------------------- charts
def _pie(title, labels, values, row_anchor, sheet):
    ch = DoughnutChart()
    ch.title = title
    data = Reference(sheet.ws, min_col=2, min_row=row_anchor[0], max_row=row_anchor[1])
    cats = Reference(sheet.ws, min_col=1, min_row=row_anchor[0], max_row=row_anchor[1])
    ch.add_data(data, titles_from_data=False)
    ch.set_categories(cats)
    ch.dataLabels = DataLabelList()
    ch.dataLabels.showPercent = True
    return ch


def _bar(title, cats_ref, data_ref, sheet, horizontal=False):
    ch = BarChart()
    ch.type = "bar" if horizontal else "col"
    ch.title = title
    ch.add_data(data_ref, titles_from_data=True)
    ch.set_categories(cats_ref)
    ch.legend = None
    return ch


# ---------------------------------------------------------------- Sheet0
def sheet_readiness(wb, R, ev):
    s = Sheet(wb, "数据就绪与口径",
              "数据就绪与口径 · 闸门报告",
              "原则: 字段没核实, 不得进入决策。本页任一闸门不通过, 后续 sheet 的结论必须降级或拒绝输出。")
    trust = R["trust"]
    s.module("一、字段可信度表")
    s.table(["字段", "可信度", "用途", "缺失率", "判定来源"], trust,
            name="t_trust", pct_cols=("缺失率",), widths=[26, 14, 46, 10, 18])
    s.note("禁用列: 商品尺寸 / 包装尺寸 / 包装重量 —— 本项目实测与真实板体尺寸常不一致, 不得作为任何规格结论的依据。")
    s.module("二、关键字段缺失率(闸门: 关键字段 > 30% 即拒绝出建议)")
    s.table(["关键字段", "缺失数", "缺失率", "闸门", "判定"], R["missing"],
            name="t_missing", pct_cols=("缺失率",), widths=[18, 10, 10, 12, 26])
    s.module("三、物理合理性校验(尺寸三值顺序: 长 ≥ 宽 ≥ 厚)")
    pc = R["physical"]
    s.note("校验器: scripts/audit_spec_order.py(只读)。规则: 长必须最大、厚必须最小; 宽>长或厚>max(长,宽) 均为违规。")
    s.kv([("受检行数", pc["total"]), ("合规行数", pc["ok"]),
          ("长=宽(圆形/方形, 不算违规)", pc.get("square", 0)), ("尺寸规格为空", pc["blank"]),
          ("A/B 违规行数", len(pc["violations"]))])
    if pc["violations"]:
        s.table(["xl_row", "变体ASIN", "尺寸规格", "问题"], pc["violations"][:50],
                name="t_phys", widths=[8, 14, 46, 8])
        s.note("存在未修正的物理违规(A=宽>长 / B=厚不是最小) → 违规行不得进入选品结论。", warn=True)
    else:
        s.note("A/B 违规 0 行(本批已修正 88 行顺序问题, 明细见 尺寸规格顺序审查.md); "
               "长=宽 的 %d 行为圆形/方形砧板正常形态, 不计违规。" % pc.get("square", 0))
    s.module("四、遗留项分类计数(ASIN 级清单另见同目录 xlsx)")
    s.table(["分类", "行数"], R["backlog"]["rows"], name="t_backlog", widths=[40, 10])
    s.note("遗留项去重后覆盖 %d / %d 行(%.1f%%); 与结论相关的遗留项须在结论旁标注。" % (
        R["backlog"]["union"], R["backlog"]["total_rows"],
        share(R["backlog"]["union"], R["backlog"]["total_rows"])))
    s.module("四、遗留项分类计数(ASIN 级清单另见同目录 xlsx)")
    bl = R["backlog"]
    if bl.get("applicable", True):
        s.table(["分类", "行数"], bl["rows"], name="t_backlog", widths=[40, 10])
        s.note("遗留项去重后覆盖 %d / %d 行(%.1f%%); 与结论相关的遗留项须在结论旁标注。" % (
            bl["union"], bl["total_rows"], share(bl["union"], bl["total_rows"])))
    else:
        s.note("不适用: %s" % bl.get("note", "本类目未配置遗留项分类规则"), warn=True)
        s.note("原因: %s" % bl.get("reason", ""))
    s.module("五、独立性统计(行数 ≠ 样本量; 父体数/品牌数必须并列)")
    ov = R["independence_overall"]
    s.kv([("变体行数", ov["变体行数"]), ("父体数", ov["父体数"]),
          ("品牌数", ov["品牌数"]), ("行数/父体", ov["行数/父体"])])
    s.table(["材质", "套装", "变体行数", "父体数", "品牌数", "行数/父体", "销额"],
            R["independence"][:24], name="t_indep",
            widths=[18, 10, 10, 9, 9, 10, 14])
    s.note("同一父体的多个变体会被重复计入行数; 因此本报告所有结论格都并列父体数与品牌数, 并在父体数 <5 时降级标注。")
    return s


# ---------------------------------------------------------------- Sheet1
def sheet_keyword(wb, kw, s0):
    s = Sheet(wb, "关键词分析", "关键词分析",
              "数据源: FR-08 %s-keyword-report.xlsx(核心词分析 / 季节性分析 sheet)。" % "")
    if kw.get("missing"):
        s.note("关键词工作簿缺失 → 本 sheet 无法生成, 需求评分中「关键词需求」记 未提供。", warn=True)
        return s
    kws = [k for k in kw["keywords"] if k["search"]]
    kws.sort(key=lambda k: -(k["search"] or 0))
    qual = [k for k in kws if k["verdict"] == "达标"]
    s.module("一、结论摘要")
    s.kv([("关键词总数", len(kw["keywords"])), ("有效搜索量样本", len(kws)),
          ("达标核心词", len(qual)), ("最高月搜索量", f0(kws[0]["search"]) if kws else "—"),
          ("长尾词条数", len(kw["longtail"]))])
    top3 = kws[:3]
    s.module("二、核心关键词(按站内月搜索量取 Top3)")
    rows = [{"关键词": k["keyword"], "站内月搜索量": k["search"], "站内同比%": k["mom_yoy"],
             "Google指数": k["google_index"], "Google同比%": k["google_yoy"], "CPC": k["cpc"],
             "CPC下限": k["cpc_min"], "CPC上限": k["cpc_max"], "达标判定": k["verdict"]}
            for k in top3]
    s.table(["关键词", "站内月搜索量", "站内同比%", "Google指数", "Google同比%", "CPC",
             "CPC下限", "CPC上限", "达标判定"], rows, name="t_core",
            pct_cols=("站内同比%", "Google同比%"),
            widths=[30, 15, 12, 11, 12, 8, 9, 9, 10])
    s.add(_bar("核心词月搜索量", Reference(s.ws, min_col=1, min_row=s.r - len(rows) - 1,
                                        max_row=s.r - 2),
               Reference(s.ws, min_col=2, min_row=s.r - len(rows) - 1, max_row=s.r - 1),
               s, horizontal=True))
    s.blank(9)
    s.module("三、CPC 对比")
    s.table(["关键词", "CPC", "CPC下限", "CPC上限"], 
            [{"关键词": k["keyword"], "CPC": k["cpc"], "CPC下限": k["cpc_min"], "CPC上限": k["cpc_max"]}
             for k in top3], name="t_cpc", widths=[30, 10, 10, 10])
    s.add(_bar("核心词 CPC", Reference(s.ws, min_col=1, min_row=s.r - len(top3) - 1, max_row=s.r - 2),
               Reference(s.ws, min_col=2, min_row=s.r - len(top3) - 1, max_row=s.r - 1), s))
    s.blank(9)
    s.module("四、搜索趋势(月度序列)")
    month_rows = kw.get("seasonality", [])
    if month_rows:
        hdr = ["关键词"] + ["%d月" % i for i in range(1, 13)]
        s.table(hdr, month_rows[:4], name="t_season",
                widths=[26] + [9] * 12)
        rows_n = min(4, len(month_rows))
        base = s.r - rows_n - 1
        ch = LineChart()
        ch.title = "月度搜索趋势"
        ch.add_data(Reference(s.ws, min_col=2, min_row=base, max_row=base + rows_n),
                    titles_from_data=True, from_rows=True)
        ch.set_categories(Reference(s.ws, min_col=2, min_row=base, max_col=13, max_row=base))
        s.add(ch, w=22)
        s.blank(9)
    s.module("五、长尾词(Top1 及其余)")
    s.table(["长尾词", "维度", "说明"], [{"长尾词": x["keyword"], "维度": x["dim"] or "—",
                                 "说明": "来自 FR-04 关键词文件"} for x in kw["longtail"][:8]],
            name="t_long", widths=[40, 12, 26])
    s.note("替代声明: " + "; ".join(kw["substitution"]))
    s.module("六、总结与洞察(≥5 条)")
    ins = keyword_insights(kw)
    for i, t in enumerate(ins, 1):
        s.note("%d. %s" % (i, t))
    return s


def keyword_insights(kw):
    kws = [k for k in kw["keywords"] if k["search"]]
    kws.sort(key=lambda k: -(k["search"] or 0))
    out = []
    if not kws:
        return ["关键词工作簿无有效搜索量样本, 无法给出需求侧洞察。"]
    top = kws[0]
    qual = [k for k in kws if k["verdict"] == "达标"]
    out.append("类目需求集中在单一主词: 「%s」站内月搜索量 %s, 是第 2 名「%s」的 %s 倍; 意味着搜索流量入口极窄, Listing 必须正面争夺该词。" % (
        top["keyword"], f0(top["search"]),
        kws[1]["keyword"] if len(kws) > 1 else "—",
        g((top["search"] / kws[1]["search"]) if len(kws) > 1 and kws[1]["search"] else 0, "%.1f")))
    out.append("达标判定: %d 个词里仅 %d 个达标(%s); 其余多因关键字段缺失被判「数据不足」, 关键词侧的类目可行性结论建立在单一样本上。" % (
        len(kws), len(qual), "、".join(k["keyword"] for k in qual[:3])))
    if top["cpc"]:
        out.append("CPC 区间 %.2f–%.2f(点值 %.2f): 按 %s 次月搜索量、3%% 点击率粗算, 单日 100 次点击的广告成本约 $%.0f——这是新品期必须计入的成本项。" % (
            top["cpc_min"] or 0, top["cpc_max"] or 0, top["cpc"],
            f0(top["search"]), 100 * (top["cpc"] or 0)))
    if top["mom_yoy"] is not None:
        out.append("站内同比 %.1f%%、Google 同比 %s%%: 站内与站外趋势方向%s, 需求侧%s。" % (
            top["mom_yoy"], g(top["google_yoy"], "%.1f"),
            "一致" if (top["google_yoy"] or 0) * (top["mom_yoy"] or 1) > 0 else "不一致",
            "仍在增长" if (top["mom_yoy"] or 0) > 0 else "处于收缩"))
    if top["season_in"]:
        out.append("季节性: 站内%s; 备货与广告节奏应据此前置, 而非按自然年平铺。" % top["season_in"])
    if kw["longtail"]:
        out.append("长尾侧仅 %d 条来自 FR-04 文件, 且报告未回填维度字段——长尾的机会判断目前证据不足, 需先补齐维度与搜索量再用于选品。" % len(kw["longtail"]))
    return out


# ---------------------------------------------------------------- Sheet2
def sheet_market(wb, M, ev):
    s = Sheet(wb, "市场大盘分析", "市场大盘分析",
              "口径: 销量只用 变体月销量; 销额只用 变体月销额; 平均价=有效价格算术平均; 成交加权价另列且不得当作平均价; 评分均值排除 评分=0。")
    k = M["kpi"]
    s.module("一、报告摘要")
    s.kv([("有效变体", k["有效变体"]), ("月销量合计", f0(k["月销量合计"])),
          ("月销额合计", "$" + f0(k["月销额合计"])), ("平均价", "$" + f2(k["平均价"])),
          ("中位价", "$" + f2(k["中位价"])), ("成交加权价", "$" + f2(k["成交加权价"])),
          ("评分均值(排除0分)", f2(k["评分均值(排除0分)"])), ("评分0分排除数", k["评分0分排除数"])])
    s.note("父体数 %d / 品牌数 %d(独立性口径); 评分均值与平均价的分母见「证据与溯源」sheet。" % (
        k["父体数"], k["品牌数"]))

    s.module("二、市场大盘与价格段(动态选段)")
    s.note("选段理由: " + M["band_meta"]["rationale"])
    rows = [{"价格段": p["label"], "样本": p["样本"], "最低价": p["min"], "最高价": p["max"],
             "销量": p["销量"], "销量占比": p["销量占比"], "销额": p["销额"], "销额占比": p["销额占比"],
             "产品类型Top1": (p["产品类型"][0]["值"] + " %.0f%%" % p["产品类型"][0]["销额占比"]) if p["产品类型"] else "—",
             "材质Top1": (p["主体材质"][0]["值"] + " %.0f%%" % p["主体材质"][0]["销额占比"]) if p["主体材质"] else "—",
             "套装Top1": (p["套装结构"][0]["值"] + " %.0f%%" % p["套装结构"][0]["销额占比"]) if p["套装结构"] else "—",
             "尺寸中位(长x宽)": "%sx%s" % (g(p["尺寸中位"][0]), g(p["尺寸中位"][1])),
             "尺寸样本": p["尺寸样本"], "厚度中位": p["厚度中位"], "厚度样本": p["厚度样本"],
             "样本标记": "sparse" if p["sparse"] else ""}
            for p in M["band_profiles"]]
    warn = tuple(i for i, p in enumerate(M["band_profiles"]) if p["sparse"])
    s.table(["价格段", "样本", "最低价", "最高价", "销量", "销量占比", "销额", "销额占比",
             "产品类型Top1", "材质Top1", "套装Top1", "尺寸中位(长x宽)", "尺寸样本",
             "厚度中位", "厚度样本", "样本标记"], rows, name="t_band",
            pct_cols=("销量占比", "销额占比"), warn_rows=warn,
            widths=[13, 7, 8, 8, 10, 9, 12, 9, 18, 18, 12, 15, 9, 9, 9, 9])
    anchor = s.r - len(rows) - 1
    ch = BarChart()
    ch.title = "价格段销额占比"
    ch.add_data(Reference(s.ws, min_col=8, min_row=anchor, max_row=anchor + len(rows)),
                titles_from_data=True)
    ch.set_categories(Reference(s.ws, min_col=1, min_row=anchor + 1, max_row=anchor + len(rows)))
    ch.legend = None
    s.add(ch, w=20)
    s.blank(9)

    s.module("三、新品/老品分析")
    lc = M["lifecycle"]
    s.table(["组", "变体数", "数量占比", "销量", "销量占比", "销额", "销额占比"], lc,
            name="t_life", pct_cols=("数量占比", "销量占比", "销额占比"),
            widths=[12, 9, 10, 12, 10, 14, 10])
    a2 = s.r - len(lc) - 1
    pie = PieChart()
    pie.title = "新品/老品 销额占比"
    pie.add_data(Reference(s.ws, min_col=7, min_row=a2, max_row=a2 + len(lc)), titles_from_data=True)
    pie.set_categories(Reference(s.ws, min_col=1, min_row=a2 + 1, max_row=a2 + len(lc)))
    pie.dataLabels = DataLabelList()
    pie.dataLabels.showPercent = True
    s.add(pie, w=13)
    s.blank(8)

    s.module("四、品牌与履约集中度")
    c = M["concentration"]
    s.kv([("CR1", "%.1f%%" % c["CR1"]), ("CR3", "%.1f%%" % c["CR3"]),
          ("CR5", "%.1f%%" % c["CR5"]), ("HHI", f0(c["HHI"]))])
    s.table(["品牌", "变体数", "新品数", "销量", "销额", "销额占比", "价格区间", "平均价",
             "中位价", "有效评分(排除0)", "履约结构"], M["brands"],
            name="t_brand", pct_cols=("销额占比",),
            widths=[18, 8, 8, 10, 12, 9, 18, 9, 9, 13, 20])
    b2 = s.r - len(M["brands"]) - 1
    ch = BarChart()
    ch.title = "品牌销额占比 Top10"
    ch.add_data(Reference(s.ws, min_col=6, min_row=b2, max_row=b2 + len(M["brands"])),
                titles_from_data=True)
    ch.set_categories(Reference(s.ws, min_col=1, min_row=b2 + 1, max_row=b2 + len(M["brands"])))
    ch.legend = None
    s.add(ch, w=20)
    s.blank(9)
    s.table(["履约", "变体数", "销额", "销额占比"], M["fulfilment"], name="t_ful",
            pct_cols=("销额占比",), widths=[14, 9, 14, 10])
    fl2 = s.r - len(M["fulfilment"]) - 1
    pie = PieChart()
    pie.title = "履约方式 销额占比"
    pie.add_data(Reference(s.ws, min_col=4, min_row=fl2, max_row=fl2 + len(M["fulfilment"])),
                 titles_from_data=True)
    pie.set_categories(Reference(s.ws, min_col=1, min_row=fl2 + 1, max_row=fl2 + len(M["fulfilment"])))
    pie.dataLabels = DataLabelList()
    pie.dataLabels.showPercent = True
    s.add(pie, w=13)
    s.blank(8)

    s.module("五、产品类型与属性结构")
    for label, tab in M["structure"].items():
        s.note("【%s】" % label + "、".join("%s %d 个变体, 销额占比 %.1f%%" % (
            x["值"], x["变体数"], x["销额占比"]) for x in tab[:6]))
    s.table(["类型", "变体数", "销额", "销额占比"], M["structure"]["产品类型"],
            name="t_type", pct_cols=("销额占比",), widths=[22, 9, 14, 10])
    s.table(["材质", "变体数", "销额", "销额占比"], M["structure"]["主体材质"],
            name="t_mat", pct_cols=("销额占比",), widths=[18, 9, 14, 10])
    s.table(["套装结构", "变体数", "销额", "销额占比"], M["structure"]["套装结构"],
            name="t_set", pct_cols=("销额占比",), widths=[16, 9, 14, 10])

    s.module("六、功能溢价分析(横截面对比, 关联非因果)")
    s.table(["功能", "明确数", "覆盖率", "完整度", "有n", "无n", "有中位价", "无中位价",
             "中位价差", "备注"], M["premium"], name="t_prem",
            pct_cols=("覆盖率", "完整度"), widths=[20, 8, 9, 9, 7, 7, 10, 10, 10, 34])
    p2 = s.r - len(M["premium"]) - 1
    ch = BarChart()
    ch.title = "功能有无的中位价差"
    ch.add_data(Reference(s.ws, min_col=9, min_row=p2, max_row=p2 + len(M["premium"])),
                titles_from_data=True)
    ch.set_categories(Reference(s.ws, min_col=1, min_row=p2 + 1, max_row=p2 + len(M["premium"])))
    ch.legend = None
    s.add(ch, w=20, h=10)
    s.blank(11)

    s.module("七、价格段 × 功能差异(仅统计「明确为是/否」, 未明确不计为否; 括号内为证据完整度)")
    hdr = ["功能"] + [p["label"] for p in M["band_profiles"]]
    rows = []
    for f in M["features"]:
        row = {"功能": f}
        for p in M["band_profiles"]:
            d = p["功能"][f]
            row[p["label"]] = ("%.0f%% (%.0f%%)" % (d["prev"], d["complete"])) if d["known"] else "—"
        rows.append(row)
    s.table(hdr, rows, name="t_heat", widths=[20] + [14] * len(M["band_profiles"]))

    s.module("八、价格配置阶梯矩阵(相邻档变化; 阈值: 新增=前<10%且当前≥20%; 强化≥+15pp; 回落≤−15pp; 已知基数<5 或完整度<50% 记证据不足)")
    s.table(["价格段", "样本", "新增配置", "覆盖率提升", "配置回落", "稳定", "证据不足"],
            [{"价格段": r["band"], "样本": r["样本"],
              "新增配置": "；".join(r["新增配置"]) or "—",
              "覆盖率提升": "；".join(r["覆盖率提升"]) or "—",
              "配置回落": "；".join(r["配置回落"]) or "—",
              "稳定": "；".join(r["稳定"][:4]) or "—",
              "证据不足": "；".join(r["证据不足"][:3]) or "—"} for r in M["staircase"]],
            name="t_stair", widths=[13, 7, 34, 34, 30, 34, 30])
    s.note("阶梯单调性: " + M["staircase_note"])

    s.module("九、Top20 产品分析(按 变体月销额 降序; 与全市场使用同一套价格带)")
    s.table(["价格段", "Top20数量", "Top20销额", "Top20销额内占比", "样本标记"],
            [{"价格段": x["band"], "Top20数量": x["Top20数量"], "Top20销额": x["Top20销额"],
              "Top20销额内占比": x["Top20销额内占比"],
              "样本标记": "sparse(<3)" if x["sparse"] else ""} for x in M["top20_bands"]],
            name="t_top20b", pct_cols=("Top20销额内占比",), widths=[13, 11, 14, 14, 12])
    s.table(["排名", "变体ASIN", "品牌", "价格", "销量", "销额", "价格段", "产品类型",
             "主体材质", "套装结构", "尺寸规格", "厚度"],
            [{"排名": x["排名"], "变体ASIN": x["asin"], "品牌": x["品牌"], "价格": x["价格"],
              "销量": x["销量"], "销额": x["销额"], "价格段": x["band"],
              "产品类型": x["产品类型"], "主体材质": x["主体材质"], "套装结构": x["套装结构"],
              "尺寸规格": x["尺寸规格"], "厚度": x["厚度"]} for x in M["top20"]],
            name="t_top20", widths=[6, 13, 16, 8, 9, 12, 12, 18, 14, 10, 40, 14])
    s.note("Top20 产品级价格配置阶梯矩阵(每个 ASIN 恰好一次, 按价格段分组、组内按价格升序):")
    mat_rows = []
    for f in ["产品类型", "主体材质", "套装结构", "尺寸规格", "厚度"]:
        row = {"属性": f}
        for x in M["top20"]:
            row["#%d" % x["排名"]] = str(x[f] or UNKNOWN)
        mat_rows.append(row)
    s.table(["属性"] + ["#%d" % x["排名"] for x in M["top20"]], mat_rows,
            name="t_top20m", widths=[12] + [16] * len(M["top20"]))
    s.note("Top20 vs 全市场(同价格带): " + M["top20_vs_market"])

    s.module("十、Top5 品牌组合对标")
    s.table(["品牌", "变体数", "销额占比", "价格阶梯", "主力功能", "代表变体", "VOC"],
            M["top5"], name="t_top5", pct_cols=("销额占比",),
            widths=[18, 8, 9, 30, 34, 26, 26])

    s.module("十一、规格组合阶梯(扩展模块: 多件套的逐件尺寸组合 × 产出 × 独立性)")
    s.note("本模块是本项目对 skill 契约的补充: 契约里「套装结构」只是一个分类行, 会丢掉具体的逐件尺寸组合; 而在多件套类目里, 决定性的证据恰恰是这个组合。")
    s.table(["套装", "阶梯", "行数", "父体数", "品牌数", "销额", "销量", "单行均销额",
             "材质", "双面异材质行数"], M["spec_ladder"][:20], name="t_ladder",
            widths=[9, 46, 7, 8, 8, 13, 10, 12, 26, 13])
    a3 = s.r - min(20, len(M["spec_ladder"])) - 1
    ch = BarChart()
    ch.title = "尺寸阶梯销额 Top"
    ch.add_data(Reference(s.ws, min_col=6, min_row=a3,
                          max_row=a3 + min(12, len(M["spec_ladder"]))), titles_from_data=True)
    ch.set_categories(Reference(s.ws, min_col=2, min_row=a3 + 1,
                                max_row=a3 + min(12, len(M["spec_ladder"]))))
    ch.legend = None
    s.add(ch, w=22, h=11)
    s.blank(12)

    s.module("十二、市场总结与洞察(≥8 条)")
    for i, t in enumerate(M["insights"], 1):
        s.note("%d. %s" % (i, t))
    return s


# ---------------------------------------------------------------- Sheet3
def sheet_voc(wb, voc):
    s = Sheet(wb, "VOC差评分析", "VOC差评分析",
              "数据源角色: VOC 工作簿(1–3 星评论归类分析)。")
    if not voc.get("available"):
        s.module("VOC 源未提供 —— 本 sheet 无法生成")
        s.note("原因: " + voc["reason"], warn=True)
        s.note("已检索目录: %s(无 VOC/差评 命名的文件)" % "data/<CAT>/")
        s.module("替代与不可替代的边界")
        s.table(["可用替代", "能否替代 1–3 星评论归类", "说明"],
                [{"可用替代": "评分数 / 评分 数值列", "能否替代 1–3 星评论归类": "不能",
                  "说明": "只有数量与均分, 无问题类型、频次、场景、严重度"},
                 {"可用替代": "五点描述", "能否替代 1–3 星评论归类": "不能",
                  "说明": "卖家自述卖点, 恰恰是差评要反驳的对象, 存在系统性偏差"},
                 {"可用替代": "Q&A 列", "能否替代 1–3 星评论归类": "不能",
                  "说明": "非结构化且本类目无有效样本"},
                 {"可用替代": "变体抓取的 title/bullets/images", "能否替代 1–3 星评论归类": "不能",
                  "说明": "同上, 属卖家侧内容"}],
                name="t_voc_sub", widths=[30, 24, 52])
        s.module("对本报告的影响(必须披露)")
        s.table(["影响项", "后果", "处理"],
                [{"影响项": "选品建议 · VOC问题价值(权重 15)", "后果": "该分项无数据, 不能打分",
                  "处理": "留空 → 按可用权重(85)归一化, 并在评分表与门槛处显式披露"},
                 {"影响项": "产品定义矩阵 · VOC对策", "后果": "无法给出针对差评的改进项",
                  "处理": "标 未提供; 不得用卖家话术填充"},
                 {"影响项": "风险与验证计划 · 用户侧风险", "后果": "缺少买家侧风险来源",
                  "处理": "仅列市场/合规/工程侧风险, 并标注 VOC 侧缺口"},
                 {"影响项": "洞察条数要求(≥6 条 VOC 洞察)", "后果": "无法满足 skill 契约的条数要求",
                  "处理": "本 sheet 明确声明不满足, 不计入洞察总数"}],
                name="t_voc_imp", widths=[30, 34, 46])
        s.note("补数建议: 抓取目标 ASIN 的 1–3 星评论 → LLM 归类(总览/功能性/体验感/场景) → 生成 VOC 工作簿后重跑本报告; 届时 VOC 权重恢复 15 分, 总分与标签可能变化。", warn=True)
        return s
    s.note("VOC 工作簿已接入归类解析: %s" % os.path.basename(voc.get("path") or ""))
    from . import voc_reader as VR
    ov = voc.get("overview") or {}
    au = voc.get("audit") or {}
    cnt = voc.get("counts") or {}
    s.module("一、数据源与口径")
    s.table(["项目", "值"], [
        {"项目": "工作簿", "值": os.path.basename(voc.get("path") or "")},
        {"项目": "去重后唯一评论", "值": ov.get("去重后唯一评论", "")},
        {"项目": "1–3★(负向) / 4–5★(正向)", "值": "%s / %s" % (ov.get("1–3★(负向)评论", ""),
                                                          ov.get("4–5★(正向)评论", ""))},
        {"项目": "星级分布", "值": ov.get("星级分布", "")},
        {"项目": "父ASIN 数", "值": ov.get("父ASIN 数", "")},
        {"项目": "抓取请求 / 页数 / 积分", "值": "%s / %s / %s" % (cnt.get("requests", 0),
                                                          cnt.get("pages", 0),
                                                          au.get("points", 0))},
        {"项目": "采集来源", "值": "、".join("%s×%d" % (k, v) for k, v in (au.get("sources") or {}).items())},
        {"项目": "3★中评(无明确负向问题)", "值": ov.get("3★中评(无明确负向问题)", "未披露")},
        {"项目": "标注失败", "值": ov.get("标注失败", "0 条")},
        {"项目": "安全证据条数", "值": ov.get("安全证据条数", "")},
    ], name="t_voc_src", widths=[28, 62])
    discount, discount_note = VR.coverage_discount(voc)
    s.note("覆盖折扣(参与 VOC问题价值 计算): " + discount_note)

    s.module("二、差评问题优先级(负向优先级分 = 20×(0.25F+0.30S+0.25H+0.10T+0.10C))")
    issues = voc.get("issues") or []
    if issues:
        issue_head = s.r                      # 表头行(图表引用需要)
        top_issues = issues[:10]
        s.table(["优先级", "问题", "所属属性", "负向条数", "问题占比", "S", "安全", "代表评论ID", "证据摘录"],
                [{"优先级": i.get("优先级"), "问题": i.get("问题"), "所属属性": i.get("所属属性"),
                  "负向条数": i.get("负向评论数"),
                  "问题占比": "%.1f%%" % (100 * float(i.get("问题占比") or 0)),
                  "S": i.get("S"), "安全": i.get("安全等级") or "无",
                  "代表评论ID": i.get("代表评论ID"), "证据摘录": (i.get("证据摘录") or "")[:90]}
                 for i in top_issues], name="t_voc_issues", widths=[8, 22, 18, 10, 10, 5, 8, 30, 46])
        s.note("A/B 级安全证据不得低于 P1; 仅 D 级(传闻)不得单独产生 P0/P1(skill 硬性规则)。")
        # 图表 1: 差评问题 Top10 条数(横向条形)
        cats = Reference(s.ws, min_col=2, min_row=issue_head + 1, max_row=issue_head + len(top_issues))
        data = Reference(s.ws, min_col=4, min_row=issue_head, max_row=issue_head + len(top_issues))
        s.add(_bar("差评问题 Top%d(条数)" % len(top_issues), cats, data, s, horizontal=True),
              anchor="A%d" % s.r, w=18, h=9)
        s.blank(19)
        # 图表 2: 优先级分布(环形) —— 用一块两列小表作为数据源
        dist = collections.Counter(i.get("优先级") or "P3" for i in issues)
        dist_rows = [{"优先级": k, "问题数": dist[k]} for k in ("P0", "P1", "P2", "P3") if dist.get(k)]
        s.table(["优先级", "问题数"], dist_rows, name="t_voc_dist", widths=[10, 10])
        if dist_rows:
            s.add(_pie("问题优先级分布(P0–P3)", None, None,
                       (s.r - len(dist_rows) - 1, s.r - 2), s), anchor="A%d" % s.r, w=14, h=8)
            s.blank(17)
    else:
        s.note("工作簿内无差评问题行", warn=True)

    s.module("三、好评价值驱动(正向驱动分 = 20×(0.30P+0.20B+0.20R+0.20V+0.10C))")
    drivers = voc.get("drivers") or []
    if drivers:
        s.table(["属性", "正向条数", "正向驱动分", "解读", "代表评论ID", "证据摘录"],
                [{"属性": d.get("属性"), "正向条数": d.get("正向评论数"),
                  "正向驱动分": d.get("正向驱动分"), "解读": d.get("解读"),
                  "代表评论ID": d.get("代表评论ID"), "证据摘录": (d.get("证据摘录") or "")[:80]}
                 for d in drivers[:15]], name="t_voc_drivers", widths=[20, 10, 12, 20, 30, 44])

    s.module("四、正负属性对照(象限: 稳定优势 / 争议属性 / 明显短板 / 低感知)")
    matrix = voc.get("matrix") or []
    if matrix:
        s.table(["属性", "证据合计", "正向提及率", "负向问题率", "象限"],
                [{"属性": m.get("属性"), "证据合计": m.get("证据合计"),
                  "正向提及率": "%.1f%%" % (100 * float(m.get("正向提及率") or 0)),
                  "负向问题率": "%.1f%%" % (100 * float(m.get("负向问题率") or 0)),
                  "象限": m.get("象限")} for m in matrix], name="t_voc_matrix", widths=[20, 10, 12, 12, 30])

    s.module("五、Listing 宣称风险(宣称 vs 评论证据)")
    claims = voc.get("claims") or []
    if claims:
        s.table(["宣称方向", "对应属性", "负向证据条数", "风险等级"],
                [{"宣称方向": c.get("宣称方向"), "对应属性": c.get("对应属性"),
                  "负向证据条数": c.get("负向证据条数"), "风险等级": c.get("风险等级")} for c in claims],
                name="t_voc_claims", widths=[22, 22, 14, 10])

    s.module("六、行动项(修复 / 保留)")
    for line in VR.voc_plan_lines(voc):
        s.note(line)

    s.module("七、局限与披露")
    for lim in (voc.get("limitations") or []):
        s.note(lim)
    s.note("「VOC问题价值(15 分)」= 100 × Σ(优先级权重 × 频次 × 严重度 × 安全加成 × 方案相关性) / Σ权重 × 覆盖折扣; "
           "仅「产品定义能解决」的问题计入价值, 客服/物流/二手/降价类相关性上限 0.2。")
    return s


# ---------------------------------------------------------------- Sheet4
def sheet_selection(wb, S):
    s = Sheet(wb, "选品建议", "选品建议",
              "决策系统: 机会假设(数量不固定) → 市场机会分(6 分项) → 市场机会门槛 → 标签级联(硬否决 → 置信度封顶 → 总分带 → 分项下限) → 过门槛者进入产品定义。")
    ex = S["executable"]
    obs = S["observe"]
    nog = S["nogo"]
    s.module("一、选品结论摘要")
    if ex:
        s.note("可执行机会 %d 个: %s" % (len(ex), "、".join(x["假设"] for x in ex)))
    else:
        s.note("暂不建议进入: 无任何假设通过市场机会门槛。决定性阻塞项与重估条件见第六节。", warn=True)
    s.note("可执行机会(过门槛的 优先开发/小批验证) %d 个: %s; 观察机会 %d 个; 不建议进入 %d 个。"
           "VOC 源未提供 → 「VOC问题价值」分项留空, 总分按可用权重(85/100)归一化, 门槛的「关键词需求或VOC≥60」以关键词需求判定。" % (
               len(ex), "、".join("%s(%s)" % (x["假设"], x["标签"]) for x in ex) or "无",
               len([r for r in S["score_rows"] if r["标签"] == "持续观察"]),
               len([r for r in S["score_rows"] if r["标签"] == "不建议进入"])), warn=not ex)

    s.module("二、机会白地矩阵(价格带 × 目标场景 × 配置)")
    s.table(["假设", "目标价格段", "价格带销额占比", "带内品牌数", "带宽", "同配置占用行数",
             "占用父体", "占用品牌", "关键词命中", "证据完整度", "VOC相关性"],
            S["whitespace"], name="t_white",
            pct_cols=("价格带销额占比",), widths=[30, 14, 13, 10, 10, 13, 9, 9, 26, 11, 26])

    s.module("三、候选产品方案评分(市场机会分 100 分; VOC 缺失 → 按可用权重归一化)")
    hdr = ["假设", "关键词需求(20)", "价格带吸引力(20)", "竞争余量(15)", "配置空白/差异化(20)",
           "VOC问题价值(15)", "证据置信度(10)", "可用权重", "加权得分(原始)", "总分(归一)", "排名", "标签"]
    s.table(hdr, S["score_rows"], name="t_score", widths=[30, 14, 15, 12, 18, 14, 13, 9, 12, 11, 6, 12])
    s.note("分项计算规则(可复现):")
    for name, rule in S["rules"].items():
        s.note("· %s: %s" % (name, rule))
    s.note("分项明细: " + " || ".join(
        "%s → %s" % (r["假设"], "; ".join("%s=%s" % (k, r.get(k)) for k in (
            "关键词需求(20)", "价格带吸引力(20)", "竞争余量(15)",
            "配置空白/差异化(20)", "VOC问题价值(15)", "证据置信度(10)")))
        for r in S["score_rows"]))
    s.note("判定说明: " + " || ".join("%s → %s" % (r["假设"], r.get("判定")) for r in S["score_rows"]))
    s.note("门槛(默认): 关键词需求≥60; 价格带吸引力≥50; 竞争余量≥40 或 配置空白≥60; 证据置信度≥50; 总分≥60; 无硬否决。"
           "VOC 分项不可用时, 「关键词需求或VOC≥60」以关键词需求判定。")

    s.module("四、产品定义矩阵(仅可执行机会)")
    if ex:
        s.table(["假设", "目标定价", "人群/场景", "核心任务", "产品类型", "材质", "套装结构",
                 "目标规格", "基准配置", "差异化配置", "刻意排除的功能", "VOC对策", "验证测试",
                 "claim边界", "证据置信度", "停止/重估条件"],
                ex, name="t_def", widths=[26, 14, 26, 26, 14, 18, 10, 26, 28, 30, 24, 18, 30, 28, 11, 30])
    else:
        s.note("无假设通过门槛 → 不输出产品定义(禁止为凑数编造产品定义)。", warn=True)

    s.module("五、风险与验证计划")
    s.table(["假设", "风险", "证据", "严重度", "验证方式", "通过标准", "依赖该测试的决策"],
            S["risks"], name="t_risk", widths=[24, 30, 34, 9, 30, 30, 30])

    s.module("六、不建议方向与停止条件")
    s.table(["假设", "决定性原因", "重估条件"], nog, name="t_nogo", widths=[30, 60, 46])

    s.module("七、Listing 与上市验证")
    s.table(["要素", "建议", "依据/待验证"],
            [{"要素": "标题核心词", "建议": S["listing"]["title_kw"], "依据/待验证": "关键词分析 sheet Top1 词"},
             {"要素": "主图/视频验证", "建议": S["listing"]["media"], "依据/待验证": "待 A/B 验证"},
             {"要素": "广告结构", "建议": S["listing"]["ppc"], "依据/待验证": "CPC 来自关键词工作簿"},
             {"要素": "claim 边界", "建议": "只声明可验证事实(材质/尺寸/厚度/认证); 不得声明抑菌率、寿命等无依据指标", "依据/待验证": "合规"}],
            name="t_list", widths=[18, 70, 30])
    return s


# ---------------------------------------------------------------- Sheet5/6
def sheet_evidence(wb, ev):
    s = Sheet(wb, "证据与溯源", "证据与溯源(值级四件套)",
              "每个进入结论的数字必须能回答四件事: 来源 / 口径 / 单位 / 支撑度(行数·父体数·品牌数)。")
    s.note("用法: 报告内任何结论引用某数值时, 应能回指到本表 evidence_id; 找不到对应行的数字视为不可追溯。")
    s.table(["evidence_id", "模块", "指标", "值", "来源", "口径", "单位", "行数", "父体数", "品牌数", "备注"],
            ev.rows, name="t_ev", widths=[11, 22, 30, 26, 30, 52, 7, 7, 8, 8, 34])
    return s


def sheet_qa(wb, checks):
    s = Sheet(wb, "QA自检", "QA自检清单(制品级 + 论断级)",
              "制品级 = skill qa-checklist 的结构/数据完整性项(可自动判定); 论断级 = 本项目 18 类追问(逐条给判定或说明)。")
    s.module("一、制品级(自动判定)")
    s.table(["检查项", "判定", "说明"], checks["artifact"], name="t_qa1",
            widths=[52, 10, 52])
    s.module("二、论断级(18 类追问)")
    s.table(["#", "追问类型", "判定", "说明"], checks["claim"], name="t_qa2",
            widths=[4, 34, 12, 62])
    s.module("三、本批未通过 / 需注意")
    bad = [c for c in checks["artifact"] + [{"检查项": x[1], "判定": x[2], "说明": x[3]} for x in checks["claim"]]
           if c["判定"] not in ("通过", "不适用")]
    s.table(["检查项", "判定", "说明"],
            bad or [{"检查项": "无", "判定": "通过", "说明": "全部通过"}], name="t_qa3",
            widths=[52, 10, 52])
    return s
