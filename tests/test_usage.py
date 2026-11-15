# 单元测试: LLM 用量/费用记账 (UsageMeter)
#
# 全部离线: 不联网、不写真项目目录(用 tmp_path), 价格统一用内置快照
# (deepseek-flash 空闲 命中0.02/未命中1/输出4 元每百万; 高峰 ×2)。
import json
import os
import threading
from datetime import datetime

import pytest

from core.config import Config
from core.errors import LLMError, LLMTruncatedError
from core.llm import LLMClient
from core.paths import Paths
from core.prices import builtin_table
from core.usage import (CSV_HEADER, UsageMeter, build_meter, default_meter,
                        reset_default_meter)
from steps.base import StepContext

# 真实抓到的 usage 形状(2026-09-28 实测): 命中 1408 / 未命中 248 / 输出 2
USAGE = {
    "prompt_tokens": 1656,
    "completion_tokens": 2,
    "total_tokens": 1658,
    "prompt_tokens_details": {"cached_tokens": 1408},
    "prompt_cache_hit_tokens": 1408,
    "prompt_cache_miss_tokens": 248,
}
# 手算基准 = (1408*0.02 + 248*1.0 + 2*4.0) / 1e6
COST_OFF = 0.00028416
# 高峰 = (1408*0.04 + 248*2.0 + 2*8.0) / 1e6 = 恰好 2 倍
COST_PEAK = 0.00056832

MON_PEAK = datetime(2026, 9, 28, 10, 0)     # 周一 10:00 = 高峰
MON_OFF = datetime(2026, 9, 28, 13, 0)      # 周一 13:00 = 空闲


class _Log:
    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg): self.infos.append(str(msg))
    def warning(self, msg): self.warnings.append(str(msg))


def _meter(tmp_path=None, **kw) -> UsageMeter:
    return UsageMeter(builtin_table(), **kw)


@pytest.fixture(autouse=True)
def _clean_default_meter():
    reset_default_meter()
    yield
    reset_default_meter()


# ------------------------------------------------------------------ 累加与分组

def test_groups_by_channel_and_model():
    m = _meter()
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    m.record(USAGE, channel="text", model="deepseek-v4-pro", at=MON_OFF)
    m.record(USAGE, channel="vision", model="deepseek-v4-flash-vision-exp", at=MON_OFF)
    groups = m.snapshot()["groups"]
    assert set(groups) == {
        "text/deepseek-flash", "text/deepseek-v4-pro",
        "vision/deepseek-v4-flash-vision-exp",
    }
    assert m.totals()["calls"] == 3
    assert m.totals()["in_hit"] == 1408 * 3


def test_cost_matches_hand_calculation():
    """费用必须逐项按"命中/未命中/输出"三个价算, 精确到 1e-9。"""
    m = _meter()
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    assert m.totals()["cost_cny"] == pytest.approx(COST_OFF, abs=1e-9)
    assert m.totals()["total_tokens"] == 1658


def test_pro_cost_uses_pro_prices():
    m = _meter()
    m.record(USAGE, channel="text", model="deepseek-v4-pro", at=MON_OFF)
    expect = (1408 * 0.15 + 248 * 4.5 + 2 * 13.5) / 1e6
    assert m.totals()["cost_cny"] == pytest.approx(expect, abs=1e-9)


def test_cache_hits_are_cheaper_regression():
    """回归保护: **绝不能只按 prompt_tokens 计价**。

    同样 1656 个输入 token, 全未命中 与 命中 1408 的费用差 5.8 倍;
    若按 prompt_tokens 一刀切, 高缓存命中的流水线会被严重高估。
    """
    allmiss = _meter()
    allmiss.record({"prompt_tokens": 1656, "completion_tokens": 2}, channel="text",
                   model="deepseek-flash", at=MON_OFF)
    cached = _meter()
    cached.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    a, b = allmiss.totals()["cost_cny"], cached.totals()["cost_cny"]
    assert a == pytest.approx((1656 * 1.0 + 2 * 4.0) / 1e6, abs=1e-9)
    assert b < a / 5


def test_peak_is_double_offpeak():
    off, peak = _meter(), _meter()
    off.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    peak.record(USAGE, channel="text", model="deepseek-flash", at=MON_PEAK)
    assert peak.totals()["cost_cny"] == pytest.approx(COST_PEAK, abs=1e-9)
    assert peak.totals()["cost_cny"] == pytest.approx(2 * off.totals()["cost_cny"], abs=1e-9)
    assert peak.totals()["peak_calls"] == 1 and off.totals()["peak_calls"] == 0


