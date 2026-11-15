# 集成测试: 编排器把用量/费用接到步骤生命周期上 (Step 1)
#
# 覆盖 core/orchestrator.py 的三处接线:
#   ① 启动时打一行"价格来源"
#   ② 每步开始重置计数 + 结束时打一行用量
#   ③ 失败/跳过的步骤同样记账(失败前可能已经烧了 token)
# 不跑真实流水线: 直接调 _run_one + 一个假步骤, 不碰网络/浏览器/真实数据。
import json
import os
from datetime import datetime
from unittest import mock

import pytest

from core.config import Config
from core.orchestrator import PipelineRunner
from steps.base import BaseStep

USAGE = {"prompt_tokens": 1656, "completion_tokens": 2,
         "prompt_cache_hit_tokens": 1408, "prompt_cache_miss_tokens": 248}
MON_OFF = datetime(2026, 9, 28, 13, 0)      # 周一 13:00 = 空闲时段


class _FakeStep(BaseStep):
    number = 7
    title = "假步骤"
    artifacts: list[str] = []

    def run(self, ctx) -> None:
        # ctx.llm() 必须绑定到本步骤的 meter, 否则按步骤归因就是假的
        assert ctx.llm().meter() is ctx.usage()
        ctx.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)


def _runner(tmp_path) -> PipelineRunner:
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T",
                  "LLM_PRICES_MODE": "builtin"})     # builtin = 绝不联网
    return PipelineRunner(cfg, dry_run=False)


def _log_text(runner: PipelineRunner) -> str:
    path = runner.paths.monitor_file("pipeline-progress.log")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def _jsonl_rows(runner: PipelineRunner) -> list[dict]:
    path = os.path.join(runner.paths.monitor_dir(), "llm-usage.jsonl")
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


@pytest.fixture(autouse=True)
def _no_popup():
    # 失败分支会弹 Windows 消息框 —— 测试里必须屏蔽, 保持无副作用
    with mock.patch("core.notify.popup_critical"):
        yield


def test_price_source_line_is_logged(tmp_path):
    r = _runner(tmp_path)
    r._log_price_source()
    log = _log_text(r)
    assert "LLM 用量监控: 价格来源" in log
    assert "内置快照" in log


def test_run_one_logs_usage_line_and_persists(tmp_path):
    r = _runner(tmp_path)
    assert r._run_one(_FakeStep()) == 0

    log = _log_text(r)
    assert "步骤 07 LLM 用量: 调用 1 次" in log
    assert "tokens 1658(命中 1408/未命中 248/输出 2)" in log
    assert "费用估算" in log

    rows = _jsonl_rows(r)
    assert rows[0]["type"] == "meta" and rows[0]["cat"] == "T"
    assert [x["type"] for x in rows if x["type"] == "call"] == ["call"]
    summary = [x for x in rows if x["type"] == "step-summary"]
    assert len(summary) == 1 and summary[0]["step"] == 7 and summary[0]["status"] == "DONE"
    assert summary[0]["cost_cny"] == pytest.approx(0.00028416, abs=1e-9)

    csv_path = os.path.join(r.paths.monitor_dir(), "llm-usage-steps.csv")
    csv_rows = open(csv_path, encoding="utf-8-sig").read().strip().splitlines()
    assert csv_rows[0].split(",")[:4] == ["步骤", "状态", "通道", "模型"]
    assert csv_rows[1].startswith("07,DONE,text,deepseek-flash,1,")
    # step-summary 必须带通道/模型拆分(看板与体检按它算"图片花了多少")
    assert summary[0]["by_channel"]["text"]["calls"] == 1
    assert "text/deepseek-flash" in summary[0]["by_model"]


def test_failed_step_still_records_usage(tmp_path):
    """失败的步骤也要记账 —— 它失败前已经调用过 LLM, 那笔钱是真实发生的。"""

    class _Boom(_FakeStep):
        def run(self, ctx) -> None:
            super().run(ctx)
            raise RuntimeError("模拟步骤失败")

    r = _runner(tmp_path)
    assert r._run_one(_Boom()) == 2
    assert "步骤 07 LLM 用量: 调用 1 次" in _log_text(r)
    summary = [x for x in _jsonl_rows(r) if x["type"] == "step-summary"]
    assert summary[0]["status"] == "FAILED" and summary[0]["calls"] == 1


def test_step_without_llm_calls_logs_nothing(tmp_path):
    """没调 LLM 的步骤不刷无意义的 0 行。"""

    class _Quiet(_FakeStep):
        def run(self, ctx) -> None:
            pass

    r = _runner(tmp_path)
    assert r._run_one(_Quiet()) == 0
    assert "LLM 用量" not in _log_text(r)
    assert [x for x in _jsonl_rows(r) if x["type"] == "step-summary"][0]["calls"] == 0


