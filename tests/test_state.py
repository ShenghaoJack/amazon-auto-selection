# 单元测试: 步骤状态与断点续跑
import os
from datetime import datetime

from core.state import RunState, format_duration


def test_format_duration():
    assert format_duration(0) == "0秒"
    assert format_duration(45.6) == "46秒"
    assert format_duration(125) == "2分5秒"
    assert format_duration(3600) == "1小时0分0秒"
    assert format_duration(3905) == "1小时5分5秒"
    assert format_duration(-5) == "0秒"


def test_duration_text_from_lifecycle(ws_tmp):
    """mark_done 后 duration_text 能算出该步耗时(完成时间-开始时间)。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(1)
    st.mark_done(1)
    text = st.duration_text(1)
    assert text  # 非空(如 "0秒"/"1秒")
    assert st.duration_seconds(1) is not None and st.duration_seconds(1) >= 0
    # 无开始/结束 → 空
    assert RunState(os.path.join(ws_tmp, "x.txt"), dur).duration_text(99) == ""


def test_state_lifecycle(ws_tmp):
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    assert st.first_unfinished(0) == 0
    st.start_step(0)
    assert st.status_of(0) == "RUNNING"
    st.mark_done(0)
    st.mark_skip(3)
    assert st.is_finished(0)
    assert st.is_finished(3)
    assert st.first_unfinished(0) == 1
    st2 = RunState(status, dur)
    assert st2.status_of(0) == "DONE"
    assert st2.first_unfinished(0) == 1
    assert os.path.exists(dur)


def test_mark_failed_reason(ws_tmp):
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(5)
    st.mark_failed(5, "上游缺失")
    st2 = RunState(status, dur)
    assert st2.status_of(5) == "FAILED"


def test_load_tolerates_utf8_bom(ws_tmp):
    """状态文件带 UTF-8 BOM 时首行不得被丢弃(否则步骤00 变未完成 → 整轮从0重跑)。

    实战 2026-09-11: 用 Windows PowerShell 5.1 `Set-Content -Encoding utf8` 改写状态文件会写入 BOM,
    按 utf-8 读取时首行解析成 "\\ufeff0|DONE|…" → int() 失败 → 步骤00 被静默丢掉。
    """
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    lines = [
        "0|DONE|2026-09-11 09:14:58|2026-09-11 09:15:56||0|0|0",
        "1|DONE|2026-09-11 09:15:56|2026-09-11 09:20:44||0|0|0",
        "2|PENDING||||0|0|0",
    ]
    # 带 BOM 写出(模拟记事本 / PowerShell 5.1)
    with open(status, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    st = RunState(status, dur)
    assert st.status_of(0) == "DONE", "带 BOM 时步骤00 不能被丢掉"
    assert st.is_finished(0) and st.is_finished(1)
    assert st.first_unfinished(0) == 2, "应从真正的未完成步骤(2)续跑, 而不是回到 0"


def test_load_plain_utf8_unchanged(ws_tmp):
    """无 BOM 的普通 UTF-8 行为不变(回归)。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    with open(status, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("0|DONE||||0|0|0\n1|DONE||||0|0|0\n")
    st = RunState(status, dur)
    assert st.status_of(0) == "DONE" and st.first_unfinished(0) == 2


def test_reason_with_pipe_and_newline_does_not_corrupt_status(ws_tmp):
    """回归 2026-09-13: reason 含 "|" 或换行时必须净化, 否则读回侧按位置取值错位。

    原实现把 str(exc)[:300] 原样拼进 "|" 分隔行: 多一个 "|" → acc/fails/ints 顺位错读(且
    int() 失败被吞成 0); 含换行 → 整行被劈开, 带真实计数的后半行被当坏行丢弃, 并在下一次
    _save 时把错误固化。
    """
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(3)
    st.mark_failed(3, "Timeout 8000ms exceeded.\nCall log:\n  - waiting|120|2|1")

    raw = open(status, encoding="utf-8-sig").read()
    assert len([ln for ln in raw.splitlines() if ln]) == 1, "reason 含换行也不得把一行劈成多行"

    st2 = RunState(status, dur)
    assert st2.status_of(3) == "FAILED"
    assert st2.steps[3]["fails"] == 1, "失败计数不得被 reason 里的 | 顶位"
    assert st2.steps[3]["ints"] == 0
    reason = st2.steps[3].get("reason", "")
    assert "|" not in reason and "\n" not in reason
    assert "Timeout 8000ms exceeded." in reason   # 内容仍可见(只是被净化)


def test_mark_interrupted_sets_end(ws_tmp):
    """中断标记: end=中断时间, 状态 INTERRUPTED; 未完成(可重跑)。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(7)
    st.mark_interrupted(7, "运行被中断(Ctrl+C)")
    assert st.status_of(7) == "INTERRUPTED"
    assert st.steps[7]["end"]  # 中断时间已记
    assert not st.is_finished(7)  # INTERRUPTED 不算完成 → 重跑
    st2 = RunState(status, dur)
    assert st2.status_of(7) == "INTERRUPTED"
    assert st2.steps[7]["end"]


def test_start_step_clears_previous_end_reason(ws_tmp):
    """重跑 start_step 应清掉上一轮 end/reason, 避免中断时间与完成时间混淆。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(9)
    st.mark_interrupted(9, "上次中断于 X")
    st.start_step(9)  # 重跑
    assert st.status_of(9) == "RUNNING"
    assert not st.steps[9].get("end")  # 旧中断时间已清
    assert not st.steps[9].get("reason")
    st.mark_done(9)
    assert st.steps[9]["end"]  # 新 end=完成时间


def test_mark_pending_clears_old_timestamps(ws_tmp):
    """断点保护重置为 PENDING: 旧 start/end/reason 全清(重跑才记新开始)。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(6)
    st.mark_done(6)
    st.mark_pending(6)
    assert st.status_of(6) == "PENDING"
    assert not st.steps[6].get("start")
    assert not st.steps[6].get("end")
    assert not st.steps[6].get("reason")


def test_interruptions_detection(ws_tmp):
    """interruptions(): 残留 RUNNING(进程被终止)与 INTERRUPTED 都报告, 便于区分中断时间。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    # 模拟: 步骤3 上次进程被强杀(残留 RUNNING 无 end); 步骤5 已标记 INTERRUPTED
    st.start_step(3)  # RUNNING 无 end(不 finish)
    st.start_step(5)
    st.mark_interrupted(5, "Ctrl+C")
    its = st.interruptions()
    kinds = {it["number"]: it["kind"] for it in its}
    assert kinds[3] == "stale_running"
    assert kinds[5] == "interrupted"
    assert its[1]["end"]  # 步骤5 带中断时间
    # DONE 步骤不应出现在中断列表
    st.mark_done(3)
    assert all(it["number"] != 3 for it in st.interruptions())


def test_rerun_accumulates_interrupted_partial(ws_tmp):
    """中断/失败后重跑: 暂停前已执行段并入 acc, 重跑从暂停的累计继续计时。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    # 模拟上一轮: 步骤7 于 10:00:00 开始, 10:00:30 被中断
    st.start_step(7)
    st.steps[7].update({"status": "INTERRUPTED", "start": "2026-09-03 10:00:00", "end": "2026-09-03 10:00:30"})
    st._save()
    st2 = RunState(status, dur)
    st2.start_step(7)  # 重跑: 旧段 30s 应并入 acc
    assert st2.steps[7]["acc"] == 30
    st2.steps[7]["start"] = "2026-09-03 10:01:00"
    st2.mark_done(7)
    assert st2.duration_seconds(7) is not None and st2.duration_seconds(7) >= 30
    # 失败重跑: 同样把失败前已执行段并入 acc(从暂停时刻继续计时)
    st.start_step(8)
    st.steps[8].update({"status": "FAILED", "start": "2026-09-03 11:00:00", "end": "2026-09-03 11:00:20"})
    st._save()
    st3 = RunState(status, dur)
    st3.start_step(8)
    assert st3.steps[8]["acc"] == 20
    # 持久化: 重载后 acc 仍在
    st3.steps[8]["start"] = "2026-09-03 11:02:00"
    st3.mark_done(8)
    st4 = RunState(status, dur)
    assert st4.steps[8]["acc"] == 20


def test_stale_running_merges_with_end_hint(ws_tmp):
    """强杀残留(RUNNING 无结束): 重跑时以 end_hint 近似中断时刻并入 acc, 续计不归零。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(3)
    st.steps[3]["start"] = "2026-09-03 08:00:00"   # 模拟被强杀, 无 end
    st._save()
    st2 = RunState(status, dur)
    st2.start_step(3, end_hint="2026-09-03 08:00:40")   # 重跑, 给出近似中断时刻
    assert st2.steps[3]["acc"] == 40
    assert st2.steps[3]["ints"] == 1
    assert st2.status_of(3) == "RUNNING"


def test_stale_running_without_hint_does_not_add_idle_gap(ws_tmp):
    """强杀残留且找不到 end_hint(无该步真实活动日志): 只保留 acc, 不加 上次start→本轮启动
    之间未运行的闲置时段(2026-09 修复: 旧逻辑回退"本轮检测时刻"会把闲置段误并入 acc)。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    # 上一轮 08:00 开始被强杀(残留 RUNNING 无 end); 若隔了很久(如 23:00)才重跑,
    # 找不到 end_hint 时应按 start 计(span=0), acc 不虚增 15 小时闲置段
    st.start_step(3)
    st.steps[3].update({"start": "2026-09-03 08:00:00"})
    st._save()
    st2 = RunState(status, dur)
    st2.start_step(3, end_hint="")        # 找不到真实活动日志
    assert st2.steps[3]["acc"] == 0       # 不并入闲置段
    assert st2.steps[3]["ints"] == 1      # 中断次数照记
    assert st2.status_of(3) == "RUNNING"
    # 带历史 acc 的残留同样不虚增
    st3 = RunState(status, dur)
    st3.steps[3] = {"status": "RUNNING", "start": "2026-09-03 08:00:00",
                    "end": "", "acc": 100, "fails": 0, "ints": 1}
    st3._save()
    st4 = RunState(status, dur)
    st4.start_step(3, end_hint="")
    assert st4.steps[3]["acc"] == 100     # 历史 acc 保留, 不加闲置


def test_fail_interrupt_counts_cumulative(ws_tmp):
    """FAILED/INTERRUPTED 卡片次数为累计: 每次失败/中断 +1, 重跑成功不清零。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    st = RunState(status, dur)
    st.start_step(2)
    st.mark_failed(2, "第一次失败")
    st.start_step(2)          # 重跑
    st.mark_failed(2, "第二次失败")
    st.start_step(2)          # 再重跑 → 成功
    st.mark_done(2)
    st2 = RunState(status, dur)
    assert st2.steps[2]["fails"] == 2       # 累计 2 次失败, DONE 后仍为 2
    assert st2.steps[2]["ints"] == 0
    st.start_step(4)
    st.mark_interrupted(4, "Ctrl+C 1")
    st.start_step(4)
    st.mark_interrupted(4, "Ctrl+C 2")
    st.start_step(4)
    st.mark_done(4)
    st3 = RunState(status, dur)
    assert st3.steps[4]["ints"] == 2        # 累计 2 次中断
    # 强杀残留 RUNNING(无 end)在下次启动重跑时计 1 次中断
    st.start_step(6)          # 模拟残留 RUNNING(不 finish)
    st4 = RunState(status, dur)
    assert st4.steps[6]["ints"] == 0
    st5 = RunState(status, dur)
    st5.start_step(6)         # 重跑 → 残留计中断
    assert st5.steps[6]["ints"] == 1


# --- 累计执行时长(2026-09-15 与 webui 看板统一口径) ---

def _write_status(path, rows):
    """按真实文件格式写 step-status.txt: num|status|start|end|reason|acc|fails|ints。"""
    lines = ["|".join(str(c) for c in r) for r in rows]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_cumulative_seconds_sums_steps(ws_tmp):
    """累计执行时长 = 各步 (end-start); 未开始的步骤不计。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    _write_status(status, [
        [0, "DONE", "2026-09-14 10:00:00", "2026-09-14 10:01:00", "", 0, 0, 0],   # 60s
        [1, "DONE", "2026-09-14 10:01:00", "2026-09-14 10:02:30", "", 0, 0, 0],   # 90s
        [2, "PENDING", "", "", "", 0, 0, 0],                                      # 0
    ])
    st = RunState(status, dur)
    assert st.cumulative_seconds() == 150


def test_cumulative_seconds_includes_acc_from_earlier_segment(ws_tmp):
    """跨进程段: 已跑完的段落在 acc 里, 必须并入(否则"中断→续跑"会少报)。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    _write_status(status, [
        # 步骤10: 前一段已跑 7200s 并入 acc, 本段又跑了 600s → 7800s
        [10, "DONE", "2026-09-14 19:45:00", "2026-09-14 19:55:00", "", 7200, 0, 1],
    ])
    st = RunState(status, dur)
    assert st.cumulative_seconds() == 7800


def test_cumulative_seconds_counts_running_step_live(ws_tmp):
    """运行中的步骤(有 start 无 end)按 start→now 实时计入, now 可注入便于对账。"""
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    _write_status(status, [
        [0, "DONE", "2026-09-14 10:00:00", "2026-09-14 10:01:00", "", 0, 0, 0],          # 60s
        [1, "RUNNING", "2026-09-14 10:01:00", "", "", 300, 0, 0],                        # acc 300 + 120 = 420s
    ])
    st = RunState(status, dur)
    now = datetime(2026, 9, 14, 10, 3, 0)
    assert st.cumulative_seconds(now) == 60 + 420


def test_cumulative_seconds_excludes_idle_gap_between_segments(ws_tmp):
    """口径要点: 累计执行时长**不含**进程段之间没在跑的空档 —— 所以它小于墙钟。

    步骤10 前一段真实活动到 19:03:00 就停了, 但进程到 19:44 才被强杀;
    那 41 分钟没有日志活动, 由 end_hint 截断, 不得计入(否则虚增累计时长)。
    """
    status = os.path.join(ws_tmp, "step-status.txt")
    dur = os.path.join(ws_tmp, "step-duration.csv")
    _write_status(status, [
        # acc 只保留真正的执行段(25 分钟), 而非 11:23→19:44 的 8 小时余
        [10, "DONE", "2026-09-14 19:45:00", "2026-09-14 20:13:10", "", 1500, 0, 1],
    ])
    st = RunState(status, dur)
    total = st.cumulative_seconds()
    assert total == 1500 + 1690          # 25min + (19:45:00→20:13:10)
    # 与墙钟的差异必须存在: 累计口径 < 从首次开始到收尾的墙钟
    wall = (datetime(2026, 9, 15, 2, 28, 31) - datetime(2026, 9, 14, 10, 53, 14)).total_seconds()
    assert total < wall
