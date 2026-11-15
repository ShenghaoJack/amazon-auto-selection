# -*- coding: utf-8 -*-
"""诊断 v2: 卖家精灵登录页(w/user/login)可见表单结构。

重点: 是否停在本页 / 可见输入框 / 可见按钮 / tab 类元素 / 含'扫码/密码/账号'文本的可点击元素。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.paths import Paths
from steps.base import StepContext


def _vis_text(page, selector, n: int = 15) -> list[str]:
    out = []
    try:
        loc = page.locator(selector)
        for i in range(min(loc.count(), n)):
            try:
                el = loc.nth(i)
                if el.is_visible():
                    t = (el.inner_text() or "").strip().replace("\n", " ")
                    if t:
                        out.append(t[:40])
            except Exception:
                continue
    except Exception:
        pass
    return out


def main() -> int:
    cfg = Config.load("config.yaml")
    ctx = StepContext(cfg, Paths(cfg))
    with ctx.browser("sellersprite", headed=True) as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/w/user/login")
        time.sleep(10)
        print("最终 URL:", page.url)
        print("=== 可见 input ===")
        loc = page.locator("input")
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            info = {}
            for attr in ("name", "type", "placeholder", "class"):
                try:
                    info[attr] = el.get_attribute(attr) or ""
                except Exception:
                    info[attr] = "?"
            print(
                f"  #{i} name={info['name']!r} type={info['type']!r} "
                f"placeholder={info['placeholder']!r} class={info['class']!r}"
            )
        print("=== 可见 button ===", _vis_text(page, "button", 30))
        print("=== 含'tab'类的可见元素文本 ===", _vis_text(page, "[class*=tab]", 20))
        print("=== 含'扫码/密码/账号/手机/邮箱'文本的可见元素 ===")
        for kw in ("扫码", "密码", "账号", "手机", "邮箱"):
            found = _vis_text(page, f"text={kw}", 8)
            if found:
                print(f"  '{kw}': {found}")
        print("=== 页面可见文本片段(前400字) ===")
        try:
            body = page.inner_text("body") or ""
        except Exception as e:
            body = f"(inner_text异常: {e})"
        print(body[:400].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
