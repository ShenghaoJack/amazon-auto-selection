# -*- coding: utf-8 -*-
"""词表放权 CLI（方案 B 的"批量确认"入口，零第三方依赖，不做 LLM 调用）。

用法（在项目根目录）：
  # 1) 把稳定性结果.json 里的新词候选转成"独立初审提示词"（给 subagent / 外部模型用）
  python tools/attr_stability/onboard_cli.py --result "data/<类目>/17_属性标注/稳定性结果.json" --prompt

  # 2) 把初审模型的裁决（items）批量写回 rules.json 的 attributes[].values
  python tools/attr_stability/onboard_cli.py --rules "data/<类目>/<snake>.rules.json" --apply 裁决.json

  # 裁决.json 形如：{"items":[{"属性":"供电方式","词":"太阳能充电","结论":"采纳","规范化写法":"太阳能供电"}]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.attr_stability import apply_vocab_acceptance, build_vocab_review_prompt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="新词候选：初审提示词 / 批量确认写回")
    ap.add_argument("--result", default="", help="稳定性结果.json 路径（--prompt 用）")
    ap.add_argument("--prompt", action="store_true", help="打印新词初审提示词")
    ap.add_argument("--rules", default="", help="rules.json 路径（--apply 用）")
    ap.add_argument("--apply", default="", help="裁决 JSON（含 items[属性,词,结论,规范化写法]）")
    a = ap.parse_args()

    if a.prompt:
        if not a.result:
            print("缺少 --result", file=sys.stderr)
            return 2
        with open(a.result, encoding="utf-8-sig") as fh:
            o = json.load(fh)
        cands = o.get("vocab_candidates") or []
        if not cands:
            print("无新词候选（见 稳定性结果.json 的 vocab_candidates）")
            return 0
        print(build_vocab_review_prompt(cands, o.get("category") or ""))
        return 0

    if a.apply:
        if not a.rules:
            print("缺少 --rules", file=sys.stderr)
            return 2
        with open(a.apply, encoding="utf-8-sig") as fh:
            dec = json.load(fh)
        items = dec.get("items") if isinstance(dec, dict) else dec
        out = apply_vocab_acceptance(a.rules, items)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
