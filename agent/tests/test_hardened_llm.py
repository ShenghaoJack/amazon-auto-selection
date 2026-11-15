"""Phase 6 · 硬化 LLM 测试(纯函数 + mock, 无网络无 crewAI)。

验证: messages 归一化、payload 注入(非思考/JSON)、JSON 容错解析、
HardenedLLM 在 dry-run 下走内置 mock 离线可测、build_hardened_llm 的依赖行为。
"""

from __future__ import annotations

import importlib.util

import pytest

from agent._vendor import Config
from agent.crews import hardened_llm as hl


def _cfg(patch: dict | None = None, dry_run: bool = True) -> Config:
    base = {
        "LLM_API_KEY": "sk-test",
        "LLM_MODEL": "deepseek-chat",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "DRY_RUN": dry_run,
    }
    if patch:
        base.update(patch)
    return Config(base)


def test_messages_to_prompt():
    assert hl.messages_to_prompt("直接字符串") == "直接字符串"
    msgs = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "好的"}]
    s = hl.messages_to_prompt(msgs)
    assert "user: 你好" in s and "assistant: 好的" in s
    # content 为列表(多模态)也能处理
    msgs2 = [{"role": "user", "content": [{"text": "图片描述"}]}]
    assert "图片描述" in hl.messages_to_prompt(msgs2)


def test_build_llm_payload_non_think_json():
    p = hl.build_llm_payload("deepseek-chat", "hi", 0.2, 4000, json_mode=True, no_think=True)
    assert p["model"] == "deepseek-chat"
    assert p["response_format"] == {"type": "json_object"}
    assert p["thinking"] == {"type": "disabled"}       # 非思考注入
    # 关闭 JSON 时无 response_format
    p2 = hl.build_llm_payload("deepseek-chat", "hi", 0.2, 4000, json_mode=False)
    assert "response_format" not in p2


def test_extract_json():
    assert hl.extract_json('{"a": 1}') == {"a": 1}
    assert hl.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert hl.extract_json('前缀 [1,2] 后缀') == [1, 2]


def test_hardened_llm_mock_chat():
    llm = hl.HardenedLLM(_cfg(dry_run=True))
    out = llm.chat("你好")
    assert isinstance(out, str) and "mock" in out.lower()


def test_hardened_llm_mock_chat_json():
    llm = hl.HardenedLLM(_cfg(dry_run=True))
    obj = llm.chat_json("提取属性")
    assert isinstance(obj, dict) and obj.get("ok") is True


def test_hardened_llm_missing_key_when_not_mock():
    # 非 dry-run 但缺 key → 调用时报错(而不是静默)
    llm = hl.HardenedLLM(_cfg(dry_run=False, patch={"LLM_API_KEY": ""}))
    with pytest.raises(Exception):
        llm.chat("你好")


def test_build_hardened_llm_dependency():
    if importlib.util.find_spec("crewai") is None:
        with pytest.raises(RuntimeError, match="crewai"):
            hl.build_hardened_llm(_cfg())
    else:
        adapt = hl.build_hardened_llm(_cfg(dry_run=True))
        assert adapt is not None


def test_describe_hardened():
    s = hl.describe_hardened(_cfg(dry_run=True))
    assert "mock" in s
