# -*- coding: utf-8 -*-
"""只读: 看 [核心大词] 里未被注入的那些词, FR-07 到底给了什么判定。

用途: 判断"自动态只注入 FR-07 达标词"是否合理 ——
  若未注入的词多为"数据不足", 说明它们只是缺指标而非不相关(可考虑一并注入);
  若多为"不达标", 说明 FR-07 是明确淘汰它们(不应注入)。
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.keyword_lists import read_keywords_file   # noqa: E402

CATS = [
    "Salt and Pepper Mill Sets-US-202608",
    "Air Purifiers for Home Pets-US-202608",
    "Cutting Boards-US-202608",
    "Dog Food Storage-US-202608",
    "Dog Slow Feeders-US-202608",
    "Food Processors-US-202608",
    "Grilling Prep and Serve Trays-US-202608",
    "Cat Fountains-US-202608",
    "Chopping Boards-UK-202608",
    "Chopping Boards-FR-202608",
    "Chopping Boards-DE-202608",
    "Dog Dryer-US-202608",
]


def main() -> int:
    print("[核心大词]分区里未被注入的词, FR-07 判定分布\n")
    grand = collections.Counter()
    for cat in CATS:
        base = ROOT / "data" / cat
        kw = base / f"{cat}-keywords.txt"
        sr = base / f"{cat}-screen-results.json"
        if not kw.is_file():
            continue
        kw_core, _tail = read_keywords_file(kw)
        verdict: dict[str, str] = {}
        if sr.is_file():
            try:
                for r in json.loads(sr.read_text(encoding="utf-8-sig", errors="replace")):
                    if isinstance(r, dict):
                        verdict[str(r.get("keyword") or "").strip().lower()] = \
                            str(r.get("verdict") or "").strip() or "(空)"
            except Exception:
                pass
        injected = {w for w, v in verdict.items() if v == "达标"}
        not_inj = [w for w in kw_core if w not in injected]
        dist = collections.Counter(verdict.get(w, "(无判定记录)") for w in not_inj)
        grand.update(dist)
        print(f"[{cat}]  [核心大词]{len(kw_core)} 个, 未注入 {len(not_inj)} 个 -> {dict(dist)}")
        for v in ("不达标", "数据不足", "(无判定记录)", "(空)"):
            ws = [w for w in not_inj if verdict.get(w, "(无判定记录)") == v]
            if ws:
                print(f"     {v}({len(ws)}): " + "、".join(ws[:7])
                      + ("…" if len(ws) > 7 else ""))
    print("\n" + "=" * 70)
    print(f"合计未注入词 {sum(grand.values())} 个, 判定分布: {dict(grand)}")
    tot = sum(grand.values())
    if tot:
        for k, v in grand.most_common():
            print(f"  {k:12} {v:3} 个  {v / tot * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
