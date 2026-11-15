# -*- coding: utf-8 -*-
"""只读对比: 三种"注入源"口径的规模与**风险**(会否摘掉排除项)。

口径:
  现 状 = plan.core(生效核心词, 自动态=FR-07达标集) ∪ plan.longtail
  方案D = 自动态用 keywords.txt [核心大词]+[长尾词] **全分区**; 人工态仍用人工清单
  全注入 = 任何态都用 [核心大词]+[长尾词] 全分区

风险 = 注入词 ∩ (hard_exclude/accessories/consumables)
     —— enrich_prompt_with_keywords() 会把命中的词**从排除表里摘掉**,
        若摘掉的是真配件/耗材词 -> 该排除规则失效 -> 可能放行配件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.category_config import is_us_site, pick_top_volume_candidates, NON_US_QUALIFIED_TOP  # noqa: E402
from core.keyword_lists import read_keywords_file, resolve as resolve_kw   # noqa: E402

DATA = ROOT / "data"
EXCL = ("hard_exclude", "accessories", "consumables")


def _json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return None


def _cfg(base: Path) -> tuple[str, str]:
    site = market = ""
    p = base / "config.yaml"
    if p.is_file():
        for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s, up = ln.strip(), ln.strip().upper()
            for k, box in (("SITE", "s"), ("MARKET", "m")):
                if up.startswith(k + ":") or up.startswith(k + " :"):
                    v = s.split(":", 1)[1].strip().strip('"').strip("'").split("#")[0].strip()
                    if box == "s":
                        site = v
                    else:
                        market = v
    return site, market


def _uniq(*ls):
    out = []
    for l in ls:
        for w in l or []:
            w = str(w).strip().lower()
            if w and w not in out:
                out.append(w)
    return out


def main() -> int:
    print(f"{'类目':<42}{'现状':>6}{'方案D':>7}{'全注入':>7}   {'方案D新增∩排除表':<20}")
    print("-" * 96)
    rows = []
    for kw in sorted(DATA.glob("*/*-keywords.txt")):
        base, cat = kw.parent, kw.parent.name
        if kw.name != f"{cat}-keywords.txt":
            continue
        fp = _json(base / f"filter-{cat}-prompt.json") or {}
        related = [str(x).strip().lower() for x in (fp.get("related_title") or [])]
        kw_core, kw_tail = read_keywords_file(kw)
        screen = _json(base / f"{cat}-screen-results.json") or []
        verdict = {str(r.get("keyword") or "").strip().lower():
                   str(r.get("verdict") or "").strip()
                   for r in screen if isinstance(r, dict)}
        site, market = _cfg(base)
        us = is_us_site(site=site, market=market, cat=cat)
        if not us:
            auto_core = [w.lower() for w in pick_top_volume_candidates(
                _json(base / f"{cat}-amazon-keyword-data.json") or [], NON_US_QUALIFIED_TOP)]
        else:
            auto_core = [w for w, v in verdict.items() if v == "达标"]
        plan = resolve_kw(base, cat, auto_core=auto_core, auto_longtail=kw_tail,
                          screen_verdict=verdict, gate=2, gate_applies=us)
        now = _uniq(related, plan.core, plan.longtail)
        full = _uniq(related, kw_core, kw_tail)
        # 方案D: 人工态用人工清单; 自动态用全分区
        if plan.is_manual:
            d = _uniq(related, plan.core, plan.longtail)
        else:
            d = full
        ex = []
        for k in EXCL:
            lst = [str(x).strip().lower() for x in (fp.get(k) or [])]
            ex += [w for w in d if w in lst and w not in now]
        rows.append((cat, len(now), len(d), len(full), ex, plan.mode))
        print(f"{cat:<42}{len(now):>6}{len(d):>7}{len(full):>7}   "
              + (f"⚠️ {ex}" if ex else "无(安全)"))

    print("-" * 96)
    print(f"合计: 现状 {sum(r[1] for r in rows)} -> 方案D {sum(r[2] for r in rows)} "
          f"-> 全注入 {sum(r[3] for r in rows)}")
    risk = [(r[0], r[4]) for r in rows if r[4]]
    print(f"\n方案D 新增命中排除表的类目: {len(risk)} 个")
    for cat, ex in risk:
        print(f"  [{cat}] {ex}")
    if not risk:
        print("  => 方案D 不会摘掉任何排除项(零风险)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
