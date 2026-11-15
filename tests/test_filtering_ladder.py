# 单元测试: judge_batch 阶梯批判定(batch_size>1, 2026-09-08 用户选定方案)
# 阶梯: 首轮过滤(batch_size) -> 集中重发(同批) -> 拆 2 条/批 -> 1 条/批逐条重试(max_tries)
# 关键语义: 批内漏判缺条(JSON 缺 index)**不默认保留**, 只送缺的那几条下档重判;
#           整批 LLM 失败送下一档; 1 条/批仍失败按 FILTER_JUDGE_FALLBACK 兜底。
from unittest import mock

from core.config import Config
from core.errors import LLMError
from core.llm import LLMClient
from steps._filtering import judge_batch


def _client() -> LLMClient:
    cfg = Config({})
    cfg.set("LLM_API_KEY", "k")
    return LLMClient(cfg)


def _recs(n: int) -> list[dict]:
    return [{"title": f"t{i}", "bullets": []} for i in range(n)]


def _full(keep_flags, base_reason="命中"):
    return {
        "judgments": [
            {"index": k, "keep": bool(keep_flags[k]),
             "reason": base_reason if keep_flags[k] else "标题命中硬排除词"}
            for k in range(len(keep_flags))
        ]
    }


def test_keep_string_booleans_and_null_are_tolerated():
    """回归 2026-09-13: LLM 把 keep 写成字符串/"null" 时判定方向必须正确。

    原实现 `bool(item.get("keep", True))` 会把字符串 "false" 判成 True(应剔除的被保留),
    把 null 判成 False(应保留的被误杀)。
    """
    client = _client()

    def fake(prompt, **kw):
        return {"judgments": [
            {"index": 0, "keep": "false", "reason": "标题命中硬排除词"},
            {"index": 1, "keep": "true", "reason": "命中相关标题词"},
            {"index": 2, "keep": "False", "reason": "配件词"},
            {"index": 3, "keep": None, "reason": ""},
            {"index": 4, "reason": "缺 key"},
        ]}

    with mock.patch.object(client, "chat_json", side_effect=fake):
        kept, dropped = judge_batch(client, {"category": "c"}, _recs(5), batch_size=5)

    assert sorted(r["title"] for r in dropped) == ["t0", "t2"]
    assert sorted(r["title"] for r in kept) == ["t1", "t3", "t4"]


def test_ladder_missing_entries_requeue_not_default_keep():
    """批内漏判缺条送下档(集中重发阶段逐条判), 不默认保留; 判定结果落到该条。"""
    client = _client()
    seq = {"n": 0}

    def flaky(prompt, **kw):
        seq["n"] += 1
        if seq["n"] == 1:                       # 第1批(t0..t3): 缺 index=3(漏判)
            return {"judgments": [
                {"index": 0, "keep": True, "reason": "命中"},
                {"index": 1, "keep": True, "reason": "命中"},
                {"index": 2, "keep": True, "reason": "命中"},
            ]}
        if seq["n"] in (2, 3):                  # 第2/3批完整保留
            return _full([True] * 4)
        if seq["n"] == 4:                       # 集中重发: 漏判的 t3 单独判 -> 剔除
            return {"judgments": [{"index": 0, "keep": False, "reason": "标题命中硬排除词"}]}
        raise AssertionError(f"不应有第 {seq['n']} 次调用")

    with mock.patch.object(client, "chat_json", side_effect=flaky):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(12), batch_size=4,
                                        max_tries=2)
    assert seq["n"] == 4                        # 3 批 + 1 条集中重发(未拆小/未逐条多轮)
    assert [r["title"] for r in dropped] == ["t3"]   # 漏判条被正确剔除而非默认保留
    assert len(kept) == 11
    assert all("兜底保留" not in r["reason"] for r in kept + dropped)
    assert dropped[0]["reason"] == "标题命中硬排除词"


def test_ladder_transport_failure_drains_to_single_then_fallback_keep():
    """整批持续失败: 集中重发/拆小后仍失败 -> 1 条/批逐条重试 max_tries, 仍失败兜底保留打标。"""
    client = _client()
    calls = {"n": 0}

    def boom(prompt, **kw):
        calls["n"] += 1
        raise LLMError("服务端无响应")

    with mock.patch.object(client, "chat_json", side_effect=boom):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(6), batch_size=5,
                                        max_tries=2)
    # 轮次: 首轮 size5: 2 批; 集中重发 size5: 2 批; 拆小 size2: 3 批; 1条/批: 6 条 × 2 次
    assert calls["n"] == 2 + 2 + 3 + 6 * 2
    assert len(kept) == 6
    assert dropped == []
    assert all("待复核" in r["reason"] for r in kept)   # 兜底保留打标


