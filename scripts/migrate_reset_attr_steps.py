# -*- coding: utf-8 -*-
"""一次性迁移：清理重编号(16/17/18 重构)后各旧类目的 step-status.txt 里的 16/17/18 旧状态。

背景：旧流水线里 16=产品属性标注、17=备份；重构后 16=属性集提取与确认、17=属性标注与标准化、
18=备份。旧类目的 step-status.txt 仍写着 `16|DONE`/`17|DONE`，看板会把这些旧状态误显示成
新步骤的绿色 DONE，但新产物(attribute-spec.json / 两张 xlsx)并不存在 → 卡片「绿 DONE 却没文件」。

本脚本只**清掉 >=16 的状态行**(保留 0-15 的 DONE)，让 16/17/18 回到 PENDING；不删任何产物。
之后续跑会自动从步骤16 重跑(或配合 validate_artifacts 自愈)。
"""
from __future__ import annotations

import glob
import os
import sys


def migrate_one(status_path: str) -> list[str]:
    kept: list[str] = []
    removed: list[str] = []
    with open(status_path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                num = int(line.split("|")[0])
            except ValueError:
                continue
            if num >= 16:
                removed.append(line)
            else:
                kept.append(line)
    if removed:
        with open(status_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))
    return removed


def main() -> int:
    pattern = os.path.join("data", "*", "20_备份与监控", "step-status.txt")
    paths = sorted(glob.glob(pattern))
    if not paths:
        print("未找到 step-status.txt")
        return 1
    n = 0
    for p in paths:
        removed = migrate_one(p)
        if removed:
            n += 1
            print("%s: 移除 %d 行(步骤>=16 旧状态)" % (p, len(removed)))
    print("完成: %d 个类目已清理 16/17/18 旧状态" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
