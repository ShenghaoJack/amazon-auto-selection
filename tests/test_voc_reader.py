# -*- coding: utf-8 -*-
"""VOC 工作簿读取 + VOC问题价值(15 分) 计算测试。"""
import os

import openpyxl
import pytest

from tools.cat_report import voc_reader as VR

OVERVIEW = [("项目", "值"), ("父ASIN 数", "10"), ("去重后唯一评论", "498"),
            ("1–3★(负向)评论", "300"), ("4–5★(正向)评论", "198"),
            ("星级分布", "1★:166、2★:71、3★:63、4★:100、5★:98"),
            ("抓取请求 / 页数 / 积分", "55 / 85 / 425"),
            ("3★中评(无明确负向问题)", "12 条(占负向 300 条的 4%)"),
            ("标注失败", "0 条(占 0.0%)"), ("安全证据条数", "29")]
AUDIT = [("来源", "父ASIN", "实际查询ASIN", "是否子体", "星级层", "请求页数", "返回条数",
          "消耗积分", "状态", "调用时间", "错误"),
         ("pangolinfo", "B063C59EEB", "B01D698E86", "是", "critical", 3, 30, 15, "ok", "2026-10-09 09:30", ""),
         ("pangolinfo", "B063C59EEB", "B01D698E86", "是", "four_star", 1, 10, 5, "ok", "2026-10-09 09:31", ""),
         (),
         ("父ASIN/查询ASIN", "该ASIN评论总数(totalReviews)", "本次抓取条数", "覆盖率"),
         ("B01D698E86", 332, 40, "12.0%")]
ISSUES = [("问题", "评论数", "所属属性", "负向评论数", "问题占比", "F", "S", "H", "T", "C",
           "负向优先级分", "优先级", "安全等级", "代表评论ID", "证据摘录", "根因假设"),
          ("净化效果不足", 53, "核心效果/净化能力", 53, 0.1767, 4, 3, 3, 3, 5, 66.0, "P1", "无",
           "R3OTW547YV1FD2、R1QFIT0HY2HFF7", "not effective", "需人工确认"),
          ("整机停止工作/损坏/寿命短", 44, "可靠性/耐用性", 44, 0.1467, 4, 4, 3, 3, 5, 72.0, "P1", "无",
           "RAMQEG0KQRT8", "stopped working", "需人工确认"),
          ("客服售后差", 6, "客服/退货/保修", 6, 0.02, 2, 3, 1, 3, 2, 34.0, "P3", "无",
           "R2XUH010Q41B34", "no reply", "需人工确认")]
DRIVERS = [("属性", "评论数", "正向评论数", "正向提及率", "P", "B", "R", "V", "C", "正向驱动分",
            "解读", "证据角色分布", "代表评论ID", "证据摘录"),
           ("核心效果/净化能力", 77, 77, 0.39, 5, 5, 2, 3, 5, 80.0, "核心差异点—保留并放大",
            "experienced_benefit×60", "R4ZXYKFGR7COG", "works great")]
MATRIX = [("属性", "评论数", "正向评论数", "负向评论数", "证据合计", "正向提及率", "负向问题率",
           "证据平衡(pp)", "象限", "代表评论ID"),
          ("核心效果/净化能力", 130, 77, 53, 130, 0.39, 0.18, 21.0, "争议属性—分场景优化", "R3OTW547YV1FD2")]
CLAIMS = [("Listing 宣称方向", "评论数", "对应属性", "负向证据条数", "风险等级", "建议表述调整"),
          ("静音/低噪", 28, "噪音/舒适/环境影响", 28, "高", "补充档位数据")]
EVIDENCE = [("评论ID", "星级", "完整正文"), ("R3OTW547YV1FD2", 1, "Turned moldy"),
            ("R1QFIT0HY2HFF7", 2, "stopped working"), ("RAMQEG0KQRT8", 1, "no power"),
            ("R2XUH010Q41B34", 3, "ok"), ("R4ZXYKFGR7COG", 5, "works great")]


def _mk_workbook(path, with_issues=True):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in (("VOC分析_总览", OVERVIEW), ("数据质量_抓取审计", AUDIT),
                       ("好评价值驱动", DRIVERS), ("正负属性对照", MATRIX),
                       ("Listing卖点_风险", CLAIMS), ("评论证据库", EVIDENCE),
                       ("差评问题分析", ISSUES if with_issues else ISSUES[:1])):
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(list(r))
    wb.save(path)
    return path