def test_unpriced_model_counts_but_costs_zero():
    """未定价模型(deepseek-chat) → 计入调用数但费用标未定价, 不静默按 0 蒙混。"""
    log = _Log()
    m = _meter(log=log)
    m.record(USAGE, channel="text", model="deepseek-chat", at=MON_OFF)
    t = m.totals()
    assert t["calls"] == 1 and t["cost_cny"] == 0.0 and t["unpriced_calls"] == 1
    assert any("未定价" in w for w in log.warnings)
    assert "未定价" in m.as_text()


# ------------------------------------------------------------------ 与 core/llm.py 的接线

class _Resp:
    status_code = 200

    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


class _FakeHTTP:
    def __init__(self, payload): self._payload = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _Resp(self._payload)


def _payload(finish_reason="stop", usage=USAGE):
    return {"choices": [{"message": {"content": '{"ok": 1}'},
                         "finish_reason": finish_reason}], "usage": usage}


def _client(tmp_path, meter=None):
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T",
                  "LLM_API_KEY": "k", "VISION_API_KEY": "k", "LLM_MODEL": "deepseek-flash"})
    return LLMClient(cfg, meter=meter or _meter())


def test_request_returns_content_and_finish_reason(tmp_path, monkeypatch):
    """埋点不得改变 `_request` 的返回契约(调用方按二元组解包)。"""
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _FakeHTTP(_payload()))
    out = _client(tmp_path)._request("https://x", "k", "deepseek-flash",
                                     [{"role": "user", "content": "hi"}], 0.2, 100, True)
    assert isinstance(out, tuple) and len(out) == 2
    assert out[0] == '{"ok": 1}' and out[1] == "stop"


def test_successful_call_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _FakeHTTP(_payload()))
    meter = _meter()
    client = _client(tmp_path, meter)
    client.chat_json("判定", max_tokens=100)
    t = meter.totals()
    assert t["calls"] == 1
    assert t["in_hit"] == 1408 and t["in_miss"] == 248 and t["out"] == 2


def test_truncated_attempts_are_all_counted(tmp_path, monkeypatch):
    """回归保护: `finish_reason=length` 的响应**确实花了钱**, 必须计入。

    它会被丢弃后升 max_tokens 重试 —— 若不记, "升档重试"的浪费会被系统性漏掉。
    链路: 4000 截断 → 8000 截断 → 到 384K 上限抛 LLMTruncatedError, 共 3 次计费调用。
    """
    meter = _meter()
    client = _client(tmp_path, meter)
    monkeypatch.setattr("httpx.Client",
                        lambda *a, **k: _FakeHTTP(_payload(finish_reason="length")))
    monkeypatch.setattr(client, "_cool_down", lambda *a: None)
    with pytest.raises(LLMTruncatedError):
        client.chat_json("判定", max_tokens=4000)
    t = meter.totals()
    assert t["calls"] == 3, "三次被截断的调用都产生了费用, 必须都记上"
    assert t["in_hit"] == 1408 * 3


def test_http_error_counts_failure_and_no_tokens(tmp_path, monkeypatch):
    """失败尝试没有 token 消耗, 只计失败次数(供"重试浪费"统计)。"""
    meter = _meter()
    client = _client(tmp_path, meter)

    def boom(*a, **k):
        raise LLMError("LLM 限流(429), 冷却后重试")

    monkeypatch.setattr(client, "_post_with_watchdog", boom)
    with pytest.raises(LLMError):
        client._request("https://x", "k", "deepseek-flash",
                        [{"role": "user", "content": "hi"}], 0.2, 100, True)
    t = meter.totals()
    assert t["calls"] == 0 and t["total_tokens"] == 0
    assert t["failed_attempts"] == 1


def test_missing_usage_does_not_crash(tmp_path, monkeypatch):
    """响应里没有 usage 时必须容错(不能因为监控把调用搞失败)。"""
    monkeypatch.setattr("httpx.Client",
                        lambda *a, **k: _FakeHTTP(_payload(usage=None)))
    meter = _meter()
    assert _client(tmp_path, meter).chat_json("x", max_tokens=100) == {"ok": 1}
    assert meter.totals()["no_usage_calls"] == 1


