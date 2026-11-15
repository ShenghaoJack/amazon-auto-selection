# 单元测试: judge_batch 逐条判定模式(batch_size<=1, 显式配置)
# 2026-09-08: 默认改为阶梯批(见 test_filtering_ladder.py); 显式 BATCH_FILTER<=1 时全逐条:
# 每条失败/解析缺失独立重试(默认3次), 不再默认保留; 多次仍失败按兜底策略。
from unittest import mock

from core.config import Config
from core.errors import LLMError
from core.llm import LLMClient
from steps._filtering import judge_batch


def _client() -> LLMClient:
    cfg = Config({})
    cfg.set("LLM_API_KEY", "k")
    return LLMClient(cfg)


def _recs(n: int = 3) -> list[dict]:
    return [{"title": f"t{i}", "bullets": []} for i in range(n)]


def test_config_default_batch_filter_is_ladder_start():
    """BATCH_FILTER 配置默认 = 10(阶梯批起点), 新类目/缺失键不再退回逐条或 15。"""
    cfg = Config({})
    assert int(cfg.get("BATCH_FILTER") or 0) == 10


def test_single_item_mode_one_call_per_record():
    """显式 batch_size<=1(逐条): 每条一次调用, 判定结果按序落位。"""
    client = _client()
    calls = []

    def ok(prompt, **kw):
        title = "t" + (prompt.split("标题: t")[1][0] if "标题: t" in prompt else "?")
        idx = int(title[1]) if title[1:].isdigit() else 0
        keep = idx % 2 == 0
        calls.append(idx)
        return {"judgments": [{"index": 0, "keep": keep, "reason": "命中" if keep else "剔除"}]}

    with mock.patch.object(client, "chat_json", side_effect=ok):
        kept, dropped = judge_batch(client, {"category": "c"}, _recs(4), batch_size=1)
    assert len(calls) == 4                       # 逐条 4 次, 非 1 批
    assert [r["title"] for r in kept] == ["t0", "t2"]
    assert [r["title"] for r in dropped] == ["t1", "t3"]
    assert all("兜底保留" not in r["reason"] for r in kept + dropped)


def test_single_retries_on_parse_missing_then_succeeds():
    """解析缺失(judgments 空)不算判定结果: 该条独立重试, 不默认保留。"""
    client = _client()
    seq = {"n": 0}

    def flaky(prompt, **kw):
        seq["n"] += 1
        if seq["n"] == 1:                        # 第1条: 返回空 judgments(漏判场景)
            return {"judgments": []}
        return {"judgments": [{"index": 0, "keep": False, "reason": "标题命中硬排除词"}]}

    with mock.patch.object(client, "chat_json", side_effect=flaky):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(1),
                                        batch_size=1, max_tries=3)
    assert seq["n"] == 2                         # 1 次失败 + 1 次重试成功
    assert kept == []
    assert dropped[0]["reason"] == "标题命中硬排除词"
    assert "兜底保留" not in dropped[0]["reason"]


def test_single_fallback_keep_on_persistent_failure():
    """多次重试仍失败(默认兜底 keep): 保留并标"待复核", 不误杀; 每条约 tries 次调用。"""
    client = _client()
    calls = {"n": 0}

    def boom(prompt, **kw):
        calls["n"] += 1
        raise LLMError("服务端无响应")

    with mock.patch.object(client, "chat_json", side_effect=boom):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(2),
                                        batch_size=1, max_tries=3)
    assert len(kept) == 2
    assert dropped == []
    assert all("待复核" in r["reason"] for r in kept)
    assert calls["n"] == 2 * 3                    # 每条都试满 3 次


def test_single_fallback_drop_configurable():
    """FILTER_JUDGE_FALLBACK=drop 语义: 多次仍失败 -> 剔除宁缺勿滥。"""
    client = _client()

    def boom(prompt, **kw):
        raise LLMError("服务端无响应")

    with mock.patch.object(client, "chat_json", side_effect=boom):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(2),
                                        batch_size=1, max_tries=2, fallback_keep=False)
    assert kept == []
    assert len(dropped) == 2
    assert all("兜底剔除" in r["reason"] for r in dropped)


def test_single_mixed_keep_drop_order_preserved():
    """混合判定: 输出与输入顺序一致, 失败条不挤占其他条的判定。"""
    client = _client()

    def _idx(prompt: str) -> int:
        seg = prompt.split("标题: t")[1]
        return int(seg[0]) if seg[:1].isdigit() else 0

    def mixed(prompt, **kw):
        idx = _idx(prompt)
        if idx == 1:                              # t1 持续失败(2 次)
            raise LLMError("超时")
        keep = idx in (0, 2)
        return {"judgments": [{"index": 0, "keep": keep,
                               "reason": "命中" if keep else "剔除"}]}

    with mock.patch.object(client, "chat_json", side_effect=mixed):
        with mock.patch("time.sleep", return_value=None):
            kept, dropped = judge_batch(client, {"category": "c"}, _recs(3),
                                        batch_size=1, max_tries=2)
    assert [r["title"] for r in kept] == ["t0", "t1", "t2"]   # t1 失败兜底保留
    assert dropped == []
    assert "待复核" in kept[1]["reason"]          # t1 兜底被打标, 便于复核识别
    assert "待复核" not in kept[0]["reason"]
    assert "待复核" not in kept[2]["reason"]
