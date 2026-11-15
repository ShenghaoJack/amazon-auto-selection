# -*- coding: utf-8 -*-
"""诊断: 查竞品页(v3/competitor-lookup)的站点/日期/输入框结构。"""
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
    with ctx.browser("sellersprite") as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/v3/competitor-lookup")
        time.sleep(8)
        print("URL:", page.url)
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
            try:
                chain = el.evaluate(
                    """e => {
                        const out = [];
                        let p = e.parentElement;
                        for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
                            out.push((p.className || '').toString().slice(0, 60));
                        }
                        return out;
                    }"""
                )
            except Exception:
                chain = []
            print(
                f"  #{i} name={info['name']!r} type={info['type']!r} "
                f"placeholder={info['placeholder']!r} class={info['class']!r} chain={chain}"
            )
        print("=== 可见 textarea ===")
        tl = page.locator("textarea")
        for i in range(tl.count()):
            try:
                if tl.nth(i).is_visible():
                    print(f"  textarea#{i} placeholder={(tl.nth(i).get_attribute('placeholder') or '')!r}")
            except Exception:
                pass
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
                    btns.append(t[:40])
            except Exception:
                continue
        print(btns[:30])
        # 站点/日期下拉: 点开看看
        sel = page.locator(".el-select:visible")
        print("可见 .el-select 数量:", sel.count())
        for i in range(min(sel.count(), 5)):
            try:
                t = (sel.nth(i).inner_text() or "")[:40].replace("\n", " ")
                print(f"  el-select#{i}: {t!r}")
            except Exception:
                pass
        # 点第一个 el-select 看下拉选项
        if sel.count() > 0:
            try:
                sel.first.click()
                time.sleep(2)
                items = page.locator(".el-select-dropdown__item:visible")
                opts = []
                for i in range(items.count()):
                    try:
                        opts.append((items.nth(i).inner_text() or "").strip()[:30])
                    except Exception:
                        pass
                print("下拉选项:", opts[:30])
            except Exception as e:
                print("点开下拉失败:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
