# -*- coding: utf-8 -*-
"""只读调查: 「注入词」在步骤10 过滤里到底起什么作用 —— 冲突消解实测。

背景: steps/_filtering.py::enrich_prompt_with_keywords() 做两件事:
  (a) 把注入词并入 "达标关键词"/related_title  -> 作为 LLM 判定"是否本类目整机"的参考信号
  (b) **把同时出现在 hard_exclude/accessories/consumables 里的注入词从中摘掉**
      -> 防止"自己的核心词被 FR-02 误判成配件/耗材"导致整机被剔除

本脚本量化 (b): 对每个类目, 统计注入词与三类排除词表的交集。
交集非空 => 删掉注入词机制后, 这些词会留在排除表里 -> 该词相关的整机可能被误剔除。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
KEYS = ("hard_exclude", "accessories", "consumables")


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
    print("注入词 × 排除词表 冲突消解实测\n")
    tot_conf = 0
    cats_with = 0
    n = 0
    for p in sorted(DATA.rglob("filter-*-inject-words.txt")):
        base, cat = p.parent, p.parent.name
        words = _txt(p)
        if not words:
            continue
        n += 1
        fp = _json(base / f"filter-{cat}-prompt.json")
        if not isinstance(fp, dict):
            print(f"[{cat}] 无 filter-prompt.json, 跳过\n")
            continue
        hits = []
        for key in KEYS:
            lst = [str(x).strip().lower() for x in (fp.get(key) or [])]
            inter = [w for w in words if w in lst]
            for w in inter:
                hits.append((key, w))
        print("=" * 78)
        print(f"[{cat}] 注入词 {len(words)} 个")
        for key in KEYS:
            lst = [str(x).strip().lower() for x in (fp.get(key) or [])]
            inter = [w for w in words if w in lst]
            print(f"   {key:14} 共 {len(lst):2} 词" + (f"  ← 与注入词交集 {inter}" if inter else ""))
        if hits:
            cats_with += 1
            tot_conf += len(hits)
            print(f"   >>> 冲突消解生效: {len(hits)} 个词会被从排除表摘出: "
                  + "、".join(f"{w}({k})" for k, w in hits))
        else:
            print("   >>> 无交集(删掉注入词对排除表无影响)")
        print()

    print("=" * 78)
    print(f"汇总: 有注入词的类目 {n} 个, 其中 {cats_with} 个存在'自己的词被写进排除表'的情况, "
          f"合计 {tot_conf} 处冲突消解")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
