# -*- coding: utf-8 -*-
"""只读核对: 「仅注入词池」实际会展示哪些词(模拟前端过滤逻辑)。

前端逻辑(webui/index.html::renderKwInjectPool):
    injected = Set(core.effective ∪ kwEdit.core)
    池子     = candidates.filter(c => !injected.has(c.word))
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("ws_pool", ROOT / "webui" / "server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

CATS = [
    "Salt and Pepper Mill Sets-US-202608",
    "Air Purifiers for Home Pets-US-202608",
    "Chopping Boards-UK-202608",
    "Dog Dryer-US-202608",
    "Food Processors-US-202608",
]


def main() -> int:
    print(f"{'类目':<40}{'候选池':>6}{'生效核心':>8}{'仅注入池可选':>12}")
    print("-" * 68)
    for cat in CATS:
        p = S.keyword_lists_panel(cat, str(ROOT / "data"))
        cands = p.get("candidates") or []
        core = p.get("core") or {}
        injected = set(core.get("effective") or []) | set(core.get("manual") or [])
        pool = [x for x in cands if x["word"] not in injected]
        print(f"{cat:<40}{len(cands):>6}{len(injected):>8}{len(pool):>12}")
        if pool:
            sample = "、".join(x["word"] for x in pool[:7])
            print(f"      可选样例: {sample}" + ("…" if len(pool) > 7 else ""))
    print("\n注: 池子里的词点一下即成为「仅注入词」—— 参与步骤10 判定, 不被步骤9 抓取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