def test_dry_run_does_not_write_usage_files(tmp_path):
    """dry-run: Mock 数据不得进真实账。"""
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T",
                  "LLM_PRICES_MODE": "builtin", "DRY_RUN": True})
    r = PipelineRunner(cfg, dry_run=True)
    r._log_price_source()
    assert "dry-run 模式, 本次不记账" in _log_text(r)
    assert not os.path.exists(os.path.join(r.paths.monitor_dir(), "llm-usage.jsonl"))


# ------------------------------------------------------------------ Step 2: run_id 按轮

def test_run_id_is_snapshot_dirname(tmp_path):
    """本轮账本身份 = 归档快照目录名(而不是进程时间戳)。"""
    r = _runner(tmp_path)
    snap = r.snapshot_dir()
    assert r.ctx.usage().run_id == os.path.basename(snap)


def test_run_id_same_across_process_segments(tmp_path):
    """一轮被强杀后分多段跑完 → 复用同一快照 → **同一 run_id**, 账才能合并成一本。

    若按进程时间戳生成 run_id, 这两个"段"会被算成两轮, "本轮总共花了多少"就不存在了。
    """
    r1 = _runner(tmp_path)
    r1.snapshot_dir()
    rid1 = r1.ctx.usage().run_id

    r2 = _runner(tmp_path)            # 模拟第二个进程段(续跑)
    r2.snapshot_dir()
    rid2 = r2.ctx.usage().run_id

    assert rid1 == rid2
    assert rid1.startswith("T-"), f"应为 <CAT>-<时间戳> 形状, 实得 {rid1!r}"


def test_explicit_run_id_wins_over_snapshot(tmp_path):
    r = _runner(tmp_path)
    r.ctx.set_run_id("MY-RUN")
    assert r.ctx.usage().run_id == "MY-RUN"
    r.snapshot_dir()                  # meter 已建 → 不应改写, 也不该抛
    assert r.ctx.usage().run_id == "MY-RUN"


# ------------------------------------------------------------------ Step 2: 完成行与归档

def test_usage_suffix_reads_ledger_by_run_id(tmp_path):
    r = _runner(tmp_path)
    r.snapshot_dir()
    r.ctx.begin_step(7)
    r.ctx.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    r.ctx.finish_step(7, "DONE")
    suffix = r._usage_suffix()
    assert "tokens 1658" in suffix and "费用估算" in suffix


def test_usage_suffix_empty_without_calls(tmp_path):
    """没调过 LLM 时不编造 0 元小结。"""
    r = _runner(tmp_path)
    r.snapshot_dir()
    assert r._usage_suffix() == ""


def test_run_summary_reaches_archive(tmp_path):
    """隐患 B 回归: 收尾后要补同步监控目录, 归档里的账本必须含 run-summary。

    run-summary 是在步骤19 的归档同步**之后**才追加的 —— 不补这一下, 归档(交付物)
    里的 llm-usage.jsonl 会缺最后一行。
    """
    r = _runner(tmp_path)
    snap = r.snapshot_dir()
    r.ctx.begin_step(7)
    r.ctx.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    r.ctx.finish_step(7, "DONE")

    r._log_run_summary("测试收尾")

    from core.paths import STEP_DIR_NAMES
    from steps.ids import MONITOR_STEP
    arch = os.path.join(snap, STEP_DIR_NAMES[MONITOR_STEP], "llm-usage.jsonl")
    assert os.path.exists(arch), "监控目录应已同步进归档"
    types = [json.loads(x)["type"] for x in open(arch, encoding="utf-8") if x.strip()]
    assert "run-summary" in types, f"归档账本缺 run-summary: {types}"


# ---- 累计口径(2026-09-29 用户定稿): 中断/重置都不清零 ----

def test_interrupt_writes_step_summary_so_tokens_are_not_lost(tmp_path):
    """中途中断必须累计: Ctrl+C 那一步已经烧掉的 token 要落 step-summary。

    旧行为只有 DONE/SKIP/FAILED 写汇总 → 中断那一步的用量在看板上"凭空消失"。
    """
    r = _runner(tmp_path)
    r.snapshot_dir()
    r.ctx.begin_step(10)
    r.ctx.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    r.state.start_step(10)                       # 步骤10 正在跑

    r._mark_current_interrupted()

    assert r.state.status_of(10) == "INTERRUPTED"
    rows = [x for x in _jsonl_rows(r) if x.get("type") == "step-summary"]
    assert [x["step"] for x in rows] == [10]
    assert rows[0]["status"] == "INTERRUPTED"
    assert rows[0]["calls"] == 1 and rows[0]["total_tokens"] == 1658
    assert "tokens 1658" in _log_text(r)


