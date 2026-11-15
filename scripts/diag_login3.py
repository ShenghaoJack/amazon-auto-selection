# -*- coding: utf-8 -*-
"""诊断 v3: 全新无会话浏览器打开卖家精灵登录页(w/user/login)的真实表单结构。

用独立临时 Profile(无任何登录态), 确保看到真正的游客登录页。
"""
from __future__ import annotations

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.browser import BrowserAutomation
from core.config import Config


def main() -> int:
    cfg = Config.load("config.yaml")
    tmp_profile = os.path.join("data", "profiles", "diag-tmp-" + uuid.uuid4().hex[:8])
    os.makedirs(tmp_profile, exist_ok=True)
    with BrowserAutomation(cfg, profile_dir=tmp_profile, headed=True) as browser:
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
        print("=== 可见 button ===")
        btns = []
        loc2 = page.locator("button")
        for i in range(loc2.count()):
            try:
                el = loc2.nth(i)
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip().replace("\n", " ")
                if t:
                    btns.append(t[:50])
            except Exception:
                continue
        print(btns[:40])
        print("=== 含 扫码/密码/账号/手机/邮箱 文本的可见元素 ===")
        for kw in ("扫码", "密码", "账号", "手机", "邮箱"):
            out = []
            try:
                els = page.locator(f"text={kw}")
                for i in range(min(els.count(), 10)):
                    try:
                        el = els.nth(i)
                        if el.is_visible():
                            t = (el.inner_text() or "").strip()[:40]
                            if t:
                                out.append(t)
                    except Exception:
                        continue
            except Exception:
                pass
            if out:
                print(f"  '{kw}': {out}")
        print("=== 页面可见文本(前300字) ===")
        try:
            body = page.inner_text("body") or ""
        except Exception as e:
            body = f"(异常: {e})"
        print(body[:300].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
