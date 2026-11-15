# -*- coding: utf-8 -*-
"""实测: headless 模式下 v3/reversing 真实查询(2 个父体, 消耗 1 组配额)。"""
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

    # 手动构造 headless 的 sellersprite 浏览器(绕过 base.py 强制 headed)
    from core.browser import BrowserAutomation

    with BrowserAutomation(cfg, profile_dir=Paths(cfg).profile_ss(), headed=False) as b:
        page = b.new_page()
        b.goto(page, "https://www.sellersprite.com/v3/reversing")
        time.sleep(6)
        body = ""
        try:
            body = page.inner_text("body") or ""
        except Exception:
            pass
        print("v3/reversing 含'未登录':", "未登录" in body, " 含'立即查询':", "立即查询" in body)
        step._select_site(ctx, b, page, str(cfg.get("SITE", "美国") or "美国"))
        step._select_month(ctx, b, page, str(cfg.get("TRAFFIC_MONTH", "") or ""))
        step._fill_asins(ctx, b, page, parents)
        step._click_query(ctx, b, page)
        print("已点击查询, 等待 15 秒...")
        time.sleep(15)
        body2 = ""
        try:
            body2 = page.inner_text("body") or ""
        except Exception:
            pass
        print("el-message--error:", page.locator(".el-message--error").count())
        print("el-table__row:", page.locator(".el-table__row").count())
        print("导出按钮:", page.locator("button:has-text('导出')").count())
        print("含'未登录':", "未登录" in body2)
        # 找查询结果提示
        for kw in ("查询成功", "无数据", "未查询到", "积分", "结果"):
            if kw in body2:
                print(f"含'{kw}'")
        print("片段:", body2[:400].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
