# -*- coding: utf-8 -*-
"""诊断: 卖家精灵反查流量来源页(v3/reversing)页面结构。

复用步骤11的选择器/定位函数, 打开真实页面:
  1. dump 全部可见输入元素(tag/class/placeholder)
  2. dump 全部可见按钮文本
  3. 定位 ASIN 输入框并填入 parents.txt 前 2 个父体
  4. 点击查询, 等待后 dump 结果/错误状态

注意: 会真实发起一次查询(消耗 1 组当日配额), 用于定位选择器问题。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.paths import Paths
from steps import step11_traffic_source as s11
from steps.base import StepContext


def main() -> int:
    cfg = Config.load("config.yaml")
    ctx = StepContext(cfg, Paths(cfg))
    step = s11.Step11TrafficSource()
    parents = [
        l.strip()
        for l in open(
            "data/salt-pepper-shaker-sets/salt-pepper-shaker-sets-parents.txt",
            encoding="utf-8",
        )
        if l.strip()
    ][:2]

    with ctx.browser("sellersprite", headed=True) as browser:
        page = browser.new_page()
        browser.goto(page, "https://www.sellersprite.com/v3/reversing")
        time.sleep(8)
        print("=== 页面标题 ===", page.title())
        print("=== 可见输入元素 ===")
        print(s11._dump_inputs(page))
        print("=== 可见按钮文本 ===")
        btns = []
        try:
            loc = page.locator("button")
            for i in range(loc.count()):
                try:
                    t = (loc.nth(i).inner_text() or "").strip().replace("\n", " ")
                    if t:
                        btns.append(t)
                except Exception:
                    continue
        except Exception as e:
            print("按钮枚举失败:", e)
        print(btns[:40])

        # 站点 + 日期
        step._select_site(ctx, browser, page, str(cfg.get("SITE", "美国") or "美国"))
        step._select_month(ctx, browser, page, str(cfg.get("TRAFFIC_MONTH", "") or ""))
        print("=== 选完站点/日期后的可见输入元素 ===")
        print(s11._dump_inputs(page))

        # ASIN 输入框
        inp = s11._find_asin_input(page)
        if inp is None:
            print("!!! 未定位到 ASIN 输入框")
            return 2
        print("=== 定位到的 ASIN 输入框 ===")
        try:
            print("class:", inp.get_attribute("class"))
            print("placeholder:", inp.get_attribute("placeholder"))
            print("tag:", inp.evaluate("e => e.tagName.toLowerCase()"))
        except Exception as e:
            print("读取属性失败:", e)

        step._fill_asins(ctx, browser, page, parents)
        print(f"=== 已填入 {len(parents)} 个父体 ===")

        # 查询按钮
        print("=== 尝试点击查询 ===")
        try:
            browser.click(page, "button:has-text('查询'), button:has-text('反查'), [class*=search-btn]")
            print("点击查询成功")
        except Exception as e:
            print("点击查询失败:", e)

        time.sleep(15)
        print("=== 查询后页面状态 ===")
        print("el-message--error 数量:", page.locator(".el-message--error").count())
        print("el-message--success 数量:", page.locator(".el-message--success").count())
        print("el-table__row 数量:", page.locator(".el-table__row").count())
        print("含'导出'按钮数量:", page.locator("button:has-text('导出')").count())
        body = ""
        try:
            body = (page.inner_text("body") or "")[:800].replace("\n", " | ")
        except Exception:
            pass
        print("页面文字片段:", body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