def test_interrupt_without_calls_writes_empty_summary_and_marks_state(tmp_path):
    """没有 LLM 调用的步骤被中断: 记一条 calls=0 的 INTERRUPTED 汇总(如实记录"这次尝试"),
    但看板对 calls=0 的步骤不显示费用行。"""
    r = _runner(tmp_path)
    r.snapshot_dir()
    r.state.start_step(11)
    r._mark_current_interrupted()
    assert r.state.status_of(11) == "INTERRUPTED"
    rows = [x for x in _jsonl_rows(r) if x.get("type") == "step-summary"]
    assert [x["step"] for x in rows] == [11]
    assert rows[0]["calls"] == 0 and rows[0]["status"] == "INTERRUPTED"
    from core.usage import summarize_category
    assert summarize_category(_jsonl_rows(r))["totals"]["calls"] == 0


def test_reset_category_keeps_llm_ledger(tmp_path):
    """重置类目不得清零累计: 账本(主文件 + 轮转卷 + CSV)要原样保留。"""
    from core.usage import load_records, summarize_category

    r = _runner(tmp_path)
    r.snapshot_dir()
    r.ctx.begin_step(10)
    r.ctx.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    r.ctx.finish_step(10, "DONE")

    mdir = r.paths.monitor_dir()
    rotated = os.path.join(mdir, "llm-usage-20260929.jsonl")
    with open(rotated, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "call", "run_id": "OLD", "cat": "T", "step": 3,
                             "in_miss": 100, "out": 0, "cost_cny": 0.01,
                             "channel": "text", "model": "deepseek-flash"}) + "\n")

    before = summarize_category(load_records(os.path.join(mdir, "llm-usage.jsonl"))
                               + load_records(rotated))["totals"]

    r.reset_category()

    assert os.path.exists(os.path.join(mdir, "llm-usage.jsonl")), "主账本应保留"
    assert os.path.exists(rotated), "轮转卷也应保留"
    assert os.path.exists(os.path.join(mdir, "llm-usage-steps.csv"))
    after = summarize_category(load_records(os.path.join(mdir, "llm-usage.jsonl"))
                               + load_records(rotated))["totals"]
    assert after["calls"] == before["calls"]
    assert after["total_tokens"] == before["total_tokens"]
    assert after["cost_cny"] == pytest.approx(before["cost_cny"], abs=1e-9)
    assert after["calls"] >= 2, "历史(轮转卷)不能被清掉"


def test_reset_steps_keeps_monitor_dir_and_ledger(tmp_path):
    """重置步骤(部分重置)同样不清账本: 17_备份与监控 明确跳过删除。"""
    r = _runner(tmp_path)
    r.snapshot_dir()
    r.ctx.begin_step(10)
    r.ctx.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    r.ctx.finish_step(10, "DONE")
    mdir = r.paths.monitor_dir()

    r.reset_steps_from(4)

    assert os.path.exists(os.path.join(mdir, "llm-usage.jsonl"))
    assert os.path.exists(os.path.join(mdir, "pipeline-progress.log"))


def test_finish_line_extra_is_appended_and_default_is_unchanged(tmp_path):
    from datetime import timedelta

    from core.orchestrator import finish_line
    from core.state import RunState

    status = os.path.join(str(tmp_path), "step-status.txt")
    dur = os.path.join(str(tmp_path), "step-duration.csv")
    with open(status, "w", encoding="utf-8") as fh:
        fh.write("0|DONE|2026-09-28 10:00:00|2026-09-28 10:00:46||0|0|0\n")
    st = RunState(status, dur)

    plain = finish_line(st, timedelta(seconds=60), datetime(2026, 9, 28, 10, 5, 0))
    assert plain.startswith("流水线全部步骤完成(本轮总耗时 ")
    assert "tokens" not in plain, "默认(extra 为空)时完成行必须与旧版一致"

    with_extra = finish_line(st, timedelta(seconds=60), datetime(2026, 9, 28, 10, 5, 0),
                             extra=" | tokens 1658 费用估算 0.0003 元")
    assert with_extra.startswith("流水线全部步骤完成(本轮总耗时 ")
    # extra 追加在括号内、作为最后一个度量段
    assert "| tokens 1658 费用估算 0.0003 元)" in with_extra
    assert with_extra.count("本轮总耗时") == 1
