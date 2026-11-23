"""Regression tests for SellerSprite MCP v1 deprecation banner wrapped around JSON payload."""
from __future__ import annotations

import json

from core.mcp import _parse_payload


def test_parse_payload_handles_double_encoded_migration_banner_prefix():
    inner = {
        "code": "OK",
        "message": "成功",
        "data": {
            "pages": 1,
            "page": 1,
            "size": 10,
            "total": 1,
            "items": [{"marketplace": "US", "keyword": "air fryer", "searches": 2718141}],
        },
    }
    banner = (
        "\nℹ️ 服务迁移提醒：卖家精灵 MCP v1 将于 2026-12-08 下线，届时 v1 接口将不可用。"
        "请尽快迁移至 MCP v2。迁移指南：https://mjjl.cn/stoQ0m"
    )
    # 实测 2026-11-23 线上原样：content[0].text 外层被多包了一层 JSON 字符串引号
    wrapped_text = json.dumps(banner + json.dumps(inner, ensure_ascii=False), ensure_ascii=False)
    parsed = _parse_payload({"content": [{"type": "text", "text": wrapped_text}]})
    assert isinstance(parsed, dict)
    assert parsed["code"] == "OK"
    assert parsed["data"]["items"][0]["keyword"] == "air fryer"
    assert parsed["data"]["items"][0]["searches"] == 2718141


def test_parse_payload_handles_unquoted_prefix_and_suffix_banner_and_plain_json():
    inner = {"code": "OK", "message": "成功", "data": {"items": [{"keyword": "cat feeder"}]}}
    raw_prefixed = "ℹ️ 提醒：{忽略提示} 详情见链接 " + json.dumps(inner, ensure_ascii=False) + "\n尾注"
    parsed = _parse_payload({"content": [{"type": "text", "text": raw_prefixed}]})
    assert parsed == inner

    plain = json.dumps(inner, ensure_ascii=False)
    assert _parse_payload({"content": [{"type": "text", "text": plain}]}) == inner


def test_parse_payload_preserves_business_error_envelope_with_banner():
    err = {"code": "ERROR", "message": "请求过于频繁", "data": None}
    wrapped = json.dumps("ℹ️ 迁移提醒" + json.dumps(err, ensure_ascii=False), ensure_ascii=False)
    parsed = _parse_payload({"content": [{"type": "text", "text": wrapped}]})
    assert parsed["code"] == "ERROR"
    assert "过于频繁" in parsed["message"]