# ------------------------------------------------------------------ 并发

def test_thread_safety():
    """进程内多线程并发调 LLM(ATTR_STAB_WORKERS/FILTER_JUDGE_WORKERS) → 累加不能丢。"""
    m = _meter()
    one = {"prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 20,
           "completion_tokens": 5}

    def work():
        for _ in range(100):
            m.record(one, channel="text", model="deepseek-flash", at=MON_OFF)

    threads = [threading.Thread(target=work) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tot = m.totals()
    assert tot["calls"] == 2000
    assert tot["in_hit"] == 20_000 and tot["in_miss"] == 40_000 and tot["out"] == 10_000


# ------------------------------------------------------------------ meter 归属

def _ctx(tmp_path, cat="T"):
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": cat,
                  "LLM_PRICES_MODE": "builtin",      # builtin = 绝不联网
                  "LLM_API_KEY": "k", "LLM_MODEL": "deepseek-flash"})
    paths = Paths(cfg)
    return cfg, StepContext(cfg, paths, _Log())


def test_two_step_contexts_do_not_mix(tmp_path):
    """两个 StepContext 各持自己的 meter —— 账不能串。"""
    _, c1 = _ctx(tmp_path)
    _, c2 = _ctx(tmp_path)
    c1.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    c2.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    c2.usage().record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    assert c1.usage().totals()["calls"] == 1
    assert c2.usage().totals()["calls"] == 2


def test_step_context_llm_reports_into_its_own_meter(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _FakeHTTP(_payload()))
    _, ctx = _ctx(tmp_path)
    ctx.begin_step(7)
    ctx.llm().chat_json("x", max_tokens=100)
    assert ctx.usage().totals()["calls"] == 1


def test_direct_client_falls_back_to_default_meter(tmp_path, monkeypatch):
    """`scripts/*.py` 里直接 `LLMClient(config)` 的入口也要落到账上(不丢账)。"""
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _FakeHTTP(_payload()))
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T",
                  "LLM_API_KEY": "k", "LLM_MODEL": "deepseek-flash"})
    client = LLMClient(cfg)
    assert client.meter() is default_meter()
    client.chat_json("x", max_tokens=100)
    assert default_meter().totals()["calls"] == 1


# ------------------------------------------------------------------ 落盘

def test_jsonl_meta_then_calls_then_step_summary(tmp_path):
    path = str(tmp_path / "llm-usage.jsonl")
    m = _meter(jsonl_path=path, cat="T")
    m.begin_step(7)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    m.finish_step(7, "DONE")
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    assert rows[0]["type"] == "meta"
    assert rows[0]["price_source"] == m.prices.source
    assert rows[0]["prices"]["deepseek-flash"]["off"]["out"] == 4.0
    kinds = [r["type"] for r in rows]
    assert kinds[0] == "meta" and "call" in kinds and kinds[-1] == "step-summary"
    call = next(r for r in rows if r["type"] == "call")
    assert call["step"] == 7 and call["channel"] == "text"
    assert call["in_hit"] == 1408 and call["priced"] is True
    summary = rows[-1]
    assert summary["calls"] == 1 and summary["status"] == "DONE"


def test_step_line_and_csv(tmp_path):
    """CSV 是**长表**: 每 (步骤 × 通道 × 模型) 一行 —— 文本与图片必须分开。"""
    path = str(tmp_path / "llm-usage.jsonl")
    csv_path = str(tmp_path / "llm-usage-steps.csv")
    log = _Log()
    m = _meter(jsonl_path=path, csv_path=csv_path, cat="T", log=log)
    for num in (7, 8):
        m.begin_step(num)
        m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
        text = m.finish_step(num, "DONE")
        assert "tokens 1658" in text and "费用估算" in text
    raw = open(csv_path, encoding="utf-8-sig").read().strip().splitlines()
    assert raw[0].split(",") == CSV_HEADER
    assert CSV_HEADER[:4] == ["步骤", "状态", "通道", "模型"]
    assert len(raw) == 3
    assert raw[1].startswith("07,DONE,text,deepseek-flash,1,0,1408,248,2,")
    assert raw[2].startswith("08,DONE,text,deepseek-flash,1,0,1408,248,2,")
    assert any("步骤 07 LLM 用量" in x for x in log.infos)


