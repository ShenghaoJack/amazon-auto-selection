# -*- coding: utf-8 -*-
"""截图: /w/user/login 与 /v3/reversing 两个页面的实际状态(登录检测排查)。"""
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
    out_dir = "data/diag_shots"
    os.makedirs(out_dir, exist_ok=True)
    with ctx.browser("sellersprite", headed=True) as browser:
        page = browser.new_page()
        # 1. 登录页
        browser.goto(page, "https://www.sellersprite.com/w/user/login")
        time.sleep(8)
        print("login URL:", page.url)
        browser.screenshot(page, os.path.join(out_dir, "login.png"))
        # 2. 反查工具页
        browser.goto(page, "https://www.sellersprite.com/v3/reversing")
        time.sleep(6)
        print("reversing URL:", page.url)
        browser.screenshot(page, os.path.join(out_dir, "reversing.png"))
        # 3. 后台工作台
        browser.goto(page, "https://www.sellersprite.com/v2/welcome")
        time.sleep(6)
        print("welcome URL:", page.url)
        browser.screenshot(page, os.path.join(out_dir, "welcome.png"))
    print("截图目录:", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
