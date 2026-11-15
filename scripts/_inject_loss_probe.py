# -*- coding: utf-8 -*-
"""只读调查: 删掉注入词后, 送给 LLM 的"判定参考词"会少掉什么?

步骤10 判定提示词里那一行是:
    {label}: {qualify}          # steps/_filtering.py:116
  fp["达标关键词"] 存在 -> label="达标关键词/相关标题词", qualify=达标关键词(含底稿)
  不存在             -> label="相关标题词",            qualify=related_title(仅 FR-02 底稿)

enrich_prompt_with_keywords() 的实际合并语义:
  qualify = (旧 达标关键词 or related_title) + 注入词
=> 有注入词 = related_title ∪ 注入词 ; 无注入词 = related_title

本脚本量化: 删掉注入词后丢失的词数 / 丢失比例, 以及 related_title 底稿本身的规模。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"


def _txt(p: Path) -> list[str]:
    if not p.is_file():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        w = ln.strip().lower()
        if w and not w.startswith("[") and w not in out:
            out.append(w)
    return out


def _json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return None


def main() -> int:
    print("删掉注入词后 '判定参考词' 的损失(步骤10 判定提示词第 116 行)\n")
    print(f"{'类目':<42}{'底稿':>5}{'注入词':>7}{'丢失':>6}{'丢/总':>8}")
    print("-" * 70)
    rows = []
    for p in sorted(DATA.rglob("filter-*-inject-words.txt")):
        base, cat = p.parent, p.parent.name
        inj = _txt(p)
        if not inj:
            continue
        fp = _json(base / f"filter-{cat}-prompt.json") or {}
        related = [str(x).strip().lower() for x in (fp.get("related_title") or [])]
        merged = list(related)
        for w in inj:
            if w not in merged:
                merged.append(w)
        lost = [w for w in merged if w not in related]
        pct = (len(lost) / len(merged) * 100) if merged else 0.0
        rows.append((cat, related, inj, lost, merged, pct))
        print(f"{cat:<42}{len(related):>5}{len(inj):>7}{len(lost):>6}{pct:>7.0f}%")

    print("-" * 70)
    if rows:
        tot_m = sum(len(r[4]) for r in rows)
        tot_l = sum(len(r[3]) for r in rows)
        print(f"合计: 参考词总量 {tot_m} 个, 删掉注入词后丢失 {tot_l} 个 "
              f"({tot_l / tot_m * 100:.0f}%)")
        empty = [r[0] for r in rows if not r[1]]
        if empty:
            print(f"\n⚠️ FR-02 底稿 related_title 为空的类目({len(empty)}): " + "、".join(empty))
            print("   -> 这些类目删掉注入词后, 判定提示词的'参考词'槽位将【完全为空】,")
            print("      LLM 只能靠'本类目产品'定义与硬排除/配件/耗材词判整机。")
        biggest = max(rows, key=lambda r: r[5])
        print(f"\n损失比例最大: {biggest[0]} ({biggest[5]:.0f}%: "
              f"底稿{len(biggest[1])} -> 合并后{len(biggest[4])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
