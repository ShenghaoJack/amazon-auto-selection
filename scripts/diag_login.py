# -*- coding: utf-8 -*-
"""诊断: 卖家精灵登录页(w/user/login)结构。

输出: 全部 input(attached/visible/name/type/placeholder/class) +
全部 button 文本 + 疑似 tab 元素, 用于修复自动登录的表单定位。
"""
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
    with ctx.browser("sellersprite", headed=True) as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/w/user/login")
        time.sleep(8)
        print("=== 页面标题 ===", page.title())
        print("=== 当前 URL ===", page.url)
        print("=== 全部 input 元素(attached) ===")
        loc = page.locator("input")
        print("input 总数:", loc.count())
        for i in range(loc.count()):
            el = loc.nth(i)
            info = {}
            for attr in ("name", "type", "placeholder", "class", "value"):
                try:
                    info[attr] = el.get_attribute(attr) or ""
                except Exception:
                    info[attr] = "?"
            try:
                vis = el.is_visible()
            except Exception:
                vis = "?"
            print(
                f"  #{i} visible={vis} name={info['name']!r} type={info['type']!r} "
                f"placeholder={info['placeholder']!r} class={info['class']!r}"
            )
        print("=== 全部 button 文本(可见) ===")
        btns = []
        loc2 = page.locator("button")
        for i in range(loc2.count()):
            try:
                el = loc2.nth(i)
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip().replace("\n", " ")
                if t:
                    btns.append(t)
            except Exception:
                continue
        print(btns[:40])
        print("=== 疑似 tab/标签元素(含'登录'/'扫码'/'密码'/'手机'/'邮箱'文本) ===")
        for kw in ("扫码", "密码", "账号", "手机", "邮箱", "登录"):
            try:
                els = page.locator(f"text={kw}")
                found = []
                for i in range(min(els.count(), 8)):
                    try:
                        el = els.nth(i)
                        if el.is_visible():
                            t = (el.inner_text() or "").strip()[:30]
                            found.append(t)
                    except Exception:
                        continue
                if found:
                    print(f"  '{kw}': {found}")
            except Exception as e:
                print(f"  '{kw}' 查询失败: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