def test_rerun_appends_and_keeps_history(tmp_path):
    """append-only: 重跑不覆盖上一轮的账(明细保留才能事后换价重算)。"""
    path = str(tmp_path / "llm-usage.jsonl")
    for run_id in ("run-a", "run-b"):
        m = _meter(jsonl_path=path, cat="T", run_id=run_id)
        m.begin_step(7)
        m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
        m.finish_step(7, "DONE")
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    assert sum(1 for r in rows if r["type"] == "meta") == 2
    assert sum(1 for r in rows if r["type"] == "call") == 2
    assert {r["run_id"] for r in rows if r["type"] == "call"} == {"run-a", "run-b"}


def test_failed_step_still_logs_usage(tmp_path):
    """失败的步骤也打用量行 —— 失败重跑的成本不能丢。"""
    log = _Log()
    m = _meter(jsonl_path=str(tmp_path / "u.jsonl"), cat="T", log=log)
    m.begin_step(17)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    m.record_failure(channel="text", model="deepseek-flash", reason="timeout")
    text = m.finish_step(17, "FAILED")
    assert "失败尝试 1 次" in text
    assert any("步骤 17 LLM 用量" in x for x in log.infos)


def test_disabled_meter_writes_nothing(tmp_path):
    """dry-run: 不累加、不落盘, 避免 Mock 数据污染真实账。"""
    path = str(tmp_path / "llm-usage.jsonl")
    m = UsageMeter(builtin_table(), enabled=False, jsonl_path=path, cat="T")
    m.begin_step(7)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    assert m.finish_step(7, "DONE") == ""
    assert m.totals()["calls"] == 0
    assert not (tmp_path / "llm-usage.jsonl").exists()


def test_build_meter_dry_run_is_disabled(tmp_path):
    cfg = Config({"DATA_ROOT": str(tmp_path), "CAT": "T",
                  "LLM_PRICES_MODE": "builtin", "DRY_RUN": True})
    m = build_meter(cfg, Paths(cfg), log=_Log())
    assert m.enabled is False


def test_run_summary_is_appended(tmp_path):
    path = str(tmp_path / "llm-usage.jsonl")
    m = _meter(jsonl_path=path, cat="T")
    m.begin_step(7)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    m.finish_step(7, "DONE")
    m.write_run_summary()
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    assert rows[-1]["type"] == "run-summary"
    assert rows[-1]["total"]["calls"] == 1


# ------------------------------------------------------------------ 账本轮转

def test_ledger_rotates_when_over_cap(tmp_path):
    """超过 LLM_USAGE_MAX_MB → 旧账轮转为 llm-usage-<run_id>.jsonl, 新账重新开始。

    账本是 append-only 且位于**被归档的监控目录**里 → 每轮归档都会复制一份, 不轮转会一直长。
    """
    path = str(tmp_path / "llm-usage.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x" * 4096 + "\n")                      # 4KB 旧账
    m = _meter(jsonl_path=path, cat="T", run_id="R-ROT",
               max_mb=0.0005, keep=5)                    # cap ≈ 512B
    m.begin_step(7)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)

    rotated = tmp_path / "llm-usage-R-ROT.jsonl"
    assert rotated.exists(), "超限应已轮转"
    assert rotated.read_text(encoding="utf-8").startswith("xxxx")
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    assert rows[0]["type"] == "meta" and rows[0]["run_id"] == "R-ROT"
    assert any(r["type"] == "call" for r in rows)


def test_rotation_disabled_by_default(tmp_path):
    """max_mb=0(默认) → 不搬动文件, 旧账继续追加。"""
    path = str(tmp_path / "llm-usage.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x" * 4096 + "\n")
    m = _meter(jsonl_path=path, cat="T", run_id="R-NO-ROT")
    m.begin_step(7)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    assert not (tmp_path / "llm-usage-R-NO-ROT.jsonl").exists()
    content = open(path, encoding="utf-8").read()
    assert content.startswith("x" * 4096), "旧账应原地保留"
    assert '"type": "call"' in content, "新记录应追加在同一文件"


