# 单元测试: COOKIE 引号冲突自动修复(Config.load 后台处理)
import os

import pytest

from core.config import Config, fix_cookie_quoting
from core.errors import ConfigError


def test_fix_cookie_quoting_detects_conflict(ws_tmp):
    """双引号包裹但值内含(未转义)双引号 -> 应修复为单引号包裹。"""
    path = os.path.join(ws_tmp, "config.yaml")
    # 真实场景: 用户直接粘贴新 COOKIE, 值内含 x-main="..." 未转义双引号
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('COOKIE: "a=1; x-main="hVql""\n')
        fh.write('CAT: "t"\n')
    assert fix_cookie_quoting(path) is True
    content = open(path, encoding="utf-8").read()
    assert content.startswith("COOKIE: 'a=1; x-main=\"hVql\"'\n")


def test_fix_cookie_quoting_no_change(ws_tmp):
    """单引号包裹或值无冲突 -> 不修改。"""
    path = os.path.join(ws_tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("COOKIE: 'a=1; b=2'\n")
        fh.write('CAT: "t"\n')
    assert fix_cookie_quoting(path) is False
    content = open(path, encoding="utf-8").read()
    assert content.startswith("COOKIE: 'a=1; b=2'\n")


def test_fix_cookie_quoting_single_quote_escape(ws_tmp):
    """值内含单引号时双写转义, 保持 YAML 合法。"""
    path = os.path.join(ws_tmp, "config.yaml")
    # 双引号包裹, 值内含双引号(触发修复); 同时内含单引号需双写
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("COOKIE: \"a=1; x-main=\"q\"; note=it's\"\n")
        fh.write('CAT: "t"\n')
    assert fix_cookie_quoting(path) is True
    cfg = Config.load(path)
    assert cfg.get("COOKIE") == 'a=1; x-main="q"; note=it\'s'


def test_config_load_auto_fixes_cookie(ws_tmp):
    """Config.load 应后台自动修复 COOKIE 引号冲突并成功加载。"""
    path = os.path.join(ws_tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("COOKIE: \"a=1; x-main=\"hVql\"\"\n")
        fh.write('CAT: "test-cat"\n')
        fh.write("MARKET: amazon.com\n")
    cfg = Config.load(path)
    assert cfg.get("CAT") == "test-cat"
    assert cfg.get("COOKIE") == 'a=1; x-main="hVql"'


def test_config_load_non_cookie_error_raises(ws_tmp):
    """非 COOKIE 引号问题的 YAML 错误应正常抛 ConfigError(不误修复)。"""
    path = os.path.join(ws_tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("CAT: [unclosed\n")  # 语法错误, 与 COOKIE 无关
    with pytest.raises(ConfigError):
        Config.load(path)
