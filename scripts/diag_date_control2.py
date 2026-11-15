# -*- coding: utf-8 -*-
"""诊断 v2: 点击第2个'请选择'(日期)输入框, dump 弹出面板结构与选项。"""
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
        browser.goto(page, "https://www.sellersprite.com/v3/reversing")
        time.sleep(6)
        loc = page.locator("input[placeholder='请选择']")
        print("输入框数:", loc.count())
        # 点击第2个(日期)
        el = loc.nth(1)
        # 记录它的祖先链类名
        try:
            chain = el.evaluate(
                """e => {
                    const out = [];
                    let p = e;
                    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
                        out.push(p.tagName.toLowerCase() + '.' + (p.className || '').toString().slice(0, 80));
                    }
                    return out;
                }"""
            )
            print("日期输入框祖先链:", chain)
        except Exception as e:
            print("祖先链失败:", e)
        el.click()
        time.sleep(3)
        # dump 所有可见的下拉/面板
        for sel in (
            ".el-select-dropdown",
            ".el-picker-panel",
            "[class*=picker]",
            "[class*=dropdown]",
            ".el-picker__popper",
            "[role=tooltip]",
        ):
            try:
                n = page.locator(sel).count()
                vis = []
                for i in range(n):
                    try:
                        if page.locator(sel).nth(i).is_visible():
                            t = (page.locator(sel).nth(i).inner_text() or "")[:300].replace("\n", " | ")
                            vis.append(f"[{i}]{t}")
                    except Exception:
                        pass
                if vis:
                    print(f"{sel} 可见项:")
                    for v in vis:
                        print("   ", v)
            except Exception as e:
                print(f"{sel} err: {e}")
        # 最近30天 控件的位置
        try:
            els = page.locator("text=最近30天")
            for i in range(els.count()):
                el2 = els.nth(i)
                try:
                    vis = el2.is_visible()
                    cls = el2.evaluate("e => e.parentElement ? e.parentElement.className.toString().slice(0,80) : ''")
                    print(f"最近30天[{i}] visible={vis} parentClass={cls!r}")
                except Exception:
                    pass
        except Exception as e:
            print("最近30天 err:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
