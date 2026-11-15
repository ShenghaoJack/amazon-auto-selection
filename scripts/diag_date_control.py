# -*- coding: utf-8 -*-
"""诊断: v3/reversing 日期控件结构(CDP 模式, 真实页面)。

点击"请选择"日期输入框, dump 弹出的面板/下拉结构及选项,
确认日期应如何选择(需求文档: 下拉选择 mousedown+click -> 文本模糊匹配)。
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
    with ctx.browser("sellersprite") as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/v3/reversing")
        time.sleep(6)
        print("URL:", page.url)
        # 找日期输入框
        loc = page.locator("input[placeholder='请选择']")
        print("请选择 输入框数量:", loc.count())
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                cls = el.get_attribute("class") or ""
                parent = el.evaluate("e => e.parentElement ? e.parentElement.className : ''")
            except Exception as e:
                cls, parent = "?", "?"
            print(f"  #{i} class={cls!r} parentClass={parent!r}")
        # 点击第一个日期输入框, 观察弹出什么
        if loc.count() > 0:
            loc.first.click()
            time.sleep(3)
            print("点击后 URL:", page.url)
            # 面板/下拉
            for sel in (
                ".el-picker-panel",
                ".el-picker-panel:visible",
                ".el-select-dropdown:visible",
                "[class*=dropdown]:visible",
                "[class*=picker]:visible",
                ".el-picker__popper",
            ):
                try:
                    n = page.locator(sel).count()
                    vis = 0
                    for i in range(n):
                        try:
                            if page.locator(sel).nth(i).is_visible():
                                vis += 1
                        except Exception:
                            pass
                    print(f"  {sel}: count={n} visible={vis}")
                except Exception as e:
                    print(f"  {sel}: err {e}")
            # dump 可见的面板文本
            for sel in (".el-picker-panel", ".el-select-dropdown"):
                try:
                    n = page.locator(sel).count()
                    for i in range(n):
                        el = page.locator(sel).nth(i)
                        if el.is_visible():
                            t = (el.inner_text() or "")[:400].replace("\n", " | ")
                            print(f"  {sel}[{i}] 文本: {t}")
                except Exception:
                    pass
        # 页面上的"最近30天"等预设控件
        for kw in ("最近30天", "最近90天", "30天", "自定义"):
            try:
                els = page.locator(f"text={kw}")
                n = els.count()
                vis = sum(1 for i in range(n) if els.nth(i).is_visible())
                print(f"  '{kw}': count={n} visible={vis}")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
