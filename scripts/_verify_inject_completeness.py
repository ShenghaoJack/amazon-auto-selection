# -*- coding: utf-8 -*-
"""只读核对: 步骤10 到底注入了哪些词? 有没有被截断/过滤?

逐层对比(全部真实数据, 不调 LLM):
  ① keywords.txt [核心大词] 分区大小        (FR-04 产物: 全部大词候选)
  ② plan.core    = 生效核心大词             (auto=FR-07达标集 / manual=人工清单)
  ③ keywords.txt [长尾词] 分区大小          (FR-04 产物)
  ④ plan.longtail = 生效长尾词              (auto=全部长尾 / manual=人工清单)
  ⑤ _final_keywords() 返回                  (② ∪ ④, 去重)
  ⑥ filter-prompt 底稿 related_title
  ⑦ 实际写进判定提示词的 = ⑥ ∪ ⑤           (= 落盘的 <CAT>-injected-keywords.txt)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Config                      # noqa: E402
from core.keyword_lists import read_keywords_file   # noqa: E402
from core.paths import Paths                        # noqa: E402
from steps._filtering import enrich_prompt_with_keywords   # noqa: E402
from steps.base import StepContext                  # noqa: E402
from steps.step10_variants import Step10Variants    # noqa: E402

import json                                          # noqa: E402

CASES = [
    ("Salt and Pepper Mill Sets-US-202608", "美国", "amazon.com"),
    ("Chopping Boards-UK-202608", "英国", "amazon.co.uk"),
    ("Dog Dryer-US-202608", "美国", "amazon.com"),
    ("Air Purifiers for Home Pets-US-202608", "美国", "amazon.com"),
    ("Chopping Boards-FR-202608", "法国", "amazon.fr"),
]


def main() -> int:
    print("步骤10 注入词逐层核对(真实数据, 只读)\n")
    for cat, site, market in CASES:
        base = ROOT / "data" / cat
        cfg = Config({"CAT": cat, "DATA_ROOT": str(ROOT / "data"), "DRY_RUN": True,
                      "SITE": site, "MARKET": market})
        ctx = StepContext(cfg, Paths(cfg))
        s = Step10Variants()
        logs: list[str] = []
        s.log = lambda ctx, text, _l=logs: _l.append(text)      # type: ignore[assignment]

        kw_file = base / f"{cat}-keywords.txt"
        kw_core, kw_tail = read_keywords_file(kw_file) if kw_file.is_file() else ([], [])
        final = s._final_keywords(ctx, cat)
        plan_line = logs[0] if logs else "(无日志)"

        # 重新解析一次 plan, 拿到 core/longtail 各自规模
        from core.category_config import is_us_site as _is_us
        from core.keyword_lists import resolve as _resolve
        auto_core = s._auto_core(ctx, cat)
        us_site = _is_us(site=site, market=market, cat=cat)
        plan = _resolve(base, cat, auto_core=auto_core, auto_longtail=kw_tail,
                        screen_verdict=s._screen_verdict(ctx, cat),
                        gate=2, gate_applies=us_site)

        fp_path = base / f"filter-{cat}-prompt.json"
        fp = {}
        if fp_path.is_file():
            fp = json.loads(fp_path.read_text(encoding="utf-8-sig", errors="replace"))
        related = [str(x).strip().lower() for x in (fp.get("related_title") or [])]

        # 模拟 run(): enrich 后再看真正进提示词的词
        merged = enrich_prompt_with_keywords(dict(fp), final) or {}
        injected = list(merged.get("达标关键词") or [])

        print("=" * 78)
        print(f"[{cat}]")
        print(f"  {plan_line}")
        print(f"  ① keywords.txt [核心大词] 分区     : {len(kw_core)} 个")
        print(f"  ② plan.core 生效核心大词          : {len(plan.core)} 个  (来源={plan.mode_label})")
        print(f"  ③ keywords.txt [长尾词] 分区      : {len(kw_tail)} 个")
        print(f"  ④ plan.longtail 生效长尾词        : {len(plan.longtail)} 个")
        print(f"  ⑤ _final_keywords() 返回          : {len(final)} 个  (= ②∪④ 去重)")
        print(f"  ⑥ filter-prompt 底稿 related_title: {len(related)} 个")
        print(f"  ⑦ 实际进判定提示词的(⑥∪⑤)        : {len(injected)} 个")
        # 是否被截断: 核心+长尾 应完整出现在注入列表里
        missing = [w for w in final if w not in injected]
        print(f"     -> 核心+长尾中缺失的: {missing if missing else '无(全部注入)'}")
        print(f"     -> 提示词里是否出现过长尾词样例: "
              f"{'是, 例: ' + (kw_tail[0] if kw_tail and kw_tail[0] in injected else '否') if kw_tail else '该类目无长尾词'}")
        # 核心大词分区 vs 生效核心词 的差集(自动态下只注入 FR-07 达标的)
        diff = [w for w in kw_core if w not in final]
        if diff:
            print(f"     ⚠️ [核心大词]分区里**未被注入**的 {len(diff)} 个(自动态只注入 FR-07 判达标的):")
            print(f"        {'、'.join(diff[:12])}{'…' if len(diff) > 12 else ''}")
    print("\n核对完成(未写文件, 未调 LLM)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