def test_rotation_keeps_only_n_volumes(tmp_path):
    """只保留最近 keep 份轮转分卷, 最旧的删掉。"""
    from core.usage import rotate_if_needed

    main = tmp_path / "llm-usage.jsonl"
    for i, name in enumerate(("a", "b", "c")):
        p = tmp_path / f"llm-usage-{name}.jsonl"
        p.write_text("old\n", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))                # a 最旧
    main.write_text("y" * 4096, encoding="utf-8")

    assert rotate_if_needed(str(main), 0.0005, keep=2, tag="new") is True
    left = sorted(p.name for p in tmp_path.glob("llm-usage-*.jsonl"))
    assert len(left) == 2
    assert "llm-usage-a.jsonl" not in left, "最旧的分卷应被清理"
    assert "llm-usage-new.jsonl" in left
    # 旧账整体搬进分卷(不丢数据); 主账本由下一次写入重新创建
    assert (tmp_path / "llm-usage-new.jsonl").read_text(encoding="utf-8").startswith("y" * 100)


def test_rotation_noop_below_cap(tmp_path):
    from core.usage import rotate_if_needed

    main = tmp_path / "llm-usage.jsonl"
    main.write_text("small\n", encoding="utf-8")
    assert rotate_if_needed(str(main), 20, keep=5, tag="t") is False
    assert main.exists()


# ------------------------------------------------------------------ 账本读取

def test_summarize_run_sums_segments_and_keeps_int_counts(tmp_path):
    """按 run_id 累加 step-summary; 计数字段必须是 int(不是 2.0 这种浮点)。"""
    from core.usage import summarize_run

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T", "price_source": "内置快照"},
        {"type": "step-summary", "run_id": "R", "step": 7, "calls": 2,
         "failed_attempts": 1, "in_hit": 10, "in_miss": 20, "out": 5,
         "cost_cny": 0.1, "unpriced_calls": 1},
        {"type": "step-summary", "run_id": "R", "step": 7, "calls": 3,
         "failed_attempts": 0, "in_hit": 5, "in_miss": 10, "out": 2,
         "cost_cny": 0.05, "unpriced_calls": 0},
        {"type": "step-summary", "run_id": "OTHER", "step": 7, "calls": 99,
         "cost_cny": 99.0},
    ]
    agg = summarize_run(recs, "R")
    t = agg["totals"]
    assert t["calls"] == 5 and isinstance(t["calls"], int)
    assert t["failed_attempts"] == 1 and isinstance(t["failed_attempts"], int)
    assert t["unpriced_calls"] == 1 and isinstance(t["unpriced_calls"], int)
    assert t["total_tokens"] == 52
    assert t["cost_cny"] == pytest.approx(0.15, abs=1e-9)
    assert agg["steps"][7]["calls"] == 5
    assert agg["price_source"] == "内置快照"


def _call(run: str, step: int, *, hit: int = 0, miss: int = 100, out: int = 10,
          cost: float = 0.02, ts: str = "2026-09-29T10:00:00+08:00",
          channel: str = "text", model: str = "deepseek-flash",
          peak: bool = False, priced: bool = True) -> dict:
    return {"type": "call", "run_id": run, "cat": "T", "step": step, "ts": ts,
            "channel": channel, "model": model, "in_hit": hit, "in_miss": miss,
            "out": out, "total": hit + miss + out, "cost_cny": cost,
            "peak": peak, "priced": priced, "pid": 1}


def _summary(run: str, step: int, calls: int, *, hit: int = 0, miss: int = 100,
             out: int = 10, cost: float = 0.02, status: str = "DONE",
             ts: str = "2026-09-29T10:00:00+08:00", pid: int = 1,
             failed: int = 0) -> dict:
    return {"type": "step-summary", "run_id": run, "cat": "T", "step": step, "ts": ts,
            "status": status, "calls": calls, "failed_attempts": failed,
            "in_hit": hit, "in_miss": miss, "out": out, "cost_cny": cost,
            "pid": pid,
            "by_channel": {"text": {"calls": calls, "in_hit": hit, "in_miss": miss,
                                    "out": out, "cost_cny": cost, "failed_attempts": failed}}}


# ---- 类目累计口径(2026-09-29 用户定稿: 历史 + 本次叠加, 永不因重置清零) ----

