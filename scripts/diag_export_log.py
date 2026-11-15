# -*- coding: utf-8 -*-
"""诊断: 卖家精灵导出日志页(v2/export-log)表格行的勾选框结构。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.paths import Paths
from steps.base import StepContext

EXPORT_LOG_URL = "https://www.sellersprite.com/v2/export-log"


def main() -> int:
    cfg = Config.load("config.yaml")
    ctx = StepContext(cfg, Paths(cfg))
    with ctx.browser("sellersprite") as b:
        page = b.new_page()
        b.goto(page, EXPORT_LOG_URL, timeout=60000)
        time.sleep(6)
        print("URL:", page.url)
        trs = page.locator("table tbody tr, table tr")
        print("tr 数量:", trs.count())
        for i in range(min(trs.count(), 3)):
            try:
                html = trs.nth(i).evaluate("e => e.outerHTML")
                print(f"=== tr[{i}] outerHTML(前1200字) ===")
                print(html[:1200])
                print()
            except Exception as e:
                print(f"tr[{i}] 读取失败: {e}")
        # 页面所有 checkbox
        cbs = page.locator("input[type=checkbox], .el-checkbox__original")
        print("页面 checkbox 总数:", cbs.count())
        for i in range(min(cbs.count(), 8)):
            el = cbs.nth(i)
            try:
                vis = el.is_visible()
            except Exception:
                vis = "?"
            try:
                parent = el.evaluate(
                    "e => { const p = e.parentElement; return p ? p.outerHTML.slice(0,200) : ''; }"
                )
            except Exception:
                parent = ""
            print(f"  checkbox#{i} visible={vis} parentHTML={parent[:180]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