def test_note_rows_before_header_are_not_parsed_as_data(ws_tmp):
    """表头之前的说明行(口径提示)不得被当成数据(2026-10-09 E2E 发现的 bug)。"""
    path = os.path.join(str(ws_tmp), "note-VOC.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("差评问题分析")
    ws.append(["负向优先级分 = 20×(0.25F+0.30S+0.25H+0.10T+0.10C)"])
    ws.append(["数据口径: 样本为页数上限内评论合集"])
    ws.append(["问题", "评论数", "所属属性", "负向评论数", "问题占比", "F", "S", "H", "T", "C",
               "负向优先级分", "优先级", "安全等级", "代表评论ID", "证据摘录"])
    ws.append(["噪音过大", 12, "噪音/舒适/环境影响", 12, 0.1, 3, 3, 1, 3, 4, 55.0, "P2", "无",
               "R1QFIT0HY2HFF7", "too loud"])
    wb.save(path)
    parsed = VR.read_voc_workbook(path)
    assert parsed["available"] is True
    assert [i["问题"] for i in parsed["issues"]] == ["噪音过大"], "说明行不得进入问题列表"


@pytest.fixture
def voc_book(ws_tmp):
    return VR.read_voc_workbook(_mk_workbook(os.path.join(str(ws_tmp), "cat-VOC报告.xlsx")))


def test_parse_workbook(voc_book):
    assert voc_book["available"] is True
    assert voc_book["counts"]["issues"] == 3
    assert voc_book["counts"]["drivers"] == 1
    assert voc_book["evidence_ids"] == {"R3OTW547YV1FD2", "R1QFIT0HY2HFF7", "RAMQEG0KQRT8",
                                        "R2XUH010Q41B34", "R4ZXYKFGR7COG"}
    assert (voc_book["audit"]["requests"], voc_book["audit"]["pages"],
            voc_book["audit"]["points"]) == (2, 4, 20)
    assert voc_book["audit"]["coverage"][0]["total"] == 332
    assert voc_book["overview"]["去重后唯一评论"] == "498"
    assert "页数上限" in "".join(voc_book["limitations"])


def test_missing_workbook_is_not_zero(ws_tmp):
    empty = VR.read_voc_workbook(os.path.join(str(ws_tmp), "不存在.xlsx"))
    assert empty["available"] is False
    score, note = VR.voc_issue_value(empty, {"名称": "x"})
    assert score is None, "VOC 缺失必须留空, 不允许按 0 计"
    assert "未提供" in note


def test_solution_relevance_caps_non_product_issues():
    rel_cs, why = VR.solution_relevance("客服售后差", {"差异化配置": "质保 2 年"})
    assert rel_cs <= 0.2, "与产品定义无关的问题(客服/物流/二手/降价)相关性上限 0.2"
    rel_cs2, _ = VR.solution_relevance("退货退款困难", {"差异化配置": "便利退货"})
    assert rel_cs2 <= 0.2
    rel_noise, _ = VR.solution_relevance("噪音/舒适/环境影响", {"差异化配置": "睡眠档 22dB 静音"})
    assert rel_noise == 1.0
    rel_miss, why2 = VR.solution_relevance("噪音/舒适/环境影响", {"差异化配置": "加大水箱"})
    assert rel_miss < 1.0 and "未命中" in why2


def test_issue_value_is_higher_for_solvable_hypothesis(voc_book):
    good = {"名称": "静音宠物净化器", "差异化配置": "睡眠档 22dB 静音 + H13 HEPA + 活性炭除味",
            "目标规格": "CADR 200 CFM"}
    bad = {"名称": "基础款", "差异化配置": "沿用公模, 不做额外设计", "目标规格": "常规配置"}
    s_good, note_good = VR.voc_issue_value(voc_book, good)
    s_bad, _ = VR.voc_issue_value(voc_book, bad)
    assert 0 <= s_bad < s_good <= 100, "可解决性高的方案应得到更高的 VOC问题价值"
    assert "方案相关性" in note_good and "覆盖折扣" in note_good


def test_coverage_discount_scales_with_evidence(voc_book):
    d, note = VR.coverage_discount(voc_book)
    assert 0.5 <= d <= 1.0 and "覆盖折扣" in note
    small = {"counts": {"evidence": 10}, "audit": {"coverage": [{"rate": "2.0%"}]}}
    d_small, _ = VR.coverage_discount(small)
    assert d_small < d, "证据少 + 覆盖率低 → 折扣更小"


def test_plan_lines_prioritise_issues(voc_book):
    lines = VR.voc_plan_lines(voc_book)
    assert lines and lines[0].startswith("修复:") and "P1" in lines[0]
    assert any(x.startswith("保留/强化:") for x in lines)


def test_scoring_note_documents_formula():
    from tools.cat_report import scoring as SC
    note = SC.RULE_NOTES["VOC问题价值"]
    assert "方案相关性" in note and "覆盖折扣" in note and "留空" in note