def test_summarize_category_sums_across_run_ids():
    """累计口径**不按 run_id 过滤**: 跨轮次/跨重置叠加, 本轮量另放 run_totals。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R1", "cat": "T", "price_source": "内置快照"},
        _summary("R1", 10, 3), _call("R1", 10, miss=100),
        _summary("R2", 10, 5), _call("R2", 10, miss=100),
    ]
    agg = summarize_category(recs)
    assert agg["totals"]["calls"] == 8          # 3(历史轮) + 5(本轮)
    assert agg["steps"][10]["calls"] == 8
    assert agg["run_totals"]["calls"] == 5      # 本轮 = 最后一次出现的 run_id
    assert agg["run_id"] == "R2"
    assert agg["runs"] == ["R1", "R2"]


def test_summarize_category_counts_orphan_calls_from_killed_step():
    """被强杀/中断那一步没有 step-summary, 只有即时落盘的 call 记录 → 也必须计入累计。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T"},
        # 步骤10: 上一次尝试跑完(汇总 2 次), 本次被强杀(额外 3 条 call 记录, 无汇总)
        _summary("R", 10, 2, miss=100),
        _call("R", 10, miss=100), _call("R", 10, miss=100),
        _call("R", 10, miss=100), _call("R", 10, miss=100), _call("R", 10, miss=100),
    ]
    agg = summarize_category(recs)
    assert agg["steps"][10]["calls"] == 5       # 取 call 侧(更全), 不是 2
    assert agg["totals"]["calls"] == 5
    assert agg["orphan_calls"] == 3             # 被强杀那 3 次调用(已计入, 可提示)
    assert agg["steps"][10]["attempts"] == 2    # 1 次有汇总 + 1 次只有记录


def test_summarize_category_legacy_ledger_without_call_records():
    """老账本(2026-09-28 之前只有 step-summary, 没有 call 记录)不能被丢掉。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T"},
        _summary("R", 3, 7, miss=200, cost=0.5),
    ]
    agg = summarize_category(recs)
    assert agg["steps"][3]["calls"] == 7
    assert agg["totals"]["total_tokens"] == 210     # 该条 summary: 命中0 + 未命中200 + 输出10
    assert agg["totals"]["cost_cny"] == pytest.approx(0.5, abs=1e-9)
    assert agg["orphan_calls"] == 0


def test_summarize_category_does_not_double_count_matching_sources():
    """同一批调用既在 call 记录又在 summary 里时, 只能算一次(取 max, 不是相加)。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T"},
        _summary("R", 7, 4, miss=100, cost=0.08),
        *[_call("R", 7, miss=100, cost=0.02) for _ in range(4)],
    ]
    agg = summarize_category(recs)
    assert agg["totals"]["calls"] == 4
    assert agg["totals"]["cost_cny"] == pytest.approx(0.08, abs=1e-9)
    assert agg["orphan_calls"] == 0


def test_summarize_category_keeps_failed_attempts_and_counts_int():
    """失败尝试只在 summary 里计数(失败不写 call 明细行) → 不能丢, 且计数为 int。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T"},
        _summary("R", 7, 1, failed=3),
        _call("R", 7),
    ]
    agg = summarize_category(recs)
    assert agg["totals"]["failed_attempts"] == 3
    assert isinstance(agg["totals"]["failed_attempts"], int)
    assert isinstance(agg["steps"][7]["calls"], int)
    assert agg["steps"][7]["total_tokens"] == 110


def test_summarize_category_step_metadata_for_panel():
    """每步带 status / attempts / last_ts(最近一次尝试时间), 供看板说明"这次还是历史"。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T"},
        _summary("R", 10, 1, ts="2026-09-29T06:10:28+08:00"),
        _summary("R", 10, 1, ts="2026-09-29T16:11:00+08:00", status="FAILED"),
        _call("R", 10, ts="2026-09-29T16:11:30+08:00"),
    ]
    agg = summarize_category(recs)
    row = agg["steps"][10]
    assert row["attempts"] == 2
    assert row["last_ts"] == "2026-09-29T16:11:30+08:00"
    assert row["status"] == "FAILED"


def test_summarize_category_channel_split_and_unpriced():
    """通道/模型拆分与未定价计数照旧可用(call 记录侧推导)。"""
    from core.usage import summarize_category

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T", "currency": "CNY"},
        _call("R", 17, channel="text", miss=10, cost=0.01),
        _call("R", 17, channel="vision", miss=20, cost=0.02, priced=False),
    ]
    agg = summarize_category(recs)
    assert set(agg["totals_by_channel"]) == {"text", "vision"}
    assert agg["totals_by_channel"]["vision"]["calls"] == 1
    assert agg["totals"]["unpriced_calls"] == 1
    assert agg["totals_by_model"]["text/deepseek-flash"]["calls"] == 1
    assert agg["currency"] == "CNY"
    assert agg["steps"][17]["by_channel"]["vision"]["cost_cny"] == pytest.approx(0.02)


