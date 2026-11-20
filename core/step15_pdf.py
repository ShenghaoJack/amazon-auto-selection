"""Render a compact Step 15 PDF from the same review snapshot as the UI."""
from __future__ import annotations

from html import escape
from decimal import Decimal
import re


def _text(value):
    return escape(str(value if value is not None and value != "" else "—"))


def _amount(value, unit):
    number = Decimal(str(value))
    divisor, suffix = (Decimal(1_000_000_000), "B") if abs(number) >= 1_000_000_000 else (Decimal(1_000_000), "M")
    return f"{unit.split('/')[0] or '币种未知'} {number / divisor:.1f}{suffix}/月"


def _actual(row):
    value, unit, name = row.get("实际值"), str(row.get("单位") or ""), row.get("指标")
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if name == "类目GMV估算":
            return _amount(value, unit)
        if name == "平均客单价":
            return f"{unit or '币种未知'} {value:.2f}"
        if name in ("月需求量估算", "评论壁垒"):
            value = f"{value:,.0f}"
        elif unit == "%" or name in ("评分", "季节波动系数"):
            value = f"{value:+.1f}" if name in ("趋势方向（站内）", "趋势方向（Google）") and value >= 0 else f"{value:.1f}"
        else:
            value = str(value)
        return str(value) + (unit if unit == "%" else f" {unit}" if unit else "")
    return str(value)


