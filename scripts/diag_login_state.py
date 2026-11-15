# -*- coding: utf-8 -*-
"""快速验证: v3/reversing 当前登录状态 + 头部文本。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.paths import Paths
from steps.base import StepContext


def main() -> int:
    cfg = Config.load("config.yaml")
    ctx = StepContext(cfg, Paths(cfg))
    with ctx.browser("sellersprite", headed=False) as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/v3/reversing")
        time.sleep(6)
        print("URL:", page.url)
        body = ""
        try:
            body = page.inner_text("body") or ""
        except Exception as e:
            print("inner_text 异常:", e)
        print("含'未登录':", "未登录" in body)
        print("含'游客':", "游客" in body)
        print("含'立即查询':", "立即查询" in body)
        print("含'退出':", "退出" in body)
        # 头部 200 字
        idx = body.find("未登录")
        print("头部片段:", body[:300].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
