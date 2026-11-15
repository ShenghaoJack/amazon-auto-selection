# 单元测试: FAILED/INTERRUPTED 卡片事件次数统计(双文件去重)
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
import server as S  # noqa: E402


def _write(tmp, prog: str, alerts: str) -> Path:
    mdir = Path(tmp) / "17_备份与监控"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "pipeline-progress.log").write_text(prog, encoding="utf-8")
    (mdir / "monitor-alerts.txt").write_text(alerts, encoding="utf-8")
    return mdir


def test_failed_lines_counted_once(ws_tmp):
    """失败事件在 progress(ERROR)与 alerts(CRIT)双写, 只按 progress 计一次。"""
    prog = ("2026-09-03 09:24:55 [ERROR] 步骤 14 流量拆分 失败: 上游产物缺失\n"
            "2026-09-03 16:29:17 [ERROR] 步骤 10 变体抓取与第二遍过滤 失败: 复核门槛\n")
    alerts = ("2026-09-03 09:24:55 [CRIT] 步骤 14 流量拆分 失败: 上游产物缺失\n"
              "2026-09-03 16:29:17 [CRIT] 步骤 10 变体抓取与第二遍过滤 失败: 复核门槛\n")
    mdir = _write(ws_tmp, prog, alerts)
    fails, ints = S.log_event_counts(mdir)
    assert fails == 2
    assert ints == 0


def test_page_stop_counted_and_covers_stale_report(ws_tmp):
    """页面手动中断(仅 alerts)计入; 其后的强杀残留报告(progress+alerts 双写)
    若在 (开始于, 报告) 区间内有页面中断 → 不重复计(同一次中断只算 1 次)。"""
    prog = ("2026-09-03 17:48:46 [WARNING] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止"
            "(开始于 2026-09-03 17:25:25, 无正常结束记录, 中断≈2026-09-03 17:48:46), 本轮将重新执行该步骤\n")
    alerts = ("2026-09-03 17:32:47 [WARN] 流水线被页面手动中断(进程树强杀 pid=26980), 进行中步骤将于下次运行标 INTERRUPTED 并自动重跑\n"
              "2026-09-03 17:48:46 [WARN] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止"
              "(开始于 2026-09-03 17:25:25, 无正常结束记录, 中断≈2026-09-03 17:48:46), 本轮将重新执行该步骤\n")
    mdir = _write(ws_tmp, prog, alerts)
    fails, ints = S.log_event_counts(mdir)
    assert ints == 1  # 1 次页面中断(残留报告被覆盖)


def test_stale_reports_dedup_by_start(ws_tmp):
    """同一残留 RUNNING 被多轮重复报告(不同 run 都报同一"开始于")只计 1 次。"""
    prog = "".join(
        f"2026-09-03 15:49:{i:02d} [WARNING] 步骤 10 变体抓取与第二遍过滤 上次运行被终止"
        f"(开始于 2026-09-03 14:42:43, 无正常结束记录, 中断时间≈2026-09-03 15:49:{i:02d}), 本轮将重新执行该步骤\n"
        for i in (46, 50, 52, 56)
    )
    mdir = _write(ws_tmp, prog, "")
    fails, ints = S.log_event_counts(mdir)
    assert ints == 1


def test_stale_without_page_stop_counts_once(ws_tmp):
    """无页面中断记录时代的强杀残留(进程自行终止)按 开始于 计 1 次。"""
    prog = ("2026-09-03 09:24:55 [WARNING] 步骤 00 上次运行被终止"
            "(开始于 2026-09-03 09:03:42, 无正常结束记录, 中断时间≈2026-09-03 09:24:55), 本轮将重新执行该步骤\n"
            "2026-09-03 11:28:44 [WARNING] 步骤 00 上次运行被终止"
            "(开始于 2026-09-03 09:03:42, 无正常结束记录, 中断时间≈2026-09-03 11:28:44), 本轮将重新执行该步骤\n")
    mdir = _write(ws_tmp, prog, "")
    fails, ints = S.log_event_counts(mdir)
    assert ints == 1  # 同一 start 重复报告只计 1