def test_ladder_single_phase_fallback_drop_configurable():
    """1 条/批阶段兜底方向可配: fallback_keep=False -> 剔除宁缺勿滥。"""
    client = _client()

    def boom(prompt, **kw):
        raise LLMError("服务端无响应")

    with mock.patch.object(client, "chat_json", side_effect=boom):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(3), batch_size=4,
                                        max_tries=2, fallback_keep=False)
    assert kept == []
    assert len(dropped) == 3
    assert all("兜底剔除" in r["reason"] for r in dropped)


def test_ladder_healthy_batches_single_pass_fast():
    """健康批一次通过: 调用次数 = 批数, 无降档/无漏判日志路径。"""
    client = _client()

    def ok(prompt, **kw):
        return _full([True] * 5)

    with mock.patch.object(client, "chat_json", side_effect=ok):
        kept, dropped = judge_batch(client, {"category": "c"}, _recs(12), batch_size=5)
    assert len(kept) == 12
    assert dropped == []
    assert all(r["reason"] == "命中" for r in kept)


def test_ladder_mixed_and_order_preserved():
    """混合判定(保留/剔除/漏判/失败): 输出顺序与输入一致, 失败条兜底不挤占他人。"""
    client = _client()
    seq = {"n": 0}

    def mixed(prompt, **kw):
        seq["n"] += 1
        if seq["n"] == 1:                       # 第1批: 完整混合判定
            return {"judgments": [
                {"index": 0, "keep": True, "reason": "命中"},
                {"index": 1, "keep": False, "reason": "标题命中硬排除词"},
                {"index": 2, "keep": True, "reason": "命中"},
                {"index": 3, "keep": False, "reason": "配件词"},
            ]}
        raise LLMError("第2批起服务端无响应")     # 剩余 4 条全部失败降档到逐条兜底

    with mock.patch.object(client, "chat_json", side_effect=mixed):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(8), batch_size=4,
                                        max_tries=1)
    # 批次: 首轮 2 批(size4, 第1批成功/第2批失败) -> 集中重发 1 批失败
    #       -> 拆小 2 批(size2)失败 -> 逐条 4 条 × 1 次
    assert seq["n"] == 2 + 1 + 2 + 4 * 1
    assert [r["title"] for r in kept] == ["t0", "t2", "t4", "t5", "t6", "t7"]
    assert [r["title"] for r in dropped] == ["t1", "t3"]
    assert "待复核" in kept[2]["reason"]        # t4 兜底保留打标
    assert all("兜底保留" not in r["reason"] for r in kept[:2] + dropped)


def test_concurrent_workers_same_result_as_serial():
    """workers>1 并发判定与串行结果一致(批间无依赖, 输出顺序仍按输入)。"""
    client = _client()

    def ok(prompt, **kw):
        # 每批返回的判定按批内 index: 偶数保留、奇数剔除
        n = prompt.count("标题:")
        return {"judgments": [
            {"index": i, "keep": i % 2 == 0,
             "reason": "命中" if i % 2 == 0 else "标题命中硬排除词"}
            for i in range(n)
        ]}

    serial_client = _client()
    with mock.patch.object(serial_client, "chat_json", side_effect=ok):
        kept_s, dropped_s = judge_batch(serial_client, {"category": "c"}, _recs(24),
                                        batch_size=4, workers=1)
    conc_client = _client()
    with mock.patch.object(conc_client, "chat_json", side_effect=ok):
        kept_c, dropped_c = judge_batch(conc_client, {"category": "c"}, _recs(24),
                                        batch_size=4, workers=3)

    assert [r["title"] for r in kept_s] == [r["title"] for r in kept_c]
    assert [r["title"] for r in dropped_s] == [r["title"] for r in dropped_c]
    # 输出顺序与输入一致(并发的 as_completed 乱序不能影响最终 keep/drop 顺序)
    assert [r["title"] for r in kept_c] == [f"t{i}" for i in range(24) if i % 2 == 0]


def test_concurrent_failure_still_falls_back():
    """并发下整批失败照样降档到逐条兜底, 不静默丢批。"""
    client = _client()

    def boom(prompt, **kw):
        raise LLMError("服务端无响应")

    with mock.patch.object(client, "chat_json", side_effect=boom):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(12),
                                        batch_size=4, workers=3, max_tries=2)
    # 首轮 3 批全失败 -> 集中重发 3 批全失败 -> 拆小 6 批全失败 -> 逐条 12×2 兜底保留
    assert len(kept) == 12
    assert dropped == []
    assert all("待复核" in r["reason"] for r in kept)
