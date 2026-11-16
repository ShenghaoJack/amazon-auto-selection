# -*- coding: utf-8 -*-
"""探测 competitor_lookup 原始 tools/call 响应(content 文本)。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from core.config import Config
from core.mcp import MCPClient, _content_texts, _parse_payload


def main() -> int:
    cfg = Config.load("config.yaml")
    client = MCPClient(cfg)
    if client._mock is not None:
        print("dry-run")
        return 0
    client._initialize()
    url = str(cfg.get("SS_MCP_URL", ""))
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "competitor_lookup",
            "arguments": {
                "request": {
                    "marketplace": "US",
                    "asins": ["B06A902C65", "B0AF164FCA"],
                    "month": "202607",
                }
            },
        },
    }
    with httpx.Client(timeout=90) as c:
        resp = c.post(url, headers=client._headers(), json=body)
        resp.raise_for_status()
        data = resp.json()
    print("顶层 keys:", list(data.keys()))
    if "error" in data and data["error"]:
        print("error:", json.dumps(data["error"], ensure_ascii=False)[:500])
        return 2
    result = data.get("result") or {}
    print("result keys:", list(result.keys()))
    texts = _content_texts(result)
    print("content 文本前 2500 字:")
    print(texts[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
