# -*- coding: utf-8 -*-
"""MCP 工具发现: 列出全部工具名与输入参数结构(定位 competitor_lookup 及其入参)。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from core.config import Config
from core.mcp import MCPClient


def main() -> int:
    cfg = Config.load("config.yaml")
    client = MCPClient(cfg)
    if client._mock is not None:
        print("dry-run: 跳过真实 MCP 发现")
        return 0
    client._initialize()
    url = str(cfg.get("SS_MCP_URL", ""))
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    with httpx.Client(timeout=60) as c:
        resp = c.post(url, headers=client._headers(), json=body)
        resp.raise_for_status()
        data = resp.json()
    result = data.get("result") or {}
    tools = result.get("tools") or []
    print(f"工具总数: {len(tools)}")
    for t in tools:
        name = t.get("name", "")
        desc = (t.get("description") or "")[:150].replace("\n", " ")
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        req = schema.get("required") or []
        print(f"\n=== {name} ===")
        print(f"  描述: {desc}")
        print(f"  必填: {req}")
        for pk, pv in list(props.items())[:8]:
            print(f"    {pk}: {json.dumps(pv, ensure_ascii=False)[:180]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
