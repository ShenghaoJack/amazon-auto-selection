# -*- coding: utf-8 -*-
"""删除"我的导出"页中今天 11 点后提交的查竞品任务(重做提交前清理错误任务)。

安全逻辑: 解析导出表格, 精确匹配 来源=查竞品 且 提交时间 >= 2026-09-02 11:00,
数量必须恰好等于预期(默认2)才执行勾选+批量删除, 否则中止不删任何任务。
用法:
  python -X utf8 scripts/delete_ss_tasks.py [--since "2026-09-02 11:00"] [--expect 2]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.paths import Paths
from steps.base import StepContext

EXPORT_LOG_URL = "https://www.sellersprite.com/v2/export-log"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-02 11:00", help="只删提交时间 >= 此值的查竞品任务")
    ap.add_argument("--expect", type=int, default=2, help="预期匹配数量, 不符则中止")
    args = ap.parse_args()

    cfg = Config.load("config.yaml")
    ctx = StepContext(cfg, Paths(cfg))
    with ctx.browser("sellersprite") as b:
        page = b.new_page()
        b.goto(page, EXPORT_LOG_URL, timeout=60000)
        time.sleep(5)
        # 解析表格(列: tds[1]=任务名, tds[2]=来源, tds[3]=时间, tds[4]=状态)
        trs = page.locator("table tbody tr, table tr").all()
        targets = []
        all_rows = []
        for tr in trs:
            cells = [c.inner_text().strip() for c in tr.locator("td").all()]
            if len(cells) < 5:
                continue
            name = cells[1].replace("最新", "").strip()
            source = cells[2] or ""
            time_raw = (cells[3] or "").split("\n")[0].strip()
            status = cells[4] or ""
            if not name or not source:
                continue
            all_rows.append((name, source, time_raw, status, tr))
            if "查竞品" in source and time_raw >= args.since:
                targets.append((name, source, time_raw, status, tr))
        print(f"导出表格任务行: {len(all_rows)}")
        for n, s, t, st, _ in all_rows:
            print(f"  {n} | {s} | {t} | {st}")
        print(f"匹配待删除(来源=查竞品 且 时间>={args.since}): {len(targets)} 条")
        if len(targets) != args.expect:
            print(f"!! 匹配数量 {len(targets)} != 预期 {args.expect}, 中止不删除")
            return 2
        # 勾选
        checked = 0
        for _, _, _, _, tr in targets:
            try:
                box = tr.locator("input[type=checkbox], .el-checkbox__original").first
                if box.count() > 0 and not box.is_checked():
                    box.check(timeout=5000)
                checked += 1
            except Exception as e:
                print(f"勾选失败: {e}")
        print(f"已勾选 {checked} 条")
        if checked == 0:
            print("未勾选任何任务, 中止")
            return 2
        # 批量删除
        try:
            page.get_by_text("批量删除", exact=False).first.click(timeout=8000)
            print("已点击批量删除")
        except Exception as e:
            print(f"点击批量删除失败: {e}")
            return 2
        time.sleep(2)
        # 确认弹窗(确定/确认/删除)
        for kw in ("确定", "确认", "删除"):
            try:
                btn = page.locator(f"button:has-text('{kw}')").first
                if btn.is_visible():
                    btn.click(timeout=5000)
                    print(f"已点击确认按钮: {kw}")
                    break
            except Exception:
                continue
        time.sleep(3)
        print("删除操作完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
