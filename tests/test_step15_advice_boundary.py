# -*- coding: utf-8 -*-
"""步骤15 市场进入评估（4大业务维度×相对偏离度）边界判定与稳定输出校验测试。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools.step15_indicator_assessment import (
    _summarize_assessment,
    advice,
    build_advice_prompt,
    summarize_four_dimensions,
)


def _row(section: str, name: str, val, status: str, std: str = "") -> dict:
    return {
        "板块": section,
        "指标": name,
        "原表标准": std,
        "实际值": val,
        "判定": status,
        "原表备注及口径": "",
    }


def _ice_cream_rows() -> list[dict]:
    """Ice Cream Machines-US-202610（B重度+C重度 → 暂不建议进入）。"""
    return [
        _row("市场需求", "核心词搜索量", "ice cream maker: 287220", "达标", "> 30000 次/月"),
        _row("市场需求", "核心词数量", 5, "达标", "> 2 个"),
        _row("市场需求", "CPC竞价", "最高 $1.92", "达标", "< 3 美元"),
        _row("市场需求", "平均客单价", 225.11, "达标", "> 35 美元"),
        _row("市场需求", "月需求量估算", 251.44, "不达标", "≥ 500 件/变体/月"),
        _row("市场需求", "类目GMV估算", 20200000.0, "达标", "≥ 100000 美元/月"),
        _row("市场需求", "趋势方向（站内）", 7.3, "达标", "> -20%"),
        _row("市场需求", "趋势方向（Google）", 3.0, "达标", "> -20%"),
        _row("市场需求", "旺季月份", "无", "仅展示", "仅展示"),
        _row("市场需求", "淡季月份", "无", "仅展示", "仅展示"),
        _row("市场需求", "季节波动系数", 2.3, "达标", "各词合计每年峰谷比 ≤ 2.5"),
        _row("竞争格局", "头部链接销售额占比", 45.1, "达标", "< 60%"),
        _row("竞争格局", "头部品牌集中度", 82.1, "不达标", "< 60%"),
        _row("竞争格局", "卖家集中度", 88.2, "不达标", "< 60%"),
        _row("竞争格局", "新卖家占比", "83 个卖家；新品销额占比 33.0%", "达标", "新品卖家数 > 10 或新品销额占比 > 20%"),
        _row("竞争格局", "大卖占比", 71.0, "不达标", "< 50%"),
        _row("竞争格局", "VC卖家占比", 83.4, "不达标", "< 20%"),
        _row("竞争格局", "本土卖家占比", 23.5, "不达标", "< 20%"),
        _row("竞争格局", "评论壁垒", 8494, "不达标", "< 5000 条评分"),
        _row("竞争格局", "评分", 4.2, "达标", "≥ 4.2 分"),
        _row("竞争格局", "老品占比", 35.2, "达标", "< 70%"),
    ]


def _dehumidifier_rows() -> list[dict]:
    """Dehumidifiers-US-202609（VC 51.3% ≥50%重度控盘 + 评论11275重度壁垒 → 暂不建议进入）。"""
    return [
        _row("市场需求", "核心词搜索量", "dehumidifier for bathroom: 78617", "达标", "> 30000 次/月"),
        _row("市场需求", "核心词数量", 4, "达标", "> 2 个"),
        _row("市场需求", "CPC竞价", "最高 $2.48", "达标", "< 3 美元"),
        _row("市场需求", "平均客单价", 207.11, "达标", "> 35 美元"),
        _row("市场需求", "月需求量估算", 797.0, "达标", "≥ 500 件/变体/月"),
        _row("市场需求", "类目GMV估算", 21700000.0, "达标", "≥ 100000 美元/月"),
        _row("市场需求", "趋势方向（站内）", 5.4, "达标", "> -20%"),
        _row("市场需求", "趋势方向（Google）", 10.6, "达标", "> -20%"),
        _row("市场需求", "旺季月份", "无", "仅展示", "仅展示"),
        _row("市场需求", "淡季月份", "无", "仅展示", "仅展示"),
        _row("市场需求", "季节波动系数", 2.0, "达标", "各词合计每年峰谷比 ≤ 2.5"),
        _row("竞争格局", "头部链接销售额占比", 12.4, "达标", "< 60%"),
        _row("竞争格局", "头部品牌集中度", 42.9, "达标", "< 60%"),
        _row("竞争格局", "卖家集中度", 63.3, "不达标", "< 60%"),
        _row("竞争格局", "新卖家占比", "64 个卖家；新品销额占比 16.1%", "达标", "新品卖家数 > 10 或新品销额占比 > 20%"),
        _row("竞争格局", "大卖占比", 58.5, "不达标", "< 50%"),
        _row("竞争格局", "VC卖家占比", 51.3, "不达标", "< 20%"),
        _row("竞争格局", "本土卖家占比", 36.5, "不达标", "< 20%"),
        _row("竞争格局", "评论壁垒", 11275, "不达标", "< 5000 条评分"),
        _row("竞争格局", "评分", 4.4, "达标", "≥ 4.2 分"),
        _row("竞争格局", "老品占比", 38.3, "达标", "< 70%"),
    ]


def _conditional_middle_rows() -> list[dict]:
    """典型中间态（VC 38% 中度 + 评论 7200 中度 + 新品窗口达标 → 有条件进入）。"""
    rows = _dehumidifier_rows()
    for r in rows:
        if r["指标"] == "VC卖家占比":
            r["实际值"] = 38.0
        elif r["指标"] == "评论壁垒":
            r["实际值"] = 7200
    return rows


def _single_heavy_with_window_rows() -> list[dict]:
    """单维重度但无评价壁垒 + 有新品窗口 → 有条件进入。"""
    rows = _dehumidifier_rows()
    for r in rows:
        if r["指标"] == "VC卖家占比":
            r["实际值"] = 55.0
        elif r["指标"] in ("大卖占比", "评论壁垒"):
            r["实际值"] = 25.0 if r["指标"] == "大卖占比" else 2000
            r["判定"] = "达标"
    return rows


def _healthy_rows() -> list[dict]:
    """需求健康、垄断最多轻度、壁垒无压力、本土超标仅作辅助提醒 → 可进入。"""
    rows = _dehumidifier_rows()
    for r in rows:
        if r["指标"] == "卖家集中度":
            r["实际值"] = 62.0  # 轻度超标
        elif r["指标"] in ("大卖占比", "VC卖家占比", "评论壁垒"):
            r["实际值"] = 10.0 if r["指标"] != "评论壁垒" else 1800
            r["判定"] = "达标"
    return rows


def test_four_dimension_boundaries_cover_all_tiers_and_vc50_redline():
    ice = _summarize_assessment(_ice_cream_rows())
    assert ice["判定边界参考"]["参考档位"] == "暂不建议进入"
    assert ice["四维度评估"]["B_垄断与自营控盘"]["评级"] == "重度"
    assert ice["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "重度"
    severe_blob = " ".join(ice["突出不达标项"])
    assert "VC/AMZ近似自营极端控盘(83.4%" in severe_blob
    assert "头部品牌集中度重度集中(82.1%" in severe_blob
    assert "卖家集中度极端垄断(88.2%" in severe_blob

    # 按新业务红线：VC 51.3% >= 50% 重度控盘 + 评论壁垒 11275 重度壁垒 -> 暂不建议进入
    deh = _summarize_assessment(_dehumidifier_rows())
    assert deh["四维度评估"]["B_垄断与自营控盘"]["评级"] == "重度"
    assert deh["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "重度"
    assert deh["判定边界参考"]["参考档位"] == "暂不建议进入"
    assert any("51.3%" in s for s in deh["突出不达标项"])

    # 中间态（B中度 + C中度 + 有新品窗口）-> 低风险条件进入
    mid = _summarize_assessment(_conditional_middle_rows())
    assert mid["四维度评估"]["B_垄断与自营控盘"]["评级"] == "中度"
    assert mid["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "中度"
    assert mid["判定边界参考"]["参考档位"] == "低风险条件进入"

    # 单维重度（VC 55%）但评价无压力 + 有新品窗口 -> 高风险条件进入
    single = _summarize_assessment(_single_heavy_with_window_rows())
    assert single["四维度评估"]["B_垄断与自营控盘"]["评级"] == "重度"
    assert single["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "无明显压力"
    assert single["判定边界参考"]["参考档位"] == "高风险条件进入"

    # 健康类目（含本土卖家 36.5% 辅助提醒 + 卖家集中度 62% 轻度超标）-> 可进入
    ok = _summarize_assessment(_healthy_rows())
    assert ok["四维度评估"]["B_垄断与自营控盘"]["评级"] == "轻度"
    assert ok["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "无明显压力"
    assert "本土卖家占比36.5%" in ok["四维度评估"]["D_新品窗口与本土身份"]["本土身份提醒"]
    assert ok["判定边界参考"]["参考档位"] == "可进入"

    missing = _summarize_assessment([_row("市场需求", "核心词数量", "", "数据不足")] * 6)
    assert missing["判定边界参考"]["参考档位"] == "数据不足无法判断"

    # Hair Dryers 红海边界：VC 48.2%(>=40%) + 卖家集中度 66.5% -> B重度，叠加评论壁垒 27026(5.4x) + 弱新品窗口(12.6%) -> 暂不建议进入
    hair_dryers = _dehumidifier_rows()
    for r in hair_dryers:
        if r["指标"] == "月需求量估算":
            r["实际值"], r["判定"] = 458.0, "不达标"
        elif r["指标"] == "卖家集中度":
            r["实际值"], r["判定"] = 66.5, "不达标"
        elif r["指标"] == "新卖家占比":
            r["实际值"], r["判定"] = "113 个卖家；新品销额占比 12.6%", "达标"
        elif r["指标"] == "大卖占比":
            r["实际值"], r["判定"] = 60.3, "不达标"
        elif r["指标"] == "VC卖家占比":
            r["实际值"], r["判定"] = 48.2, "不达标"
        elif r["指标"] == "评论壁垒":
            r["实际值"], r["判定"] = 27026, "不达标"
    hd = _summarize_assessment(hair_dryers)
    assert hd["四维度评估"]["B_垄断与自营控盘"]["评级"] == "重度"
    assert hd["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "重度"
    assert hd["四维度评估"]["D_新品窗口与本土身份"]["新品窗口"] == "弱窗口"
    assert hd["判定边界参考"]["参考档位"] == "暂不建议进入"
    assert any("评论壁垒极端高企(27026条≥20000)" in s for s in hd["突出不达标项"])

    # 维度 B 极端控盘（VC 72%）结合维度 C：叠加 C 中度(7200) -> 暂不建议进入；若 C 无压力且有窗口 -> 高风险条件进入
    ext_b_mid_c = _conditional_middle_rows()
    for r in ext_b_mid_c:
        if r["指标"] == "VC卖家占比":
            r["实际值"] = 72.0
    res_bm = _summarize_assessment(ext_b_mid_c)
    assert res_bm["判定边界参考"]["参考档位"] == "暂不建议进入"

    ext_b_no_c = _single_heavy_with_window_rows()
    for r in ext_b_no_c:
        if r["指标"] == "VC卖家占比":
            r["实际值"] = 72.0
    res_bn = _summarize_assessment(ext_b_no_c)
    assert res_bn["四维度评估"]["C_评价与冷启动壁垒"]["评级"] == "无明显压力"
    assert res_bn["判定边界参考"]["参考档位"] == "高风险条件进入"


def test_prompt_json_structure_and_bare_label_rejection():
    ctx = SimpleNamespace(
        config={"CAT": "Dehumidifiers-US-202609", "SITE": "美国", "TRAFFIC_MONTH": "2026-09",
                "COMPETITOR_MONTH": "2026-09", "TREND_MONTH": "202609"},
        dry_run=False,
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    rows = _dehumidifier_rows()
    prompt = build_advice_prompt(ctx, rows)
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["判定边界参考"]["参考档位"] == "暂不建议进入"
    assert payload["四维度评估"]["B_垄断与自营控盘"]["评级"] == "重度"
    assert any("51.3%" in s for s in payload["突出不达标项"])

    replies = iter([
        {"建议": "有条件进入", "主要依据": [], "主要风险": [], "待验证": []},
        {
            "建议": "暂不建议进入：需求端9/9达标但竞争端VC/AMZ占比51.3%已构成重度控盘，且评论壁垒11275条叠加本土卖家36.5%，双维重度封锁下冷启动成本过高。",
            "主要依据": ["市场需求：...", "竞争格局：..."],
            "主要风险": ["自营重度控盘：VC占比51.3%超过50%红线。"],
            "待验证": ["核实AMZ自营价格带分布。"],
        },
    ])
    ctx.llm = lambda: SimpleNamespace(chat_json=lambda *a, **k: next(replies))
    out = advice(ctx, rows)
    ai_row = next(r for r in out if r["项目"] == "AI建议")
    assert ai_row["内容"].startswith("暂不建议进入：")
    assert len(ai_row["内容"]) > 40


def test_pdf_renders_four_dimension_breakdown_table():
    from core.step15_pdf import render_html

    rows = _ice_cream_rows()
    four_dim = summarize_four_dimensions(rows)
    assert four_dim["参考档位"] == "暂不建议进入"
    assert [d["维度"] for d in four_dim["维度明细"]] == [
        "A. 需求底盘",
        "B. 垄断与自营控盘",
        "C. 评价与冷启动壁垒",
        "D. 新品窗口与本土身份",
    ]
    assert four_dim["维度明细"][1]["评级"] == "极端"

    html = render_html("Ice_Cream_Machines-US-202610", {
        "指标": rows,
        "建议": [{"项目": "AI建议", "内容": "暂不建议进入：B与C双维重度封锁。"}],
        "趋势明细": {},
    })
    assert "四维度判定拆解" in html
    assert "A. 需求底盘" in html
    assert "B. 垄断与自营控盘" in html
    assert "C. 评价与冷启动壁垒" in html
    assert "D. 新品窗口与本土身份" in html
    assert "<td>极端</td>" in html
    assert "规则判定参考：暂不建议进入" in html

    from pathlib import Path
    webui_html = Path("webui/index.html").read_text(encoding="utf-8")
    assert 'data["四维度拆解"]' in webui_html
    assert "四维度判定拆解" in webui_html
    assert "规则判定参考：" in webui_html
