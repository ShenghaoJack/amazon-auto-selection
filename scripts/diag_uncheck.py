# -*- coding: utf-8 -*-
"""诊断: 查竞品页查询后"展示所有变体"勾选框的 DOM 结构。"""
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
    parents = [
        l.strip()
        for l in open(
            "data/salt-pepper-shaker-sets/salt-pepper-shaker-sets-parents.txt",
            encoding="utf-8",
        )
        if l.strip()
    ][:2]
    with ctx.browser("sellersprite") as b:
        page = b.new_page()
        b.goto(page, "https://www.sellersprite.com/v3/competitor-lookup")
        time.sleep(8)
        # 填 ASIN(JS 解除 disabled)
        inp = page.locator("input[placeholder*='ASIN']").first
        try:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.removeAttribute('disabled');
                }""",
                "input[placeholder*='ASIN']",
            )
        except Exception:
            pass
        try:
            inp.fill("\n".join(parents))
            print("ASIN 已填入")
        except Exception as e:
            print("填 ASIN 失败:", e)
        # 立即查询
        try:
            page.get_by_text("立即查询", exact=False).first.click(timeout=8000)
            print("已点击立即查询")
        except Exception as e:
            print("点击查询失败:", e)
        # 等结果表格(最多 60s)
        try:
            page.wait_for_selector("table", timeout=60000)
            print("结果表格出现")
        except Exception:
            print("表格超时未出现, 继续诊断")
        time.sleep(3)
        # 找 展示所有变体
        print("=== '展示所有变体' 文本元素 ===")
        els = page.locator("text=展示所有变体")
        print("数量:", els.count())
        for i in range(els.count()):
            el = els.nth(i)
            try:
                vis = el.is_visible()
            except Exception:
                vis = "?"
            try:
                chain = el.evaluate(
                    """e => {
                        const out = [];
                        let p = e;
                        for (let j = 0; j < 6 && p; j++, p = p.parentElement) {
                            out.push(p.tagName.toLowerCase() + '.' + (p.className || '').toString().slice(0, 70));
                        }
                        return out;
                    }"""
                )
            except Exception:
                chain = []
            print(f"  [{i}] visible={vis} chain={chain}")
        # 页面所有 checkbox
        print("=== 可见 checkbox ===")
        cbs = page.locator("input[type=checkbox]")
        for i in range(cbs.count()):
            el = cbs.nth(i)
            try:
                vis = el.is_visible()
                checked = el.is_checked()
            except Exception:
                vis, checked = "?", "?"
            try:
                chain = el.evaluate(
                    """e => {
                        const out = [];
                        let p = e.parentElement;
                        for (let j = 0; j < 4 && p; j++, p = p.parentElement) {
                            out.push(p.tagName.toLowerCase() + '.' + (p.className || '').toString().slice(0, 60));
                        }
                        return out;
                    }"""
                )
            except Exception:
                chain = []
            if vis is True or "展示" in str(chain):
                print(f"  #{i} visible={vis} checked={checked} chain={chain}")
        # 表格区域文本
        try:
            body = page.inner_text("body") or ""
            idx = body.find("展示所有变体")
            print("=== 含'展示所有变体'的正文片段 ===")
            print(body[max(0, idx - 300): idx + 300].replace("\n", " | "))
        except Exception as e:
            print("正文片段失败:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
