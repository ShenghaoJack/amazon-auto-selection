# -*- coding: utf-8 -*-
"""只读冒烟: 用**真实类目数据**验证步骤10 新词源 + webui 面板函数。

不调用任何 LLM / 不写任何文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Config            # noqa: E402
from core.paths import Paths              # noqa: E402
from steps.base import StepContext        # noqa: E402
from steps.step10_variants import Step10Variants   # noqa: E402

CATS = [
    "Salt and Pepper Mill Sets-US-202608",
    "Chopping Boards-UK-202608",
    "Dog Dryer-US-202608",
]


def main() -> int:
    print("=== ① 步骤10 新词源(真实类目, 只读) ===")
    for cat in CATS:
        cfg = Config({"CAT": cat, "DATA_ROOT": str(ROOT / "data"), "DRY_RUN": True,
                      "SITE": "美国" if cat.endswith("US-202608") else "英国",
                      "MARKET": "amazon.com" if cat.endswith("US-202608") else "amazon.co.uk"})
        ctx = StepContext(cfg, Paths(cfg))
        s = Step10Variants()
        logs: list[str] = []
        s.log = lambda ctx, text, _l=logs: _l.append(text)      # type: ignore[assignment]
        words = s._final_keywords(ctx, cat)
        print(f"\n[{cat}]  判定参考词 {len(words)} 个")
        for ln in logs:
            print("   " + ln)
        print("   词: " + "、".join(words[:14]) + ("…" if len(words) > 14 else ""))
        # 与旧口径对比: 旧 = 人工注入词文件(若存在) or FR-07 达标全集
        old_path = ROOT / "data" / cat / f"filter-{cat}-inject-words.txt"
        old = []
        if old_path.is_file():
            old = [x.strip().lower() for x in
                   old_path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        print(f"   旧人工注入词文件词数 {len(old)}"
              + ("(已失效, 文件仍在磁盘)" if old else "(无)"))

    print("\n=== ② webui 面板函数(真实类目, 只读) ===")
    sys.path.insert(0, str(ROOT / "webui"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("wserver", ROOT / "webui" / "server.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    S.PROJECT_ROOT = ROOT
    for cat in CATS[:1]:
        kc = S.keyword_candidates(cat, str(ROOT / "data"))
        print(f"[{cat}] keyword_candidates: ok={kc.get('ok')} "
              f"候选={len(kc.get('candidates') or [])} 自动核心={len(kc.get('auto_qualified') or [])} "
              f"site_scope={kc.get('site_scope')}")
        p = S.keyword_lists_panel(cat, str(ROOT / "data"))
        print(f"[{cat}] keyword_lists_panel: ok={p.get('ok')} mode={p.get('mode')} "
              f"核心={len((p.get('core') or {}).get('effective') or [])} "
              f"长尾={len((p.get('longtail') or {}).get('effective') or [])}")
        print(f"   残留字段检查: mode={p.get('mode')} 不再有 inject_file={'inject_file' not in p}")
    print("\n冒烟完成(未写任何文件, 未调用 LLM)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
