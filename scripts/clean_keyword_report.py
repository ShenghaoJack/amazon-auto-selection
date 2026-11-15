#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清理关键词报告里的"方法/口径行"脏数据(2026-10-10 用户要求做成机制)。

## 背景

`<CAT>-keyword-report.xlsx` 的 `长尾词` sheet 末尾混着 3 行方法行(`长尾词`=`搜索配置`,
方法标签写在 `维度` 列)。全仓库审计 **35/35** 份报告都是这个形态 —— 下游会把 `搜索配置`
当成长尾词列出来(整合报告 2026-10-10 就出现过)。

判定规则与 step8 写入时的防御过滤共用同一套: `core/keyword_report_clean.py`。

## 用法

    # 1) 只体检(不改任何文件): 有脏数据 → 退出码 2
    python scripts/clean_keyword_report.py --check

    # 2) 真正清理(会先备份 .bak, 方法行搬到 `方法说明` sheet)
    python scripts/clean_keyword_report.py --apply

    # 3) 连 data/archive 里的历史报告一起处理
    python scripts/clean_keyword_report.py --check --all
    python scripts/clean_keyword_report.py --apply --all

也可以只处理指定文件:

    python scripts/clean_keyword_report.py --apply "data/Dog Dryer-US-202608/Dog Dryer-US-202608-keyword-report.xlsx"

退出码: `--check` 有脏数据 = 2 / 干净 = 0; `--apply` 正常 = 0; 文件不存在等错误 = 1。
幂等: 重复执行不会重复搬运, 第二次 `--check` 返回 0。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.keyword_report_clean import clean_workbook, default_reports, find_dirty   # noqa: E402


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")        # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog="clean_keyword_report.py",
                                 description="清理关键词报告 `长尾词` sheet 里的方法/口径行(脏数据)")
    ap.add_argument("paths", nargs="*", help="指定报告文件; 不传则按 --all 范围自动发现")
    ap.add_argument("--check", action="store_true", help="只体检, 不写盘(默认行为)")
    ap.add_argument("--apply", action="store_true", help="真正清理(备份 .bak + 原子替换)")
    ap.add_argument("--all", action="store_true", help="含 data/archive 下的历史报告")
    ap.add_argument("--no-backup", action="store_true", help="不写 .bak(默认写, 且只保留最初一份)")
    args = ap.parse_args(argv)

    targets = [Path(p) for p in args.paths] or default_reports(include_archive=args.all)
    targets = [p for p in targets if p.is_file()]
    if not targets:
        print("没找到关键词报告(检查 data/*/*keyword-report.xlsx 或显式传路径)")
        return 1

    mode = "apply" if args.apply else "check"
    print("=" * 78)
    print("关键词报告脏数据清理 | 模式=%s | 目标 %d 份%s"
          % (mode, len(targets), "(含 archive)" if args.all else "(不含 archive)"))
    print("=" * 78)
    dirty_files = 0
    removed = moved = 0
    for p in targets:
        if args.apply:
            r = clean_workbook(p, apply=True, backup=not args.no_backup)
            if r["dirty"]:
                dirty_files += 1
                removed += r["removed"]
                moved += r["moved"]
                print("  ✓ %-58s 移除 %d 行 → %s%s"
                      % (p.name[:58], r["removed"], r["moved"] and "方法说明" or "—",
                         ("  (备份 %s)" % Path(r["backup"]).name) if r["backup"] else ""))
            else:
                print("  · %-58s 无需处理" % p.name[:58])
        else:
            found = find_dirty(p)
            n = len(found.get("长尾词") or [])
            if n:
                dirty_files += 1
                for h in found["长尾词"]:
                    print("  ⚠ %-52s 第 %d 行: 长尾词=%s 维度=%s"
                          % (p.name[:52], h["row"], h["keyword"], h["dim"]))
            else:
                print("  ✓ %-58s 干净" % p.name[:58])
    print("-" * 78)
    if args.apply:
        print("处理完成: %d 份有脏数据, 共移除 %d 行, 搬入 `方法说明` %d 行。" % (dirty_files, removed, moved))
        print("再跑一次 --check 应为 0 脏数据(幂等)。")
        return 0
    if dirty_files:
        print("发现 %d/%d 份报告含方法行 → 加 `--apply` 清理。" % (dirty_files, len(targets)))
        return 2
    print("全部干净 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
