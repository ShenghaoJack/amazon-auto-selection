# -*- coding: utf-8 -*-
"""**部署期密钥走环境变量**（2026-10-12 用户口径）。

背景
===============================================================================
系统要上服务器。令牌**不该进源码/仓库**（分享即泄露），也不该让 14 个类目的
`config.yaml` 各存一份（换令牌要改 14 处、还容易漏）。

规则
===============================================================================
  ① `Config.load(真实配置文件)` → **环境变量优先**（`ENV_OVERRIDE_KEYS`）
  ② **只覆盖非空**环境变量；环境变量为空 → **原样保留 config 的值**（向后兼容）
  ③ `Config({...})`（inline 构造，测试/内联用）**不受影响** ——
     否则单元测试会被本机环境变量悄悄改掉（假失败/假通过），这是**刻意**的边界
  ④ `PANGOLIN_MCP_URL` **不进环境变量**：它已写死在代码里（固定端点、非秘密）
"""
from __future__ import annotations

import pytest

from core.config import ENV_OVERRIDE_KEYS, Config

SECRETS = ("LLM_API_KEY", "VISION_API_KEY", "SS_MCP_KEY", "PANGOLIN_MCP_KEY")


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_applies_env_overrides(tmp_path, monkeypatch):
    """★ `Config.load` 时环境变量**覆盖** config.yaml 里的密钥。"""
    for k in SECRETS:
        monkeypatch.setenv(k, "env-" + k)
    p = _write(tmp_path, 'CAT: "t"\nLLM_API_KEY: "from-yaml"\nSS_MCP_KEY: "from-yaml"\n')
    cfg = Config.load(p)
    for k in SECRETS:
        assert cfg.get(k) == "env-" + k, k


def test_load_keeps_config_when_env_empty(tmp_path, monkeypatch):
    """★ 环境变量为空 → **不动** config 的值（向后兼容：老部署不改也能跑）。"""
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    p = _write(tmp_path, 'CAT: "t"\nLLM_API_KEY: "from-yaml"\nSS_MCP_KEY: "from-yaml"\n')
    cfg = Config.load(p)
    assert cfg.get("LLM_API_KEY") == "from-yaml"
    assert cfg.get("SS_MCP_KEY") == "from-yaml"


def test_env_does_not_leak_into_inline_config(monkeypatch):
    """★ **边界**：`Config({...})`（inline）**不受环境变量影响**。

    为什么必须有这条：大量单元测试用 `Config({...})` 构造夹具；若这里也被环境变量覆盖，
    本机一旦 `setx` 了真令牌，那些测试就会被悄悄改掉（假失败/假通过）。
    """
    for k in SECRETS:
        monkeypatch.setenv(k, "env-" + k)
    cfg = Config({"CAT": "t", "LLM_API_KEY": "inline", "SS_MCP_KEY": "inline",
                  "PANGOLIN_MCP_KEY": "inline"})
    assert cfg.get("LLM_API_KEY") == "inline"
    assert cfg.get("SS_MCP_KEY") == "inline"
    assert cfg.get("PANGOLIN_MCP_KEY") == "inline"


def test_env_override_key_list_is_exactly_these_four():
    """★ 白名单**恰好**这四个 —— 别把普通配置项也变成"环境变量优先"（会很难查）。

    `PANGOLIN_MCP_URL` **不在**里面：它写在代码里（`tools/voc/pipeline.PANGOLIN_MCP_URL`）。
    `VISION_BASE_URL`/`VISION_MODEL` 也**不在**：它们**不是密钥**（与 `LLM_BASE_URL`/`LLM_MODEL` 同待遇，留在 config）。
    """
    assert set(ENV_OVERRIDE_KEYS) == set(SECRETS)
    assert "PANGOLIN_MCP_URL" not in ENV_OVERRIDE_KEYS
    assert "VISION_BASE_URL" not in ENV_OVERRIDE_KEYS
    assert "VISION_MODEL" not in ENV_OVERRIDE_KEYS


def test_pangolinfo_url_is_a_code_constant():
    """★ Pangolinfo 的 URL 必须是**代码常量**（固定端点，非秘密）。"""
    from tools.voc import pipeline as P
    assert P.PANGOLIN_MCP_URL.startswith("https://"), P.PANGOLIN_MCP_URL
    # 且解析函数返回的就是它（不受 config 影响）
    monkeypatch_free = Config({"CAT": "t", "PANGOLIN_MCP_URL": "https://other.example/mcp"})
    assert P.resolve_pangolinfo_creds(monkeypatch_free)[0] == P.PANGOLIN_MCP_URL