def test_summarize_category_call_without_step_goes_to_bucket():
    """独立脚本直连 meter 的调用(step=null)不能丢: 归到 -1, 仍进累计。"""
    from core.usage import summarize_category

    recs = [{"type": "meta", "run_id": "R", "cat": "T"},
            {"type": "call", "run_id": "R", "cat": "T", "step": None, "in_miss": 5,
             "out": 1, "cost_cny": 0.001, "channel": "text", "model": "deepseek-flash"}]
    agg = summarize_category(recs)
    assert agg["totals"]["calls"] == 1
    assert agg["steps"][-1]["calls"] == 1


def test_summarize_category_empty_ledger():
    from core.usage import summarize_category

    agg = summarize_category([])
    assert agg["totals"]["calls"] == 0
    assert agg["steps"] == {} and agg["run_totals"]["calls"] == 0


def test_summarize_runs_sorted_by_cost(tmp_path):
    from core.usage import summarize_runs

    recs = [
        {"type": "step-summary", "run_id": "cheap", "cat": "T", "step": 7,
         "calls": 1, "cost_cny": 0.1},
        {"type": "step-summary", "run_id": "pricey", "cat": "T", "step": 7,
         "calls": 1, "cost_cny": 9.9},
    ]
    runs = summarize_runs(recs)
    assert [r["run_id"] for r in runs] == ["pricey", "cheap"]


def test_ledger_paths_includes_rotated_volumes(tmp_path):
    from core.usage import ledger_paths

    (tmp_path / "llm-usage.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "llm-usage-20260101.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "llm-usage-steps.csv").write_text("", encoding="utf-8")
    names = [os.path.basename(p) for p in ledger_paths(str(tmp_path))]
    assert names[0] == "llm-usage.jsonl", "主账本优先"
    assert "llm-usage-20260101.jsonl" in names
    assert "llm-usage-steps.csv" not in names, "CSV 不是账本"


# ------------------------------------------------------------------ 按通道/模型拆分
#
# 项目有**两个模型通道**: 文本(LLM_MODEL)与图片(VISION_MODEL), 两个 key、两个端点,
# 单价也可能不同; 图片 token 还是服务端按尺寸折算后并进 prompt_tokens 的 ——
# **只看合计永远看不出图片花了多少**。下面锁死"拆分必须贯穿 summary/CSV/日志"。

VUSAGE = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 2000,
          "completion_tokens": 300}
VMODEL = "deepseek-v4-flash-vision-exp"          # 官方: 旧名, 按 flash 价计费
COST_VISION = 0.0032                             # (2000*1.0 + 300*4.0) / 1e6


def _two_channel_meter(**kw):
    log = _Log()
    m = _meter(log=log, **kw)
    m.begin_step(17)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    m.record(VUSAGE, channel="vision", model=VMODEL, at=MON_OFF)
    return m, log


def test_step_line_splits_two_channels():
    m, _log = _two_channel_meter()
    text = m.finish_step(17, "DONE")
    assert "调用 2 次(文本 1/视觉 1)" in text
    assert "文本 0.0003" in text and "视觉 0.0032" in text
    assert "0.0035" in text                      # 合计 = 0.00348416


def test_step_line_single_channel_stays_terse():
    """只有一个通道时不刷多余的括号, 保持老格式。"""
    m = _meter()
    m.begin_step(7)
    m.record(USAGE, channel="text", model="deepseek-flash", at=MON_OFF)
    text = m.finish_step(7, "DONE")
    assert "(" not in text.split("tokens")[0]
    assert "费用估算 0.0003 元" in text


def test_csv_one_row_per_channel_and_model(tmp_path):
    csv_path = str(tmp_path / "s.csv")
    m, _log = _two_channel_meter(csv_path=csv_path)
    m.finish_step(17, "DONE")
    rows = open(csv_path, encoding="utf-8-sig").read().strip().splitlines()[1:]
    assert len(rows) == 2, "一个步骤用了两个通道 → 两行"
    assert rows[0].startswith("17,DONE,vision,deepseek-v4-flash-vision-exp,1,")
    assert rows[1].startswith("17,DONE,text,deepseek-flash,1,"), "按费用降序应在后"


