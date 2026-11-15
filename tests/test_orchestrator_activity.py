# 单元测试: _is_step_real_activity 只认真实执行活动, 排除编排告警行
import os
from datetime import datetime, timedelta

from core.orchestrator import _is_step_real_activity, finish_line
from core.state import RunState


def _write_status(path, rows):
    lines = ["|".join(str(c) for c in r) for r in rows]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_real_step_activity_lines():
    """真实执行活动日志被识别。"""
    assert _is_step_real_activity("2026-09-03 17:25:25 [INFO] [Step10Variants] 开始 FR-10", 10)
    assert _is_step_real_activity("2026-09-03 17:49:21 [INFO] === 步骤 10 变体抓取与第二遍过滤 开始 ===", 10)
    assert _is_step_real_activity("2026-09-03 17:25:31 [INFO] [Step10Variants] 全部 1012 个 ASIN 抓取完成", 10)
    # 完成/跳过行(紧跟式, 与 webui 展示口径一致); 残留 RUNNING 场景无结束行,
    # 结束行匹配用于完成日志定位, 失败行带标题不参与 end_hint(失败必有 end)
    assert _is_step_real_activity("2026-09-03 12:00:00 [INFO] 步骤 10 完成(DONE)(耗时 3分25秒)", 10)
    assert _is_step_real_activity("2026-09-03 12:00:00 [INFO] 步骤 10 跳过(SKIP)", 10)


def test_orchestrator_report_lines_excluded():
    """编排报告行(本轮启动时写的告警/续跑提示)不得当作该步真实活动:
    否则 end_hint=本轮启动时刻, 会把闲置时段误并入 acc(2026-09 修复)。"""
    # "上次运行被进程强杀/终止" 报告行(monitor 里常见)
    assert not _is_step_real_activity(
        "2026-09-03 17:48:46 [WARNING] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止(开始于 2026-09-03 17:25:25, 无正常结束记录, 中断≈2026-09-03 17:48:46), 本轮将重新执行该步骤", 10)
    # "断点续跑: …从步骤 N 开始" 行
    assert not _is_step_real_activity(
        "2026-09-03 17:48:46 [INFO] 断点续跑: 步骤 0-9 已完成, 从步骤 10 开始", 10)
    # "流水线被页面手动中断" 行
    assert not _is_step_real_activity(
        "2026-09-03 17:32:47 [WARN] 流水线被页面手动中断(进程树强杀 pid=26980)", 10)
    # 其它步骤的日志不影响本步判定
    assert not _is_step_real_activity("2026-09-03 17:00:00 [INFO] [Step11TrafficSource] 开始", 10)


def test_other_step_number_not_confused():
    """步骤号区分: 步骤 1 的日志不应被当成步骤 10 的活动。"""
    assert not _is_step_real_activity("2026-09-03 17:00:00 [INFO] [Step01Details] 开始", 10)
    assert not _is_step_real_activity("2026-09-03 17:00:00 [INFO] === 步骤 1 榜单详情抓取 开始 ===", 10)
    assert _is_step_real_activity("2026-09-03 17:00:00 [INFO] [Step01Details] 开始", 1)
    assert not _is_step_real_activity("2026-09-03 17:00:00 [INFO] [Step1Variants] 开始", 10)


# --- 完成行耗时口径(2026-09-15 与 webui 看板「累计执行时长」统一) ---

def test_finish_line_reports_cumulative_not_process_segment(ws_tmp):
    """完成行的首数必须是**累计执行时长**(页面口径), 而不是本进程段。

    场景 = 2026-09-15 Grilling 实战的形状(数值为示意, 非逐秒复刻): 同一轮被强杀
    两次、分 3 段跑完。步骤10 前一段已跑 7200s(并入 acc), 本段又跑 1682s;
    步骤16 在本段跑 15188s。最后一段进程只跑了 4h33m13s, 累计应为
    73 + (7200+1682) + 15188 = 24143s = 6小时42分23秒。
    """
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    _write_status(status, [
        [0, "DONE", "2026-09-14 10:53:14", "2026-09-14 10:54:27", "", 0, 0, 0],
        [10, "DONE", "2026-09-14 19:45:08", "2026-09-14 20:13:10", "", 7200, 0, 1],
        [16, "DONE", "2026-09-14 22:15:20", "2026-09-15 02:28:28", "", 0, 0, 0],
    ])
    st = RunState(status, dur)
    now = datetime(2026, 9, 15, 2, 28, 31)
    line = finish_line(st, timedelta(seconds=16393), now)

    assert line.startswith("流水线全部步骤完成(本轮总耗时 ")
    # 首数 = 累计执行(页面口径), 而不是本进程段
    assert "本轮总耗时 6小时42分23秒" in line
    assert "本进程段 4小时33分13秒" in line          # 旧口径作为参照保留
    assert "整轮墙钟 15小时35分17秒" in line          # 00 步开始 → now(含中断空档)
    assert "本轮总耗时 4小时33分13秒" not in line


def test_finish_line_without_first_start_omits_wall_clock(ws_tmp):
    """没有步骤00开始时间时(异常/只跑单步)不编造墙钟数, 只报累计与本段。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    _write_status(status, [[3, "DONE", "2026-09-14 10:00:00", "2026-09-14 10:00:46", "", 0, 0, 0]])
    st = RunState(status, dur)
    line = finish_line(st, timedelta(seconds=100), datetime(2026, 9, 14, 10, 5, 0))
    assert "本轮总耗时 46秒" in line
    assert "本进程段 1分40秒" in line
    assert "整轮墙钟" not in line
