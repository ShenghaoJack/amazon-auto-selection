"""COOKIE 引号冲突修复脚本(命令行入口, 复用 core.config.fix_cookie_quoting)。

新 COOKIE 常含双引号(如 `x-main="hVql..."`), 与 config.yaml 中双引号包裹的
COOKIE 值冲突导致 YAML 解析失败。本脚本把 COOKIE 值改为单引号包裹。

注意: 流水线启动时 Config.load 已**自动**执行此修复(无需手动); 本脚本用于
主动修复/人工确认。用法: python -X utf8 scripts/fix_cookie_yaml.py [config.yaml]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import fix_cookie_quoting


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    if not os.path.exists(path):
        print(f"配置文件不存在: {path}")
        return 1
    changed = fix_cookie_quoting(path)
    if changed:
        print(f"已修复 {path}: COOKIE 改为单引号包裹")
        return 0
    print(f"{path}: COOKIE 无需修复(无引号冲突)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
