# -*- coding: utf-8 -*-
"""诊断: 导出日志页勾选后的操作按钮(批量下载/批量删除等)。"""
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
        # 勾选前: 可见按钮
        def dump_buttons(tag):
            print(f"=== {tag}: 可见按钮 ===")
            loc = page.locator("button")
            seen = set()
            for i in range(loc.count()):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip().replace("\n", " ")
                    if t and t not in seen:
                        seen.add(t)
                        print(f"  {t[:50]}")
                except Exception:
                    pass
            # 含"批量/下载"文本的可见元素
            print(f"=== {tag}: 含'批量/下载'的可见元素 ===")
            for kw in ("批量下载", "批量删除", "下载"):
                try:
                    els = page.locator(f"text={kw}")
                    for i in range(min(els.count(), 6)):
                        el = els.nth(i)
                        try:
                            if el.is_visible():
                                cls = el.evaluate(
                                    "e => e.tagName.toLowerCase() + '.' + (e.className||'').toString().slice(0,60)"
                                )
                                print(f"  '{kw}': {cls} text={(el.inner_text() or '').strip()[:40]!r}")
                        except Exception:
                            pass
                except Exception:
                    pass

        dump_buttons("勾选前")
        # 勾选 2 个新任务(按 data-item-id 找)
        ids = []
        trs = page.locator("table tbody tr, table tr").all()
        for tr in trs:
            cells = [c.inner_text().strip() for c in tr.locator("td").all()]
            if len(cells) >= 5 and "查竞品" in cells[2] and cells[3].startswith("2026-09-02 11:15"):
                cb = tr.locator("input[type=checkbox]").first
                if cb.count() > 0:
                    ids.append(cb.get_attribute("data-item-id"))
        print("待勾选 item-id:", ids)
        for iid in ids:
            box = page.locator(f"input[data-item-id='{iid}']").first
            if box.count() > 0 and not box.is_checked():
                try:
                    box.check(timeout=5000)
                except Exception:
                    box.evaluate("el => el.click()")
        print("已勾选, 等待 2 秒...")
        time.sleep(2)
        dump_buttons("勾选后")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