def render_html(cat, data):
    from tools.step15_indicator_assessment import BUSINESS_MEANINGS
    rows = data.get("指标") or []
    advice = data.get("建议") or []
    unit = next((str(r.get("单位") or "").split("/")[0] for r in rows if r.get("指标") == "类目GMV估算"), "")
    def money(value):
        text = str(value or "")
        def scaled(match):
            amount = Decimal(match.group(1)) * {"万": Decimal(10000), "百万": Decimal(1000000), "千万": Decimal(10000000)}[match.group(2)]
            return _amount(amount, "USD/月").removesuffix("/月")
        text = re.sub(r"(\d+(?:\.\d+)?)\s*(百万|千万|万)\s*美元", scaled, text)
        text = re.sub(r"(?<![A-Za-z])\$\s*(\d+(?:\.\d+)?)", lambda m: f"{unit} {m.group(1)}" if unit else m.group(), text)
        return re.sub(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:美元|美金)", r"USD \1", text)
    parts = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><style>",
        "@page{size:A4;margin:12mm 13mm}body{font-family:'Microsoft YaHei',sans-serif;color:#202b3c;font-size:9pt;line-height:1.5}",
        "h1{font-size:17pt;margin:0 0 3mm}h2{font-size:12pt;margin:5mm 0 2mm;color:#263e69;break-after:avoid}h3{font-size:10pt;margin:3mm 0 1mm;break-after:avoid}",
        "p{margin:1mm 0 2mm;white-space:pre-wrap}ul{margin:1mm 0 2mm;padding-left:5mm}li{margin:0 0 1mm}table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:8pt}",
        "th,td{text-align:left;vertical-align:top;padding:1.4mm;border-bottom:1px solid #e3e8ef;overflow-wrap:anywhere}th{background:#edf2f8}thead{display:table-header-group}tr{break-inside:avoid}.note{color:#64748b;font-size:8pt}",
        "</style><h1>步骤15｜市场进入评估</h1><p class='note'>", _text(cat), " · 与页面同源的已保存报告</p>"]
    old_season = any(r.get("指标") == "季节波动系数" and "各词每年峰谷比" in str(r.get("原表标准") or "") for r in rows)
    legacy = data.get("report_outdated") or old_season or any("否决" in str(r.get("影响") or "") or "降级" in str(r.get("影响") or "") for r in rows)
    legacy = legacy or any(x.get("项目") == "规则初判" and any(word in str(x.get("内容") or "") for word in ("否决", "降级")) for x in advice)
    if legacy:
        parts.append("<p>当前为旧版指标报告，AI 建议基于旧规则，请重新运行步骤15生成新的建议。</p>")
    else:
        for key, title in (("AI建议", "能否进入"), ("主要依据", "主要依据：市场需求与竞争格局"), ("主要风险", "主要风险"), ("待验证", "下一步怎么办（待验证）")):
            values = [str(x.get("内容")) for x in advice if x.get("项目") == key and x.get("内容")]
            parts.append(f"<h2>{title}</h2>")
            if not values:
                parts.append("<p class='note'>暂无单独记录；旧报告可重新运行步骤15生成。</p>")
            elif key == "AI建议":
                parts.extend(f"<p>{_text(money(v))}</p>" for v in values)
            else:
                parts.append("<ul>" + "".join(f"<li>{_text(money(v))}</li>" for v in values) + "</ul>")
    parts.append("<h2>指标明细</h2>")
    for section in ("市场需求", "竞争格局"):
        subset = [r for r in rows if r.get("板块") == section]
        if not subset:
            continue
        parts.append(f"<h3>{section}</h3><table><colgroup><col style='width:23%'><col style='width:32%'><col style='width:30%'><col style='width:15%'></colgroup><thead><tr><th>指标</th><th>实际值</th><th>标准</th><th>判定</th></tr></thead><tbody>")
        for r in subset:
            name = "上架超3年链接销额占比" if r.get("指标") == "老品占比" else r.get("指标")
            parts.append("<tr>" + "".join(f"<td>{_text(v)}</td>" for v in (name, money(_actual(r)), money(r.get("原表标准")), r.get("判定"))) + "</tr>")
        parts.append("</tbody></table>")
    parts.append("<h2>附录｜趋势方向明细</h2>")
    for name in ("趋势方向（站内）", "趋势方向（Google）"):
        parts.append(f"<h3>{name}</h3>")
        detail = (data.get("趋势明细") or {}).get(name)
        if not detail:
            parts.append("<p class='note'>当前报告缺少可展示的完整趋势明细。</p>")
            continue
        parts.append("<table><thead><tr><th>核心词</th>" + "".join(f"<th>{_text(m)}同比</th>" for m in detail["months"]) + "</tr></thead><tbody>")
        for word in detail["words"]:
            parts.append("<tr><td>" + _text(word["name"]) + "</td>" + "".join(f"<td>{_text(v)}</td>" for v in word["values"]) + "</tr>")
        parts.append("<tr><td>各词加总</td>" + "".join(f"<td>{_text(v)}</td>" for v in detail["total"]) + "</tr></tbody></table>")
        parts.append(f"<p>汇总（3个月均值）：{_text(detail['mean'])}</p>")
        trend_row = next((r for r in rows if r.get("指标") == name), {})
        parts.append(f"<p class='note'>{_text(trend_row.get('证据与限制'))}</p>")
    parts.append("<h2>附录｜指标业务解释</h2>")
    for section in ("市场需求", "竞争格局"):
        subset = [r for r in rows if r.get("板块") == section]
        if not subset:
            continue
        parts.append(f"<h3>{section}</h3><table><colgroup><col style='width:24%'><col style='width:76%'></colgroup><thead><tr><th>指标</th><th>业务含义与计算口径</th></tr></thead><tbody>")
        for row in subset:
            key = row.get("指标")
            name = "上架超3年链接销额占比" if key == "老品占比" else key
            meaning = BUSINESS_MEANINGS.get(key, "暂无业务解释，请参考报告中的计算口径。")
            formula = row.get("原表备注及口径")
            description = _text(meaning)
            if formula:
                description += "<br>计算口径：" + _text(formula)
            parts.append(f"<tr><td>{_text(name)}</td><td>{description}</td></tr>")
        parts.append("</tbody></table>")
    return "".join(parts) + "</html>"


def render_pdf(cat, data):
    from playwright.sync_api import sync_playwright
    from core.browser import find_chrome_path
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=find_chrome_path(), headless=True)
        try:
            page = browser.new_page()
            page.set_content(render_html(cat, data), wait_until="load")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()
