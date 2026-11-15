# -*- coding: utf-8 -*-
"""core.mcp._parse_payload 回归测试。

2026-09-07: 修复"content 单段 JSON 内字符串含 ' | ' 导致整段被切碎解析失败"
(competitor_lookup 全历史回填 0 的根因)。此文件防止该问题回归。
"""
import json

from core.mcp import _parse_payload


def _result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def test_single_block_with_pipe_inside_string():
    # 商品标题含 " | " 时, 旧实现按 " | " 切分会把整段 JSON 撕碎 -> {}
    payload = {"code": "OK", "data": {"items": [{"asin": "B0X", "title": "A | B"}]}}
    res = _result(json.dumps(payload, ensure_ascii=False))
    assert _parse_payload(res) == payload


def test_large_single_block_with_many_pipes():
    items = [{"asin": f"B0{i}", "title": "Salt and Pepper Mill Sets | Electric | Model X"} for i in range(30)]
    payload = {"code": "OK", "message": "成功", "data": {"items": items}}
    res = _result(json.dumps(payload, ensure_ascii=False))
    parsed = _parse_payload(res)
    assert parsed.get("code") == "OK"
    assert len(parsed["data"]["items"]) == 30


def test_multi_block_each_complete_json():
    p1 = {"code": "OK", "data": {"n": 1}}
    p2 = {"code": "OK", "data": {"n": 2}}
    res = {
        "content": [
            {"type": "text", "text": json.dumps(p1)},
            {"type": "text", "text": json.dumps(p2)},
        ]
    }
    assert _parse_payload(res) == p1


def test_legacy_pipe_separated_docs():
    # 旧格式兼容: 单段内多份 JSON 以 " | " 分隔
    p1 = {"code": "OK", "data": 1}
    p2 = {"code": "ERR", "data": 2}
    res = _result(" | ".join([json.dumps(p1), json.dumps(p2)]))
    assert _parse_payload(res) == p1


def test_empty_content_returns_dict():
    assert _parse_payload({"content": []}) == {}
    assert _parse_payload({}) == {}
