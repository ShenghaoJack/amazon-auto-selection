# -*- coding: utf-8 -*-
"""把步骤17 标注结果里某个"模糊值"改成具体数值（或 未明确）。

场景（2026-10-22 用户实测）: `喷嘴数量` 出现 93 个「多个」，用户要具体数值。
做法（**不调 LLM**，纯规则抽取，可复现）:
  ① 先看该行的「`<属性>_依据`」——很多情况下 LLM 已经把证据写在里面（如 `标题：5 Nozzles`），
     只是值没写成数字；
  ② 依据里没有就扫商品原文（标题/五点/详细参数/SKU），找「数字 + 关键词」；
  ③ 都找不到 → 写 `未明确`。

只改三处，保证步骤18 读到的是一致的:
  · 稳定性结果.json 的 `deliverable[].attributes[属性]`
  · 稳定性结果.json 的 `cells["{xl_row}|attr.属性"].mode`
  · 17_属性标注/唯一值统计.json（按新值重算）

用法:
  python scripts/fix_vague_attr_value.py --cat "Dog Dryer-US-202608" \
      --attr 喷嘴数量 --vague 多个 --keywords "nozzle,nozzles,风嘴,喷嘴,attachment,attachments,tool,tools"
  加 --dry-run 只看不改。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}


def _num_pattern(keywords: list[str]) -> re.Pattern:
    """「数字 + 关键词」的模式：5 Nozzles / 4个喷嘴 / 3 x attachments …"""
    kw = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(r"(\d+)\s*(?:x\s*)?(?:个)?\s*(?:%s)" % kw, re.I)


def _find_count(text: str, pat: re.Pattern, unit: str) -> str | None:
    """从一段文字里抽「N<unit>」；抽不到返回 None。支持 two/three 这类英文数词。"""
    if not text:
        return None
    m = pat.search(text)
    if m:
        try:
            return "%d%s" % (int(m.group(1)), unit)
        except ValueError:
            return None
    # 英文数词 + 关键词（数词后面 40 字内出现关键词）
    low = str(text).lower()
    for w, n in _WORD_NUM.items():
        for mm in re.finditer(r"\b" + w + r"\b", low):
            tail = low[mm.end():mm.end() + 40]
            if re.search(r"nozzle|attachment|tool|风嘴|喷嘴", tail):
                return "%d%s" % (n, unit)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True, help="类目目录名，如 Dog Dryer-US-202608")
    ap.add_argument("--attr", required=True, help="属性名，如 喷嘴数量")
    ap.add_argument("--vague", required=True, help="要替换掉的模糊值，如 多个")
    ap.add_argument("--keywords", default="nozzle,nozzles,风嘴,喷嘴,attachment,attachments,tool,tools")
    ap.add_argument("--unit", default="个", help="数字后缀，默认 个")
    ap.add_argument("--fallback", default="未明确", help="抽不到数字时写什么")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = ROOT / "data" / args.cat / "17_属性标注"
    stab_p = base / "稳定性结果.json"
    stats_p = base / "唯一值统计.json"
    input_p = base / "input.json"
    if not stab_p.is_file():
        print("找不到 %s" % stab_p)
        return 1
    d = json.loads(stab_p.read_text(encoding="utf-8-sig"))
    dl = d.get("deliverable") or []
    cells = d.get("cells") or {}
    rows = json.loads(input_p.read_text(encoding="utf-8-sig")) if input_p.is_file() else []
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    by_xl = {int(r.get("xl_row") or 0): r for r in rows if isinstance(r, dict)}

    kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    pat = _num_pattern(kws)
    text_cols = ("标题", "五点描述", "详细参数", "SKU")

    changed, from_basis, from_text, to_fallback = 0, 0, 0, 0
    for r in dl:
        a = r.get("attributes") or {}
        if str(a.get(args.attr) or "").strip() != args.vague:
            continue
        xl = int(r.get("xl_row") or 0)
        basis = str(a.get(args.attr + "_依据") or "")
        val = _find_count(basis, pat, args.unit)
        src = "依据"
        if not val:
            rec = by_xl.get(xl) or {}
            blob = " ".join(str(rec.get(c) or "") for c in text_cols)
            val = _find_count(blob, pat, args.unit)
            src = "原文"
        if val:
            from_basis += 1 if src == "依据" else 0
            from_text += 1 if src == "原文" else 0
        else:
            val = args.fallback
            to_fallback += 1
            src = "兜底"
        changed += 1
        print("  xl_row=%-4s %s → %-8s (%s)" % (xl, args.vague, val, src))
        if args.dry_run:
            continue
        a[args.attr] = val
        # 依据保留（它就是证据），来源保持不变
        ck = "%d|attr.%s" % (xl, args.attr)
        c = cells.get(ck)
        if isinstance(c, dict):
            c["mode"] = val
            c["values"] = [{"value": val, "by": (c.get("values") or [{}])[0].get("by") or ["a1"]}]
            c["no_majority"] = False

    print()
    print("命中 %d 行：依据里抽到 %d / 原文里抽到 %d / 兜底为「%s」%d"
          % (changed, from_basis, from_text, args.fallback, to_fallback))

    if args.dry_run:
        print("（dry-run，未写盘）")
        return 0
    if not changed:
        print("没有需要改的行，未写盘")
        return 0

    # 备份 + 写盘
    for p in (stab_p, stats_p):
        if p.is_file():
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    stab_p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    # 重算唯一值统计（只重算该属性，其余不动）
    if stats_p.is_file():
        st = json.loads(stats_p.read_text(encoding="utf-8-sig"))
        pool = st.get("属性唯一值") or {}
        cnt = Counter(str((r.get("attributes") or {}).get(args.attr) or "").strip() for r in dl)
        pool[args.attr] = [{"值": v, "次数": n} for v, n in cnt.most_common() if v]
        st["属性唯一值"] = pool
        stats_p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已重算唯一值统计:", dict(cnt.most_common()))

    print()
    print("已写回:")
    print("  ", stab_p, "（备份 .bak）")
    print("  ", stats_p, "（备份 .bak）")
    print("提示: 单遍原始 xlsx 未改（它只是单遍参考）；步骤18 读的是 稳定性结果.json。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
