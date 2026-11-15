# -*- coding: utf-8 -*-
"""对照: 同一 ss-profile, headless vs headed 打开 /w/user/login 的状态差异。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.browser import BrowserAutomation
from core.config import Config
from core.paths import Paths


def probe(cfg, headed: bool) -> None:
    print(f"===== headed={headed} =====")
    with BrowserAutomation(cfg, profile_dir=Paths(cfg).profile_ss(), headed=headed) as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/w/user/login")
        time.sleep(10)
        print("URL:", page.url)
        try:
            dep = page.locator("input[name=departmentKeyword]").first.is_visible()
        except Exception:
            dep = "?"
        print("departmentKeyword(工作台)可见:", dep)
        try:
            email = page.locator("input[name=email]").first.is_visible()
        except Exception:
            email = "?"
        print("input[name=email](登录表单)可见:", email)
        try:
            body = page.inner_text("body") or ""
        except Exception as e:
            body = f"(异常: {e})"
        print("含'高级会员':", "高级会员" in body)
        print("含'未登录':", "未登录" in body)
        print("含'立即登录':", "立即登录" in body)
        print("片段:", body[:200].replace("\n", " | "))
        print()


def main() -> int:
    cfg = Config.load("config.yaml")
    probe(cfg, headed=False)
    probe(cfg, headed=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