def test_snapshot_has_by_channel_and_reconciles():
    m, _log = _two_channel_meter()
    by = m.totals_by_channel()
    assert set(by) == {"text", "vision"}
    assert by["vision"]["calls"] == 1
    assert by["vision"]["cost_cny"] == pytest.approx(COST_VISION, abs=1e-9)
    tot = m.totals()
    # 通道合计必须等于总账(可对账)
    assert sum(v["calls"] for v in by.values()) == tot["calls"]
    assert sum(v["cost_cny"] for v in by.values()) == pytest.approx(tot["cost_cny"], abs=1e-9)
    assert sum(v["total_tokens"] for v in by.values()) == tot["total_tokens"]


def test_step_summary_jsonl_carries_split(tmp_path):
    path = str(tmp_path / "u.jsonl")
    m, _log = _two_channel_meter(jsonl_path=path)
    m.finish_step(17, "DONE")
    rec = next(r for r in (json.loads(x) for x in open(path, encoding="utf-8") if x.strip())
               if r["type"] == "step-summary")
    assert set(rec["by_channel"]) == {"text", "vision"}
    assert rec["by_channel"]["vision"]["cost_cny"] == pytest.approx(COST_VISION, abs=1e-9)
    assert rec["by_model"][f"vision/{VMODEL}"]["calls"] == 1
    assert rec["by_model"]["text/deepseek-flash"]["calls"] == 1
    assert rec["calls"] == 2, "合并口径仍在 → 老读者不受影响"


def test_summarize_run_splits_channels_across_segments():
    """跨进程段也要按通道累加(同一轮分多段跑完时)。"""
    from core.usage import summarize_run

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T", "price_source": "x"},
        {"type": "step-summary", "run_id": "R", "step": 17, "status": "DONE", "calls": 2,
         "in_miss": 2500, "out": 400, "cost_cny": 0.00348416,
         "by_channel": {"text": {"calls": 1, "in_miss": 500, "out": 100, "cost_cny": 0.00028416},
                        "vision": {"calls": 1, "in_miss": 2000, "out": 300, "cost_cny": 0.0032}},
         "by_model": {"text/deepseek-flash": {"calls": 1, "cost_cny": 0.00028416},
                      f"vision/{VMODEL}": {"calls": 1, "cost_cny": 0.0032}}},
        {"type": "step-summary", "run_id": "R", "step": 17, "status": "DONE", "calls": 1,
         "in_miss": 100, "out": 10, "cost_cny": 0.00014,
         "by_channel": {"vision": {"calls": 1, "in_miss": 100, "out": 10, "cost_cny": 0.00014}},
         "by_model": {f"vision/{VMODEL}": {"calls": 1, "cost_cny": 0.00014}}},
    ]
    agg = summarize_run(recs, "R")
    ch = agg["totals_by_channel"]
    assert ch["text"]["calls"] == 1
    assert ch["vision"]["calls"] == 2, "第二段的视觉调用必须并入"
    assert ch["vision"]["cost_cny"] == pytest.approx(0.00334, abs=1e-9)
    assert agg["steps"][17]["by_channel"]["vision"]["calls"] == 2
    assert agg["totals"]["calls"] == 3
    assert sum(v["calls"] for v in ch.values()) == agg["totals"]["calls"]


def test_summarize_run_legacy_record_without_channel_goes_to_unknown():
    """老账本(Step 1/2 早期写的, 无 by_channel) → 归到 unknown, 保证通道合计仍等于总账。"""
    from core.usage import summarize_run

    recs = [
        {"type": "meta", "run_id": "R", "cat": "T"},
        {"type": "step-summary", "run_id": "R", "step": 7, "calls": 5,
         "in_miss": 500, "out": 50, "cost_cny": 0.0015},
    ]
    agg = summarize_run(recs, "R")
    assert set(agg["totals_by_channel"]) == {"unknown"}
    assert agg["totals_by_channel"]["unknown"]["calls"] == 5
    assert agg["totals_by_channel"]["unknown"]["cost_cny"] == pytest.approx(0.0015, abs=1e-9)
    assert agg["steps"][7]["by_channel"]["unknown"]["calls"] == 5
    assert agg["totals"]["calls"] == 5
