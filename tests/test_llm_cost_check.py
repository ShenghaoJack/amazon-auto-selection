# 测试: 体检第 7 项「LLM 用量与费用」(Step 2)
#
# 契约(2026-09-28 用户定调): **只记录, 不告警** —— 恒返回 "OK", 不进 exit_code、
# 不推企业微信。费用高低是经营判断(模型单价会变、类目体量不同), 会误报的告警
# 会让人不再信任整套告警。
#
# 聚合口径: 按 run_id **累加 step-summary** → 同一轮被强杀后分多段跑完时,
# 每段各写一份同名步骤的 step-summary, 累加即得该步本轮总账。
import json
import os

import pytest

from core.config import Config
from core.paths import Paths
from core.usage import CSV_NAME, JSONL_NAME
from steps.base import StepContext
from steps.step17_backup_monitor import Step17BackupMonitor

RUN = "Cutting Boards-US-202608-20260923-200415"
OTHER = "Cutting Boards-US-202608-20260920-101010"


class _Log:
    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg): self.infos.append(str(msg))
    def warning(self, msg): self.warnings.append(str(msg))


def _meta(run: str, cat: str = "T", source: str = "官网实时(2026-09-28 10:28, CNY)") -> dict:
    return {"type": "meta", "run_id": run, "cat": cat,
            "price_source": source, "currency": "CNY"}


def _step(run: str, step: int, calls: int, cost: float, *, cat: str = "T",
          in_hit: int = 0, in_miss: int = 0, out: int = 0,
          unpriced: int = 0, failed: int = 0, status: str = "DONE") -> dict:
    return {"type": "step-summary", "run_id": run, "cat": cat, "step": step,
            "status": status, "calls": calls, "failed_attempts": failed,
            "in_hit": in_hit, "in_miss": in_miss, "out": out,
            "cost_cny": cost, "unpriced_calls": unpriced}


def _ctx(tmp_path, run: str = RUN) -> StepContext:
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T", "LLM_PRICES_MODE": "builtin"})
    ctx = StepContext(cfg, Paths(cfg), _Log())
    ctx.set_run_id(run)
    return ctx


def _write_ledger(ctx: StepContext, records: list[dict], name: str = JSONL_NAME) -> str:
    path = os.path.join(ctx.paths.monitor_dir(), name)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _check(ctx: StepContext) -> tuple[str, str]:
    return Step17BackupMonitor()._check_llm_cost(ctx)


# ------------------------------------------------------------------ 注册

def test_registered_as_check():
    """LLM 费用体检必须注册在 `_CHECKERS` 里。

    2026-10-08 起它**不再是最后一项** —— 后面加了「属性一致性」后置检查
    （把跨步骤的不变量挂进流水线，每次跑完自动报，不必等人发现）。
    """
    names = [n for n, _ in Step17BackupMonitor._CHECKERS]
    assert "LLM 用量与费用" in names
    assert "属性一致性" in names
    assert names[-1] == "属性一致性", "属性一致性应排在最后（它是收口检查）"
    assert len(names) == 8


def test_attribute_consistency_check_is_callable():
    """新加的后置检查必须**可调用**（只断言名字存在 = 假绿灯）。"""
    m = getattr(Step17BackupMonitor, "_check_attribute_consistency", None)
    assert callable(m), "缺 _check_attribute_consistency 实现"


# ------------------------------------------------------------------ 记录内容

def test_records_totals_across_segments(tmp_path):
    """同一轮的两段(同一步骤两份 step-summary)必须累加。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [
        _meta(RUN),
        _step(RUN, 7, calls=10, cost=0.001, in_hit=100, in_miss=200, out=50, failed=1),
        _step(RUN, 7, calls=5, cost=0.0005, in_hit=50, in_miss=100, out=25),   # 第二段
        _step(RUN, 17, calls=100, cost=2.5, in_hit=1000, in_miss=2000, out=500,
              unpriced=2, failed=3),
    ])
    level, msg = _check(ctx)

    assert level == "OK"
    assert "费用总2.5015元" in msg
    assert "115 次调用" in msg
    assert "4025 tokens" in msg                       # 1150 命中 + 2300 未命中 + 575 输出
    assert "步骤17=2.5000元" in msg
    assert "步骤07=0.0015元" in msg                   # 两段累加
    assert "价格来源=官网实时(2026-09-28 10:28, CNY)" in msg
    assert "未定价 2 次" in msg
    assert "失败尝试 4 次" in msg


def test_ignores_other_runs(tmp_path):
    """别的轮的账不得混进来(否则"本轮总账"就是假的)。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [
        _meta(RUN),
        _step(RUN, 17, calls=10, cost=1.0),
        _meta(OTHER),
        _step(OTHER, 17, calls=999, cost=99.0),
    ])
    _level, msg = _check(ctx)
    assert "费用总1.0000元" in msg
    assert "99" not in msg


