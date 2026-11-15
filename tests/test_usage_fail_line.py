# 回归测试: LLM 记账日志行不得被体检的「失败行」判定误伤 (Step 2)
#
# 背景(2026-09-28 实证): steps/step17_backup_monitor.py 的 `_is_fail_line` 只判子串
# "失败", 而 Step 1 新增的两行记账日志天然含这两个字 ——
#   `LLM 用量监控: 价格来源 = 内置快照(抓取失败且无缓存)`
#   `步骤 NN LLM 用量: … | 失败尝试 N 次(未计费)`
# 不排除就会被 [连续失败](>=5 → CRIT) 与 [最近成功率](>50% → WARN) 当成失败,
# **把假告警推到企业微信**。这里锁死修复, 也锁死"真实失败仍要被抓到"。
import os
from datetime import datetime

import pytest

from core.config import Config
from core.paths import Paths
from core.prices import builtin_table
from core.usage import UsageMeter
from steps.base import StepContext
from steps.step17_backup_monitor import Step17BackupMonitor

USAGE = {"prompt_tokens": 1656, "completion_tokens": 2,
         "prompt_cache_hit_tokens": 1408, "prompt_cache_miss_tokens": 248}
MON_OFF = datetime(2026, 9, 28, 13, 0)


class _Log:
    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg): self.infos.append(str(msg))
    def warning(self, msg): self.warnings.append(str(msg))


def _monitor() -> Step17BackupMonitor:
    return Step17BackupMonitor()


# ------------------------------------------------------------------ 单元: _is_fail_line

@pytest.mark.parametrize("line", [
    # 抓取失败 → 兜底内置快照时的价格来源行(措辞含"失败")
    "2026-09-28 10:00:00 [INFO] LLM 用量监控: 价格来源 = 内置快照(抓取失败且无缓存)",
    "2026-09-28 10:00:00 [INFO] LLM 用量监控: 价格来源 = 磁盘缓存(2026-09-24 07:29, CNY, 已过期 99 小时)",
    "2026-09-28 10:00:00 [INFO] LLM 用量监控: 价格来源 = 官网实时(2026-09-28 10:28, CNY)",
    # 步骤记账行(有失败尝试时措辞含"失败")
    "2026-09-28 10:00:00 [INFO] 步骤 17 LLM 用量: 调用 412 次 | tokens 1580000"
    "(命中 1200000/未命中 300000/输出 80000) | 费用估算 3.8120 元 | 失败尝试 3 次(未计费)",
    "2026-09-28 10:00:00 [INFO] 步骤 07 LLM 用量: 调用 1 次 | tokens 1658"
    "(命中 1408/未命中 248/输出 2) | 费用估算 0.0003 元",
])
def test_accounting_lines_are_never_fail_lines(line):
    assert _monitor()._is_fail_line(line) is False


@pytest.mark.parametrize("line", [
    "2026-09-28 10:00:00 [ERROR] 步骤 17 属性标注 失败: boom(耗时 1分2秒)",
    "2026-09-28 10:00:00 [ERROR] [Step10Variants] 抓取失败, 已重试 3 次",
    "2026-09-28 10:00:00 [INFO] 体检项[抓取质量] WARN: 风控失败 5 条",
])
def test_real_failure_lines_still_detected(line):
    """修复不能把真失败一起放过。"""
    assert _monitor()._is_fail_line(line) is True


def test_own_step_logs_still_excluded():
    """既有行为(排除本步骤自身日志)不能被破坏。"""
    assert _monitor()._is_fail_line(
        "2026-09-28 10:00:00 [WARN] [Step17BackupMonitor] 检查内部异常: 失败") is False


def test_real_usage_line_generated_by_meter_is_not_fail_line():
    """用手写样例不够 —— 直接拿 meter **真实产出**的那一行来验。"""
    log = _Log()
    m = UsageMeter(builtin_table(), log=log, cat="T")
    m.begin_step(17)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    m.record_failure(channel="text", model="deepseek-flash", reason="LLM 请求超时")
    m.finish_step(17, "FAILED")

    line = next(x for x in log.infos if "LLM 用量" in x)
    assert "失败尝试 1 次" in line, "文案要保留信息量, 不能靠改文案规避"
    assert _monitor()._is_fail_line(line) is False


# ------------------------------------------------------------------ 端到端: 连续失败检查

def _ctx(tmp_path) -> StepContext:
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T", "LLM_PRICES_MODE": "builtin"})
    return StepContext(cfg, Paths(cfg), _Log())


def test_consecutive_failure_check_not_triggered_by_accounting_lines(tmp_path):
    """端到端: 日志尾部连续 6 行记账日志(含"失败"字样) 不得报 CRIT。

    这是本次修复的**真实收益**: 修复前 `_check_consecutive_failures` 会数出 6 条
    "失败"→ 达到阈值 5 → CRIT「疑似流程中断」→ 推企业微信。
    """
    ctx = _ctx(tmp_path)
    log_path = ctx.paths.monitor_file("pipeline-progress.log")
    lines = ["2026-09-28 10:00:00 [INFO] LLM 用量监控: 价格来源 = 内置快照(抓取失败且无缓存)"]
    lines += [
        f"2026-09-28 10:0{i}:00 [INFO] 步骤 {n:02d} LLM 用量: 调用 10 次 | tokens 1000"
        f"(命中 100/未命中 800/输出 100) | 费用估算 0.0100 元 | 失败尝试 2 次(未计费)"
        for i, n in enumerate(range(2, 8))
    ]
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    level, msg = _monitor()._check_consecutive_failures(ctx)
    assert level == "OK", f"记账日志被当成失败了: {msg}"
    assert "0 条" in msg


def test_consecutive_failure_check_still_catches_real_failures(tmp_path):
    """真失败连成串时仍必须 CRIT。"""
    ctx = _ctx(tmp_path)
    log_path = ctx.paths.monitor_file("pipeline-progress.log")
    lines = [f"2026-09-28 10:0{i}:00 [ERROR] 步骤 1{i} 抓取 失败: boom"
             for i in range(6)]
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    level, _msg = _monitor()._check_consecutive_failures(ctx)
    assert level == "CRIT"


def test_recent_success_ratio_not_skewed_by_accounting_lines(tmp_path):
    """端到端: 最近 20 行里混入记账日志, 失败占比不得被抬高到阈值以上。"""
    ctx = _ctx(tmp_path)
    log_path = ctx.paths.monitor_file("pipeline-progress.log")
    lines = []
    for i in range(6):
        lines.append(f"2026-09-28 10:{i:02d}:00 [ERROR] 步骤 0{i} 抓取 失败: boom")
        lines.append(f"2026-09-28 10:{i:02d}:30 [INFO] 步骤 0{i} LLM 用量: 调用 10 次"
                     f" | 失败尝试 1 次(未计费) | 费用估算 0.0100 元")
    lines += [f"2026-09-28 10:1{i}:00 [INFO] 步骤 1{i} 完成(DONE)(耗时 1分)" for i in range(5)]
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    level, msg = _monitor()._check_recent_success_ratio(ctx)
    assert level == "OK", f"失败占比被记账日志抬高了: {msg}"
