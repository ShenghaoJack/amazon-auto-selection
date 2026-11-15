"""Phase 3 · LLM 工厂(纯函数)测试。

验证模型名归一化、参数组装、配置校验、描述; 以及 build_llm 的依赖/退化行为。
无需真实 API key、无网络、无 crewAI(纯函数部分始终可测)。
"""

from __future__ import annotations

import importlib.util

import pytest

from agent._vendor import Config
from agent.crews import llm_factory as lf


def _cfg(patch: dict | None = None) -> Config:
    base = {
        "LLM_API_KEY": "sk-test",
        "LLM_MODEL": "deepseek-chat",
        "LLM_BASE_URL": "https://api.deepseek.com",
    }
    if patch:
        base.update(patch)
    return Config(base)


def test_normalize_model():
    assert lf.normalize_model("deepseek-chat") == "deepseek/deepseek-chat"
    assert lf.normalize_model("openai/gpt-4o") == "openai/gpt-4o"     # 已带 provider
    assert lf.normalize_model("deepseek-chat", "moonshot") == "moonshot/deepseek-chat"
    assert lf.normalize_model("") == ""


def test_llm_kwargs():
    kw = lf.llm_kwargs(_cfg(), temperature=0.1, max_tokens=2048)
    assert kw["model"] == "deepseek/deepseek-chat"
    assert kw["base_url"] == "https://api.deepseek.com"
    assert kw["api_key"] == "sk-test"
    assert kw["temperature"] == 0.1
    assert kw["max_tokens"] == 2048


def test_validate_config_ok():
    ok, missing = lf.validate_config(_cfg())
    assert ok and missing == []


def test_validate_config_missing():
    # LLM_MODEL/LLM_BASE_URL 均有默认值, 故仅当 key 缺失时报缺 LLM_API_KEY
    ok, missing = lf.validate_config(Config({"LLM_MODEL": "deepseek-chat"}))
    assert not ok
    assert "LLM_API_KEY" in missing
    # 空配置也走默认, 因此仍只缺 LLM_API_KEY
    ok2, missing2 = lf.validate_config(Config({}))
    assert not ok2
    assert "LLM_API_KEY" in missing2
    assert "LLM_MODEL" not in missing2   # 默认已提供


def test_describe_llm():
    s = lf.describe_llm(_cfg())
    assert "deepseek/deepseek-chat" in s
    assert "已配置" in s


def test_build_llm_dependency_and_missing_key():
    # 缺 key → 无论 crewai 是否安装, 先报"配置缺失"
    with pytest.raises(RuntimeError, match="LLM 配置缺失"):
        lf.build_llm(Config({"LLM_MODEL": "deepseek-chat"}))

    # 有 key, 但 crewai 未装 → 报"需要 crewai"; 已装则尝试返回 LLM 实例
    if importlib.util.find_spec("crewai") is None:
        with pytest.raises(RuntimeError, match="crewai"):
            lf.build_llm(_cfg())
    else:
        llm = lf.build_llm(_cfg())
        assert llm is not None and hasattr(llm, "model")