def test_ctrl_c_marked_interrupted(ws_tmp):
    """Ctrl+C 标记行(已被标记 INTERRUPTED, 带标题)计入 1 次。"""
    prog = ("2026-09-03 17:40:00 [WARNING] 步骤 10 变体抓取与第二遍过滤 已被标记 INTERRUPTED"
            "(中断时间 2026-09-03 17:40:00); 重新运行将从该步骤自动续跑\n")
    alerts = ("2026-09-03 17:40:00 [WARN] 步骤 10 变体抓取与第二遍过滤 已被标记 INTERRUPTED"
              "(中断时间 2026-09-03 17:40:00); 重新运行将从该步骤自动续跑\n")
    mdir = _write(ws_tmp, prog, alerts)
    fails, ints = S.log_event_counts(mdir)
    assert ints == 1  # progress+alerts 双写仍只计 1(以 progress 为准)


def test_page_stop_two_times_counts_two(ws_tmp):
    """两次页面中断(无对应残留报告或残留已被覆盖)各计 1 次 → 共 2。"""
    alerts = ("2026-09-03 17:32:47 [WARN] 流水线被页面手动中断(进程树强杀 pid=26980), 进行中步骤将于下次运行标 INTERRUPTED 并自动重跑\n"
              "2026-09-03 18:11:30 [WARN] 流水线被页面手动中断(进程树强杀 pid=18532), 进行中步骤将于下次运行标 INTERRUPTED 并自动重跑\n")
    mdir = _write(ws_tmp, "", alerts)
    fails, ints = S.log_event_counts(mdir)
    assert ints == 2


def test_real_v2_timeline_counts(ws_tmp):
    """v2 当天真实时间线: 3 页面中断 + 3 早期残留(09:03/13:37/14:42) = 6;
    17:25/17:49 两次残留被页面中断覆盖不计(重复行 7 → 去重 6)。"""
    prog = (
        "2026-09-03 09:24:55 [ERROR] 步骤 14 流量拆分 失败: 上游产物缺失\n"
        "2026-09-03 09:24:55 [WARNING] 步骤 00 上次运行被终止(开始于 2026-09-03 09:03:42, 无正常结束记录, 中断时间≈2026-09-03 09:24:55), 本轮将重新执行该步骤\n"
        "2026-09-03 11:28:44 [WARNING] 步骤 00 上次运行被终止(开始于 2026-09-03 09:03:42, 无正常结束记录, 中断时间≈2026-09-03 11:28:44), 本轮将重新执行该步骤\n"
        "2026-09-03 11:37:19 [ERROR] 步骤 04 生成关键词 失败: 上游产物为空\n"
        "2026-09-03 11:43:55 [ERROR] 步骤 04 生成关键词 失败: 上游产物为空\n"
        "2026-09-03 12:23:45 [ERROR] 步骤 09 搜索收集 失败: 类目不达标\n"
        "2026-09-03 14:42:09 [WARNING] 步骤 10 上次运行被终止(开始于 2026-09-03 13:37:07, 无正常结束记录, 中断时间≈2026-09-03 14:42:09), 本轮将重新执行该步骤\n"
        "2026-09-03 15:49:46 [WARNING] 步骤 10 上次运行被终止(开始于 2026-09-03 14:42:43, 无正常结束记录, 中断时间≈2026-09-03 15:49:46), 本轮将重新执行该步骤\n"
        "2026-09-03 15:50:58 [WARNING] 步骤 10 上次运行被终止(开始于 2026-09-03 14:42:43, 无正常结束记录, 中断时间≈2026-09-03 15:50:58), 本轮将重新执行该步骤\n"
        "2026-09-03 15:52:31 [WARNING] 步骤 10 上次运行被终止(开始于 2026-09-03 14:42:43, 无正常结束记录, 中断时间≈2026-09-03 15:52:31), 本轮将重新执行该步骤\n"
        "2026-09-03 15:56:06 [WARNING] 步骤 10 上次运行被终止(开始于 2026-09-03 14:42:43, 无正常结束记录, 中断时间≈2026-09-03 15:56:06), 本轮将重新执行该步骤\n"
        "2026-09-03 16:29:17 [ERROR] 步骤 10 变体抓取与第二遍过滤 失败: 过滤复核一致率 63%\n"
        "2026-09-03 17:48:46 [WARNING] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止(开始于 2026-09-03 17:25:25, 无正常结束记录, 中断≈2026-09-03 17:48:46), 本轮将重新执行该步骤\n"
        "2026-09-03 23:34:55 [WARNING] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止(开始于 2026-09-03 17:49:21, 无正常结束记录, 中断≈2026-09-03 23:34:55), 本轮将重新执行该步骤\n"
    )
    alerts = (
        "2026-09-03 09:24:55 [CRIT] 步骤 14 流量拆分 失败: 上游产物缺失\n"
        "2026-09-03 11:37:19 [CRIT] 步骤 04 生成关键词 失败: 上游产物为空\n"
        "2026-09-03 11:43:55 [CRIT] 步骤 04 生成关键词 失败: 上游产物为空\n"
        "2026-09-03 12:23:45 [CRIT] 步骤 09 搜索收集 失败: 类目不达标\n"
        "2026-09-03 13:33:34 [RESET] 步骤 4-17 已重置\n"
        "2026-09-03 16:29:17 [CRIT] 步骤 10 变体抓取与第二遍过滤 失败: 过滤复核一致率 63%\n"
        "2026-09-03 17:32:47 [WARN] 流水线被页面手动中断(进程树强杀 pid=26980)\n"
        "2026-09-03 17:48:46 [WARN] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止(开始于 2026-09-03 17:25:25, 无正常结束记录, 中断≈2026-09-03 17:48:46)\n"
        "2026-09-03 18:11:30 [WARN] 流水线被页面手动中断(进程树强杀 pid=18532)\n"
        "2026-09-03 23:34:55 [WARN] 步骤 10 变体抓取与第二遍过滤 上次运行被进程强杀/终止(开始于 2026-09-03 17:49:21, 无正常结束记录, 中断≈2026-09-03 23:34:55)\n"
        "2026-09-03 23:53:33 [WARN] 流水线被页面手动中断(进程树强杀 pid=33356)\n"
    )
    mdir = _write(ws_tmp, prog, alerts)
    fails, ints = S.log_event_counts(mdir)
    assert fails == 5
    assert ints == 6