def test_reads_rotated_volumes(tmp_path):
    """账本轮转后(只剩分卷)仍要能读到。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [_meta(RUN), _step(RUN, 17, calls=3, cost=0.5)],
                  name="llm-usage-20260101-000000-1.jsonl")
    level, msg = _check(ctx)
    assert level == "OK" and "费用总0.5000元" in msg


def test_never_warns_even_with_huge_cost(tmp_path):
    """关键回归: **只记录不告警** —— 再贵也必须是 OK。

    若将来有人给它加回阈值告警, 这个测试会立刻失败, 从而逼他确认口径变更。
    """
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [_meta(RUN), _step(RUN, 17, calls=100000, cost=99999.0)])
    level, msg = _check(ctx)
    assert level == "OK"
    assert "费用总99999.0000元" in msg


def test_never_warns_on_unpriced_or_fallback_prices(tmp_path):
    """未定价 / 价表兜底也只在文案里体现, 不升级为 WARN。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [
        _meta(RUN, source="内置快照(抓取失败且无缓存)"),
        _step(RUN, 17, calls=10, cost=0.0, unpriced=10),
    ])
    level, msg = _check(ctx)
    assert level == "OK"
    assert "未定价 10 次" in msg
    assert "内置快照" in msg


def test_failed_step_is_recorded(tmp_path):
    """失败步骤也烧了钱, 账里要能看到。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [_meta(RUN),
                        _step(RUN, 17, calls=8, cost=0.3, status="FAILED", failed=2)])
    level, msg = _check(ctx)
    assert level == "OK" and "步骤17=0.3000元" in msg


def test_records_text_and_vision_separately(tmp_path):
    """文本/图片两个模型必须分开报。

    图片 token 是服务端按尺寸折算后并进 `prompt_tokens` 的 → 只看合计永远看不出
    图片花了多少(而 `VISION_DETAIL`/`MAX_IMAGES` 正是靠这个数来调优的)。
    """
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [
        _meta(RUN),
        {**_step(RUN, 17, calls=2, cost=0.00348416, in_miss=2500, out=400),
         "by_channel": {
             "text": {"calls": 1, "in_miss": 500, "out": 100, "cost_cny": 0.00028416},
             "vision": {"calls": 1, "in_miss": 2000, "out": 300, "cost_cny": 0.0032},
         }},
    ])
    level, msg = _check(ctx)
    assert level == "OK"
    assert "文本 1次/0.0003元" in msg           # 总账行的通道拆分
    assert "视觉 1次/0.0032元" in msg
    assert "步骤17=0.0035元" in msg             # 逐步合计
    assert "[文本 0.0003 + 视觉 0.0032]" in msg  # 逐步明细的通道拆分


def test_single_channel_step_has_no_channel_suffix(tmp_path):
    """只有一个通道时不加多余的括号后缀(老文案不变)。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [
        _meta(RUN),
        {**_step(RUN, 7, calls=1, cost=0.0003, in_miss=500, out=100),
         "by_channel": {"text": {"calls": 1, "in_miss": 500, "out": 100,
                                 "cost_cny": 0.00028416}}},
    ])
    level, msg = _check(ctx)
    assert level == "OK"
    assert "[" not in msg
    assert "步骤07=0.0003元" in msg


# ------------------------------------------------------------------ 容错

def test_missing_ledger_is_graceful(tmp_path):
    """没有账本(未落盘/无 LLM 调用) → OK + 说明, 不抛。"""
    level, msg = _check(_ctx(tmp_path))
    assert level == "OK" and "账本不存在或为空" in msg


def test_empty_ledger_is_graceful(tmp_path):
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [])
    level, msg = _check(ctx)
    assert level == "OK" and "账本不存在或为空" in msg


def test_other_run_only_is_graceful(tmp_path):
    """账本里只有别的轮 → 明确说"本轮无记录", 不拿别人的数字充数。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [_meta(OTHER), _step(OTHER, 17, calls=5, cost=9.9)])
    level, msg = _check(ctx)
    assert level == "OK" and "本轮" in msg and "无 LLM 调用记录" in msg
    assert "9.9" not in msg


def test_corrupt_lines_are_skipped(tmp_path):
    """坏行跳过(与项目其它 JSONL 读取同口径)。"""
    ctx = _ctx(tmp_path)
    path = _write_ledger(ctx, [_meta(RUN), _step(RUN, 17, calls=2, cost=0.25)])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{ 这不是 JSON\n")
    level, msg = _check(ctx)
    assert level == "OK" and "费用总0.2500元" in msg


def test_step19_own_zero_row_is_not_noise(tmp_path):
    """步骤19 自己不做 LLM 调用, 账本里没有它的行 —— 不得因此报错或输出空明细。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [_meta(RUN), _step(RUN, 18, calls=4, cost=0.4)])
    level, msg = _check(ctx)
    assert level == "OK"
    assert "步骤18=0.4000元" in msg
    assert "步骤19" not in msg


def test_csv_name_is_not_mistaken_for_ledger(tmp_path):
    """同一目录里的 llm-usage-steps.csv 不该被当 JSONL 读(格式不同)。"""
    ctx = _ctx(tmp_path)
    _write_ledger(ctx, [_meta(RUN), _step(RUN, 7, calls=1, cost=0.1)])
    with open(os.path.join(ctx.paths.monitor_dir(), CSV_NAME), "w", encoding="utf-8") as fh:
        fh.write("步骤,状态,调用数\n07,DONE,1\n")
    level, msg = _check(ctx)
    assert level == "OK" and "费用总0.1000元" in msg
