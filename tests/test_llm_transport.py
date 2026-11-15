# 单元测试: LLM 传输层错误(如 SSL 断连)包装为 LLMError, 不逃逸炸步骤
import httpx
import pytest
from unittest import mock

from core.config import Config
from core.errors import LLMError
from core.llm import LLMClient


def _client() -> LLMClient:
    cfg = Config({})
    cfg.set("LLM_API_KEY", "k")
    return LLMClient(cfg)


def test_request_wraps_ssl_error_as_llmerror():
    """httpx 传输层异常(SSL EOF)必须包装为 LLMError, 供上层重试/容错。"""
    client = _client()

    class _SSLBrokenClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise httpx.ReadError("SSL: UNEXPECTED_EOF_WHILE_READING", request=None)

    with mock.patch("httpx.Client", return_value=_SSLBrokenClient()):
        with pytest.raises(LLMError) as ei:
            client._request("https://api.example.com", "k", "m", [{"role": "user", "content": "x"}], 0.2, 100, True)
    assert "SSL" in str(ei.value) or "请求失败" in str(ei.value)


def test_chat_json_retries_then_raises_llmerror():
    """chat_json 对反复传输失败: 重试后最终抛 LLMError(而非裸 httpx 异常)。"""
    client = _client()

    class _SSLBrokenClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise httpx.ReadError("SSL: UNEXPECTED_EOF_WHILE_READING", request=None)

    with mock.patch("httpx.Client", return_value=_SSLBrokenClient()):
        with mock.patch.object(client, "_cool_down", return_value=None):
            with pytest.raises(LLMError):
                client.chat_json("请判定", temperature=0.2, max_tokens=100)


def test_judge_batch_all_keep_on_llm_failure():
    """LLM 一直失败时 judge_batch 阶梯重试后最终兜底全保留(不误杀), 步骤不中断。"""
    from steps._filtering import judge_batch

    client = _client()

    def boom(prompt, **kw):
        raise LLMError("全部重试失败: SSL")

    with mock.patch.object(client, "chat_json", side_effect=boom):
        recs = [{"title": f"t{i}", "bullets": []} for i in range(3)]
        kept, dropped = judge_batch(client, {"category": "c"}, recs, batch_size=15)
    assert len(kept) == 3
    assert dropped == []
    assert all(r["keep"] is True for r in kept)
    # 兜底保留应打审计标记(区别于正常判定保留)
    assert all("兜底保留" in r["reason"] for r in kept)


def test_judge_batch_retries_timeout_then_succeeds():
    """批次超时后不立即放弃: 收集到末尾集中重发, 重发成功则正常判定(不误杀不误留)。"""
    from steps._filtering import judge_batch

    client = _client()
    calls = {"n": 0}

    def flaky(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:          # 第1轮整个批超时
            raise LLMError("LLM 请求超时(>15s, 服务端无响应/挂起)")
        return {"judgments": [
            {"index": 0, "keep": True, "reason": "命中相关词"},
            {"index": 1, "keep": False, "reason": "标题命中硬排除词"},
            {"index": 2, "keep": True, "reason": "命中相关词"},
        ]}

    with mock.patch.object(client, "chat_json", side_effect=flaky):
        recs = [{"title": f"t{i}", "bullets": []} for i in range(3)]
        kept, dropped = judge_batch(client, {"category": "c"}, recs, batch_size=10, log=None)
    # 第1轮超时 -> 第2轮(同批大小)集中重发成功
    assert calls["n"] == 2
    assert len(kept) == 2
    assert len(dropped) == 1
    assert all("兜底保留" not in r["reason"] for r in kept + dropped)
    assert dropped[0]["reason"] == "标题命中硬排除词"


def test_judge_batch_retry_shrinks_to_2_then_keeps_order():
    """前两轮(原批/集中重发)都失败时拆成 2 条/批, 输出顺序与输入一致。"""
    from steps._filtering import judge_batch

    client = _client()
    calls = {"n": 0}

    def flaky(prompt, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:          # 第1轮 10条批超时 + 第2轮集中重发仍超时
            raise LLMError("LLM 请求超时(>15s, 服务端无响应/挂起)")
        return {"judgments": [
            {"index": 0, "keep": True, "reason": "保留"},
            {"index": 1, "keep": True, "reason": "保留"},
        ]}

    with mock.patch.object(client, "chat_json", side_effect=flaky):
        recs = [{"title": f"t{i}", "bullets": []} for i in range(5)]
        kept, dropped = judge_batch(client, {"category": "c"}, recs, batch_size=10)
    # 第3轮拆 2 条/批: 3 次调用(2+2+1)
    assert calls["n"] == 2 + 3
    assert len(kept) == 5
    assert dropped == []
    assert [r["title"] for r in kept] == [f"t{i}" for i in range(5)]  # 顺序保持
    assert all("兜底保留" not in r["reason"] for r in kept)


def test_watchdog_timeout_raises_not_hang():
    """服务端挂起(请求永不返回)时 watchdog 在 timeout 内抛 LLMError, 不永久卡死。

    2026-09: httpx 的 timeout 不覆盖"接受请求后静默挂起"(ssl.read 永不返回),
    watchdog 用 daemon 线程 + join(timeout) 保证调用方在限定时间内拿到失败。
    """
    import time

    client = _client()

    class _HangingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            time.sleep(300)  # 模拟服务端永不返回

    with mock.patch("httpx.Client", return_value=_HangingClient()):
        t0 = time.time()
        with pytest.raises(LLMError) as ei:
            client._post_with_watchdog("https://api.example.com", {"a": "b"},
                                       {"x": 1}, timeout=2)
    assert time.time() - t0 < 10
    assert "超时" in str(ei.value) or "挂起" in str(ei.value)