def test_event_cache_is_per_directory(ws_tmp):
    """回归(2026-09-11): 缓存键必须带路径 —— 两个目录监控文件时间戳相同也不能互相串结果。

    原实现缓存键只有 (progress_mtime, alerts_mtime), 不同类目/不同测试目录很容易撞上同一时间戳
    (NTFS 时间戳粒度约 15ms), 会返回**另一个目录**的计数。
    """
    import os as _os

    mdir_a = _write(_os.path.join(ws_tmp, "A"), "",
                    "2026-09-03 17:32:47 [WARN] 流水线被页面手动中断(进程树强杀 pid=1)\n"
                    "2026-09-03 18:11:30 [WARN] 流水线被页面手动中断(进程树强杀 pid=2)\n")
    mdir_b = _write(_os.path.join(ws_tmp, "B"), "",
                    "2026-09-03 19:00:00 [WARN] 流水线被页面手动中断(进程树强杀 pid=3)\n")
    # 把两个目录的时间戳强行设成一致(复现 mtime 撞车)
    for name in ("pipeline-progress.log", "monitor-alerts.txt"):
        _os.utime(mdir_a / name, (1700000000, 1700000000))
        _os.utime(mdir_b / name, (1700000000, 1700000000))
    S._EVENT_CACHE.clear()
    ints_a = S.log_event_counts(mdir_a)[1]
    ints_b = S.log_event_counts(mdir_b)[1]
    assert ints_a == 2, f"A 目录应为 2 次页面中断, 实际 {ints_a}"
    assert ints_b == 1, f"B 目录应为 1 次页面中断, 实际 {ints_b}(说明拿到了 A 的缓存结果)"
    # 反向再取一次(A 的结果不能被 B 覆盖)
    assert S.log_event_counts(mdir_a)[1] == 2
