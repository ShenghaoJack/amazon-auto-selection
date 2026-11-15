# -*- coding: utf-8 -*-
"""只读勘察: 目标类目的 14/16/17/18 数据形状与规模(决定试点范围)。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CATS = ["Cutting Boards-US-202608", "Dog Dryer-US-202608", "Salt and Pepper Mill Sets-US-202608"]


def jload(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as e:
        return {"__error__": str(e)}


def main() -> int:
    for cat in CATS:
        d = ROOT / "data" / cat
        print("=" * 78)
        print(f"[{cat}]")
        # 16 spec
        spec = jload(d / f"{cat}-attribute-spec.json") or {}
        attrs = spec.get("attributes") or []
        print(f"  16 spec: status={spec.get('status')} 属性 {len(attrs)} 个")
        print(f"     {[a.get('name') for a in attrs]}")
        # 17 稳定性
        res = jload(d / "17_属性标注" / "稳定性结果.json") or {}
        cells = res.get("cells") or {}
        deliv = res.get("deliverable") or []
        vc = res.get("vocab_candidates") or []
        vcells = sum(1 for v in cells.values() if isinstance(v, dict) and v.get("kind") == "value")
        nvals = sum(len(v.get("values") or []) for v in cells.values() if isinstance(v, dict))
        print(f"  17 稳定性: cells={len(cells)}(值格 {vcells}) deliverable={len(deliv)} 行 "
              f"提名总数={nvals} 新词候选={len(vc)}")
        print(f"     keys: {sorted(res.keys())[:14]}")
        used = res.get("used_runs"); passed = res.get("passed")
        print(f"     used_runs={used} passed={len(passed) if isinstance(passed, list) else passed}")
        # 17 input
        inp = jload(d / "17_属性标注" / "input.json") or {}
        rows = inp.get("rows") or []
        print(f"  17 input: {len(rows)} 行; 列={list(rows[0].keys())[:12] if rows else []}")
        # rules
        rp = next((p for p in d.glob("*.rules.json")), None)
        if rp:
            rules = jload(rp) or {}
            rattrs = rules.get("attributes") or []
            print(f"  rules({rp.name}): 属性 {len(rattrs)} 个; top_keys={len(rules.get('top_keys') or [])}; "
                  f"relations={'有' if rules.get('relations') else '无'}")
            typed = []
            withvals = 0
            for a in rattrs:
                p = a.get("params") if isinstance(a.get("params"), dict) else {}
                vals = p.get("values") or a.get("values") or []
                if vals:
                    withvals += 1
                typed.append(f"{a.get('name')}:{a.get('rule') or p.get('rule') or a.get('type') or '?'}"
                             f"{'(' + str(len(vals)) + '值)' if vals else ''}")
            print(f"     有词表 {withvals}/{len(rattrs)}")
            print(f"     {typed[:14]}{'…' if len(typed) > 14 else ''}")
        # 18 交付
        x18 = d / "18_标准化" / f"{cat}-属性标注-标准化交付.xlsx"
        if x18.is_file():
            try:
                import openpyxl
                wb = openpyxl.load_workbook(x18, read_only=True)
                ws = wb[wb.sheetnames[0]]
                hdr = next(ws.iter_rows(values_only=True))
                print(f"  18 交付: sheets={wb.sheetnames} 列={len(hdr)} 行≈{ws.max_row - 1}")
                print(f"     列名尾部: {list(hdr)[-10:]}")
                wb.close()
            except Exception as e:
                print(f"  18 交付: 读取失败 {e}")
        else:
            print("  18 交付: 无")
        # 视觉补充
        vp = jload(d / "17_属性标注" / "视觉补充.json")
        if isinstance(vp, dict):
            print(f"  17 视觉补充: filled={len(vp.get('filled') or {})} sources={len(vp.get('sources') or {})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
