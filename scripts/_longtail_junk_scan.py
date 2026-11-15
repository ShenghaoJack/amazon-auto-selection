# -*- coding: utf-8 -*-
"""只读预检: 长尾词里是否混有"配件/耗材"性质的词(会让 LLM 把配件当整机)。

同步后这些词会作为"本类目相关词"证据送进判定提示词。若长尾词里出现
replacement/refill/filter/blade/cover/liner 之类词根, 属于伪证据 -> 需人工剔除。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.keyword_lists import read_keywords_file   # noqa: E402

DATA = ROOT / "data"
# 配件/耗材/服务 词根(命中即疑似伪证据)
ROOTS = ("replacement", "refill", "refillable", "filter", "blade", "cover", "liner",
         "brush", "pad", "bag", "cartridge", "parts", "part ", "accessor", "stand",
         "holder", "rack", "mat", "tray liner", "cleaner", "clean", "cover", "case",
         "cable", "charger", "adapter", "manual", "book", "sticker", "gift")


def main() -> int:
    print("长尾词 '配件/耗材词根' 扫描(仅类目本级 <CAT>-keywords.txt)\n")
    total = 0
    bad = 0
    for kw in sorted(DATA.glob("*/*-keywords.txt")):
        base = kw.parent
        cat = base.name
        if kw.name != f"{cat}-keywords.txt":
            continue
        try:
            _c, tail = read_keywords_file(kw)
        except Exception:                              # noqa: BLE001
            continue
        if not tail:
            continue
        hits = sorted({w for w in tail for r in ROOTS if r in w})
        total += len(tail)
        if hits:
            bad += len(hits)
            print(f"[{cat}] 长尾 {len(tail)} 词 -> 疑似 {len(hits)}:")
            for h in hits:
                print(f"    - {h}")
        else:
            print(f"[{cat}] 长尾 {len(tail)} 词 -> 无疑似")
    print(f"\n合计长尾 {total} 词, 疑似配件/耗材词 {bad} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
