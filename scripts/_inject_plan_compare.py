# -*- coding: utf-8 -*-
"""只读调查: 三种口径下步骤10 判定参考词的规模对比。

对比对象(都是"送到 LLM 的判定参考词"= related_title ∪ X):
  N0    真·全删   : X = 空          -> 只有 FR-02 底稿 related_title
  N_auto 方案A'(不迁移): X = 生效核心词(自动态 = FR-07达标全集 / 非US搜索量前5)
  N_now  今天     : X = 人工注入词 filter-<CAT>-inject-words.txt (无则自动)
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


def _site(base: Path):
    p = base / "config.yaml"
    site = market = ""
    if p.is_file():
        for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s = ln.strip()
            up = s.upper()
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
        picked = [w.lower() for w in
                  pick_top_volume_candidates(_json(base / f"{cat}-amazon-keyword-data.json") or [],
                                             NON_US_QUALIFIED_TOP)]
        if picked:
            return picked
    out = []
    for r in _json(base / f"{cat}-screen-results.json") or []:
        if isinstance(r, dict) and str(r.get("verdict") or "").strip() == "达标":
            w = str(r.get("keyword") or "").strip().lower()
            if w and w not in out:
                out.append(w)
    return out


def _merged(related, extra):
    m = list(related)
    for w in extra:
        if w not in m:
            m.append(w)
    return m


def main() -> int:
    print("步骤10 判定参考词规模: 真·全删 vs 方案A'(接生效核心词) vs 今天\n")
    hdr = f"{'类目':<40}{'底稿':>5}{'N0':>5}{'N_auto':>8}{'N_now':>7}   A′相对今天"
    print(hdr)
    print("-" * 84)
    t0 = ta = tn = 0
    for p in sorted(DATA.rglob("filter-*-inject-words.txt")):
        base, cat = p.parent, p.parent.name
        inj = _txt(p)
        fp = _json(base / f"filter-{cat}-prompt.json") or {}
        related = [str(x).strip().lower() for x in (fp.get("related_title") or [])]
        auto = _auto_core(base, cat)
        n0 = len(_merged(related, []))
        na = len(_merged(related, auto))
        nn = len(_merged(related, inj))
        t0, ta, tn = t0 + n0, ta + na, tn + nn
        delta = na - nn
        note = "持平" if delta == 0 else (f"{delta:+d}")
        print(f"{cat:<40}{len(related):>5}{n0:>5}{na:>8}{nn:>7}   {note}")
    print("-" * 84)
    print(f"{'合计':<40}{'':>5}{t0:>5}{ta:>8}{tn:>7}   {ta - tn:+d}")
    print()
    print(f"真·全删   : 参考词总量 {t0}  (相对今天 -{tn - t0}, 丢 {(tn - t0) / tn * 100:.0f}%)")
    print(f"方案A'    : 参考词总量 {ta}  (相对今天 -{tn - ta}, 丢 {(tn - ta) / tn * 100:.0f}%)")
    print(f"今天      : 参考词总量 {tn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
