# -*- coding: utf-8 -*-
"""只读核对: 三份"核心词"清单的关系(决定「仅注入词池」该取哪一份)。

对比:
  A = keywords.txt `[核心大词]` 分区        (FR-04 全部大词候选)
  B = 面板候选池 keywords-candidates.txt    (`kwData.candidates` 的来源)
  C = 生效核心大词 plan.core                (步骤9 抓取 + 步骤10 注入)
  D = FR-07 判"达标"的词
问题: B 是否 == A? C 是否 ⊆ A? 未注入的 A 有多少(即"可以从核心词中选择"的候选量)?
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.category_config import is_us_site, pick_top_volume_candidates, NON_US_QUALIFIED_TOP  # noqa: E402
from core.keyword_lists import read_keywords_file   # noqa: E402

DATA = ROOT / "data"


def _json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return None


def _cfg(base: Path):
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


def main() -> int:
    print(f"{'类目':<42}{'A核心大词':>9}{'B候选池':>8}{'C生效核心':>9}{'D达标':>6}{'A未注入':>8}")
    print("-" * 88)
    tot_uninjected = 0
    same_ab = 0
    n = 0
    for kw in sorted(DATA.glob("*/*-keywords.txt")):
        base, cat = kw.parent, kw.parent.name
        if kw.name != f"{cat}-keywords.txt":
            continue
        A, _tail = read_keywords_file(kw)
        cand_path = base / f"{cat}-keywords-candidates.txt"
        B = [ln.strip().lower() for ln in cand_path.read_text(encoding="utf-8-sig").splitlines()
             if ln.strip()] if cand_path.is_file() else []
        screen = _json(base / f"{cat}-screen-results.json") or []
        verdict = {str(r.get("keyword") or "").strip().lower(): str(r.get("verdict") or "").strip()
                   for r in screen if isinstance(r, dict)}
        site, market = _cfg(base)
        us = is_us_site(site=site, market=market, cat=cat)
        if us:
            C = [w for w, v in verdict.items() if v == "达标"]
        else:
            C = [w.lower() for w in pick_top_volume_candidates(
                _json(base / f"{cat}-amazon-keyword-data.json") or [], NON_US_QUALIFIED_TOP)]
        D = [w for w, v in verdict.items() if v == "达标"]
        uninj = [w for w in A if w not in C]
        tot_uninjected += len(uninj)
        n += 1
        if set(A) == set(B):
            same_ab += 1
        print(f"{cat:<42}{len(A):>9}{len(B):>8}{len(C):>9}{len(D):>6}{len(uninj):>8}")
    print("-" * 88)
    print(f"类目数 {n}; A==B(核心大词分区 与 候选池 同集合) 的类目: {same_ab}/{n}")
    print(f"「核心大词里未注入」的候选总数: {tot_uninjected}  <- 这就是「仅注入词池」可选的量")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
