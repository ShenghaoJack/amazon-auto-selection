# -*- coding: utf-8 -*-
"""只读预检(修正版): 注入词同步为「生效核心词 + 长尾词」的影响。

只扫 **类目目录本级**的 <CAT>-keywords.txt (排除 04_关键词/ 等归档快照里的副本)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.category_config import (          # noqa: E402
    NON_US_QUALIFIED_TOP, is_us_site, pick_top_volume_candidates)
from core.keyword_lists import read_keywords_file   # noqa: E402

DATA = ROOT / "data"
EXCL = ("hard_exclude", "accessories", "consumables")


def _json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return None


def _site(base: Path):
    p = base / "config.yaml"
    site = market = ""
    if p.is_file():
        for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s, up = ln.strip(), ln.strip().upper()
            for key, box in (("SITE", "s"), ("MARKET", "m")):
                if up.startswith(key + ":") or up.startswith(key + " :"):
                    v = s.split(":", 1)[1].strip().strip('"').strip("'").split("#")[0].strip()
                    if box == "s":
                        site = v
                    else:
                        market = v
    return site, market


def _auto_core(base: Path, cat: str) -> list[str]:
    site, market = _site(base)
    if not is_us_site(site=site, market=market, cat=cat):
        picked = [w.lower() for w in pick_top_volume_candidates(
            _json(base / f"{cat}-amazon-keyword-data.json") or [], NON_US_QUALIFIED_TOP)]
        if picked:
            return picked
    out = []
    for r in _json(base / f"{cat}-screen-results.json") or []:
        if isinstance(r, dict) and str(r.get("verdict") or "").strip() == "达标":
            w = str(r.get("keyword") or "").strip().lower()
            if w and w not in out:
                out.append(w)
    return out


def _uniq(*lists):
    out = []
    for lst in lists:
        for w in lst or []:
            w = str(w).strip().lower()
            if w and w not in out:
                out.append(w)
    return out


def main() -> int:
    print("预检(仅类目本级活文件): 注入词 = 生效核心词 + 长尾词\n")
    print(f"{'类目':<42}{'底稿':>5}{'今天':>5}{'核心':>5}{'长尾':>5}{'同步后':>7}{'增减':>6}")
    print("-" * 80)
    rows = []
    seen = set()
    for kw in sorted(DATA.glob("*/*-keywords.txt")):
        base = kw.parent
        cat = base.name
        if kw.name != f"{cat}-keywords.txt":     # 排除 <CAT>-injected-keywords.txt
            continue
        if cat in seen:
            continue
        seen.add(cat)
        try:
            _cp, tail = read_keywords_file(kw)
        except Exception as exc:                     # noqa: BLE001
            print(f"[{cat}] 解析失败: {exc}")
            tail = []
        if not isinstance(tail, list):
            tail = []
        fp = _json(base / f"filter-{cat}-prompt.json") or {}
        related = [str(x).strip().lower() for x in (fp.get("related_title") or [])]
        core = _auto_core(base, cat)
        today = _uniq(related, core)
        sync = _uniq(related, core, tail)
        rows.append(dict(cat=cat, related=related, core=core, tail=tail,
                         today=today, sync=sync, fp=fp))
        print(f"{cat:<42}{len(related):>5}{len(today):>5}{len(core):>5}{len(tail):>5}"
              f"{len(sync):>7}{len(sync) - len(today):>+6}")

    print("-" * 80)
    t_today = sum(len(r["today"]) for r in rows)
    t_sync = sum(len(r["sync"]) for r in rows)
    print(f"合计 {len(rows)} 个类目(本级): 今天 {t_today} -> 同步后 {t_sync} "
          f"({t_sync - t_today:+d}, +{(t_sync - t_today) / max(1, t_today) * 100:.0f}%)")

    print("\n长尾词样例(前 6 个):")
    for r in rows:
        if r["tail"]:
            print(f"  {r['cat']:<42}{'、'.join(r['tail'][:6])}")

    print("\n" + "=" * 80)
    print("⚠️ 风险: 并集 ∩ 排除词表 (命中 = 该项会被 enrich 从排除表摘掉)")
    print("=" * 80)
    n_new = n_old = 0
    for r in rows:
        base_args = _uniq(r["related"], r["core"])
        for key in EXCL:
            lst = [str(x).strip().lower() for x in (r["fp"].get(key) or [])]
            old = [w for w in base_args if w in lst]
            new = [w for w in r["tail"] if w in lst and w not in base_args]
            if old:
                n_old += 1
                print(f"  [{r['cat']}] {key}: 今天已有 -> {old}")
            if new:
                n_new += 1
                print(f"  [{r['cat']}] {key}: **同步新增** -> {new}   <<< 新风险")
    print(f"\n今天已存在的交集 {n_old} 处 / 同步后**新增**的交集 {n_new} 处")
    if n_new == 0:
        print("=> 同步长尾词 **不会** 摘掉任何排除项, 对排除表零影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
