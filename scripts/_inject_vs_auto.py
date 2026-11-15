# -*- coding: utf-8 -*-
"""只读调查: 各类目「人工注入词」与「自动注入词(核心词清单自动态)」的差异。

目的(用户提问): 步骤10 的注入词是不是已经被「关键词清单(核心/长尾)」覆盖、可以删?
本脚本对**磁盘上真实存在的** filter-<CAT>-inject-words.txt 逐个比对:
  - 人工清单词 (filter-<CAT>-inject-words.txt)
  - 自动词 = 核心词清单自动态 = inject_panel().auto_qualified
      US 站: <CAT>-screen-results.json verdict==达标 全集
      非US : 站内月搜索量前 NON_US_QUALIFIED_TOP(5)
输出: 人工独有 / 自动独有 / 交集, 以及是否同一批。
不写任何文件(除 stdout 重定向)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.category_config import (          # noqa: E402
    NON_US_QUALIFIED_TOP,
    is_us_site,
    pick_top_volume_candidates,
)
DATA = ROOT / "data"


def _cfg_site(base: Path) -> tuple[str, str]:
    """从 config.yaml 粗取 SITE/MARKET(不依赖 webui.server 的 YAML 加载器)。"""
    p = base / "config.yaml"
    site = market = ""
    if not p.is_file():
        return site, market
    for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = ln.strip()
        for key, box in (("SITE", "site"), ("MARKET", "market")):
            if s.upper().startswith(key + ":") or s.upper().startswith(key + " :"):
                val = s.split(":", 1)[1].strip().strip('"').strip("'")
                if box == "site":
                    site = val
                else:
                    market = val
    return site, market


def _txt(p: Path) -> list[str]:
    if not p.is_file():
        return []
    out: list[str] = []
    for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        w = ln.strip().lower()
        if w and not w.startswith("[") and w not in out:
            out.append(w)
    return out


def _json(p: Path):
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return []


def auto_words(base: Path, cat: str) -> tuple[list[str], str]:
    kw = _json(base / f"{cat}-amazon-keyword-data.json")
    site, market = _cfg_site(base)
    us = is_us_site(site=site, market=market, cat=cat)
    if not us:
        picked = [w.lower() for w in pick_top_volume_candidates(kw, NON_US_QUALIFIED_TOP)]
        if picked:
            return picked, f"非US({site or market}) 搜索量前{NON_US_QUALIFIED_TOP}"
    screen = _json(base / f"{cat}-screen-results.json")
    out: list[str] = []
    for r in screen if isinstance(screen, list) else []:
        if isinstance(r, dict) and str(r.get("verdict") or "").strip() == "达标":
            w = str(r.get("keyword") or "").strip().lower()
            if w and w not in out:
                out.append(w)
    return out, f"US({site or market}) FR-07达标全集" if us else "回退FR-07达标全集"


def main() -> int:
    rows = []
    for p in sorted(DATA.rglob("filter-*-inject-words.txt")):
        base = p.parent
        cat = base.name
        manual = _txt(p)
        auto, src = auto_words(base, cat)
        core_file = base / f"filter-{cat}-core-words.txt"
        core = _txt(core_file)
        ms, as_ = set(manual), set(auto)
        rows.append({
            "cat": cat,
            "manual": manual,
            "auto": auto,
            "src": src,
            "only_manual": [w for w in manual if w not in as_],
            "only_auto": [w for w in auto if w not in ms],
            "same": manual == auto,
            "core_file": core,
        })

    print(f"扫描到 {len(rows)} 个类目存在人工注入词清单\n")
    for r in rows:
        print("=" * 78)
        print(f"[{r['cat']}]  自动源={r['src']}")
        print(f"  人工注入词({len(r['manual'])}): {'、'.join(r['manual']) or '(空)'}")
        print(f"  自动注入词({len(r['auto'])}): {'、'.join(r['auto']) or '(空)'}")
        print(f"  完全一致? {'是' if r['same'] else '否'}")
        if r["only_manual"]:
            print(f"  人工独有({len(r['only_manual'])}): {'、'.join(r['only_manual'])}")
        if r["only_auto"]:
            print(f"  自动独有({len(r['only_auto'])}): {'、'.join(r['only_auto'])}")
        print(f"  已有核心词人工清单? {'有 ' + str(r['core_file']) if r['core_file'] else '无'}")

    n_same = sum(1 for r in rows if r["same"])
    n_diff = len(rows) - n_same
    print("\n" + "=" * 78)
    print(f"汇总: 共 {len(rows)} 个类目有注入词清单, 与自动口径完全一致 {n_same} 个, "
          f"有差异 {n_diff} 个")
    tot_om = sum(len(r["only_manual"]) for r in rows)
    tot_oa = sum(len(r["only_auto"]) for r in rows)
    print(f"      人工独有词合计 {tot_om} 个 / 自动独有词合计 {tot_oa} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
