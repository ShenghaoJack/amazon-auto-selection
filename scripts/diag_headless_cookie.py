# -*- coding: utf-8 -*-
"""快速验证: headless 下二次导航/刷新能否加载 ss-profile 会话 cookie。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.browser import BrowserAutomation
from core.config import Config
from core.paths import Paths


def state(page) -> str:
    try:
        if page.locator("input[name=departmentKeyword]").first.is_visible():
            return "WORKBENCH(已登录)"
    except Exception:
        pass
    try:
        if page.locator("input[name=email]").first.is_visible():
            return "LOGIN_FORM(未登录)"
    except Exception:
        pass
    try:
        body = page.inner_text("body") or ""
    except Exception:
        body = ""
    if "立即登录" in body:
        return "LOGIN_FORM(未登录)"
    return "UNKNOWN: " + body[:60]


def main() -> int:
    cfg = Config.load("config.yaml")
    with BrowserAutomation(cfg, profile_dir=Paths(cfg).profile_ss(), headed=False) as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/w/user/login")
        time.sleep(8)
        print("第1次导航:", state(page))
        browser.goto(page, "https://www.sellersprite.com/w/user/login")
        time.sleep(8)
        print("第2次导航:", state(page))
        page.reload()
        time.sleep(8)
        print("reload 后:", state(page))
        # 直接访问工作台
        browser.goto(page, "https://www.sellersprite.com/v2/welcome")
        time.sleep(8)
        print("v2/welcome:", state(page))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
