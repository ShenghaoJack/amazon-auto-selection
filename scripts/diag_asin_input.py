# -*- coding: utf-8 -*-
"""诊断: 查竞品页 ASIN 输入框(disabled)的可编辑行为。"""
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
        asin_input = page.locator("input[placeholder*='ASIN']").first
        print("ASIN 输入框存在:", asin_input.count() > 0)
        try:
            print("初始 disabled:", asin_input.get_attribute("disabled"))
            print("初始 editable:", asin_input.is_editable())
        except Exception as e:
            print("初始检查异常:", e)
        # 先选站点+日期(按正式流程)
        try:
            page.locator("[class*=market-select] input").first.click()
            time.sleep(1.5)
            page.locator(".el-select-dropdown__item:visible", has_text="美国站").first.click()
            time.sleep(1)
            print("站点已选")
        except Exception as e:
            print("站点选择异常:", e)
        try:
            page.locator("[class*=date-select] input").first.click()
            time.sleep(1.5)
            page.locator(".el-select-dropdown__item:visible", has_text="2026-07").first.click()
            time.sleep(1)
            val = page.locator("[class*=date-select] input").first.input_value()
            print("日期已选, 输入框值:", repr(val))
        except Exception as e:
            print("日期选择异常:", e)
        # ASIN 输入框状态
        try:
            print("选完站点日期后 disabled:", asin_input.get_attribute("disabled"))
            print("选完站点日期后 editable:", asin_input.is_editable())
            asin_input.click()
            time.sleep(1)
            print("点击后 editable:", asin_input.is_editable())
            try:
                asin_input.fill("B06A902C65")
                print("fill 成功, 值:", asin_input.input_value())
            except Exception as fe:
                print("fill 失败:", fe)
                # JS 解除 disabled 再填
                page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.removeAttribute('disabled');
                }""", "input[placeholder*='ASIN']")
                time.sleep(0.5)
                try:
                    asin_input.fill("B06A902C65")
                    print("JS 解除后 fill 成功, 值:", asin_input.input_value())
                except Exception as fe2:
                    print("JS 解除后仍失败:", fe2)
        except Exception as e:
            print("ASIN 交互异常:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
