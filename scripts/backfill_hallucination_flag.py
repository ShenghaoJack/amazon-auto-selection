# -*- coding: utf-8 -*-
"""回填 `疑似幻觉` 标记（2026-10-26）。

背景: 步骤16 原来**只打日志不落盘**，用户要求在 Web 上看到哪些属性是"疑似幻觉"。
      代码已修（新跑的会自动写），但已有的 spec 需要回填一次。

判据（与步骤16 的 `_zero` 完全一致）:
    高价段（`差异化.高价段` 合并后）的**文案提及率 == 0** → 疑似幻觉。

只标记不删。默认 dry-run，`--apply` 才写盘（写前 .bak 备份）。

用法:
    python scripts/backfill_hallucination_flag.py                    # 全部类目 dry-run
    python scripts/backfill_hallucination_flag.py --apply            # 全部类目写盘
    python scripts/backfill_hallucination_flag.py --cat "Dehumidifiers-US-202609" --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_DATA = Path("data")


def _band_sort_key(label: str):
    """价格段排序键（"$16-40" → (16, 40)）；解析失败排到最后。"""
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)", str(label or ""))
    if not m:
        m2 = re.search(r"(\d+(?:\.\d+)?)", str(label or ""))
        return (float(m2.group(1)), 0.0) if m2 else (9e9, 9e9)
    return (float(m.group(1)), float(m.group(2)))


def high_band_zero_attrs(side: dict) -> list[str]:
    """从侧车算出「高价段文案提及率 == 0」的属性名列表。"""
    meta = side.get("文案提及率") or side.get("差异化") or {}
    high = meta.get("高价段") or []
    if not high:
        # 没存高价段 → 自己按价格段顺序取最后 2 段（与步骤16 默认一致）
        order = side.get("价格段顺序") or list((side.get("覆盖率") or {}).keys())
        order = sorted(order, key=_band_sort_key)
        high = order[-2:] if len(order) >= 2 else order
    cov = side.get("覆盖率") or {}
    hit, tot = {}, {}
    for band in high:
        for name, cell in (cov.get(band) or {}).items():
            if not isinstance(cell, dict):
                continue
            hit[name] = hit.get(name, 0) + int(cell.get("命中") or 0)
            tot[name] = tot.get(name, 0) + int(cell.get("总行") or 0)
    return sorted(n for n in hit if tot.get(n, 0) > 0 and hit[n] == 0)


def process(cat_dir: Path, apply: bool) -> int:
    cat = cat_dir.name
    spec_p = cat_dir / f"{cat}-attribute-spec.json"
    side_p = cat_dir / "16_产品属性标注" / "属性-价格段.json"
    if not spec_p.exists():
        return 0
    if not side_p.exists():
        print(f"  [{cat}] 无侧车（属性-价格段.json），跳过")
        return 0
    spec = json.loads(spec_p.read_text(encoding="utf-8-sig"))
    side = json.loads(side_p.read_text(encoding="utf-8-sig"))
    zeros = high_band_zero_attrs(side)
    if not zeros:
        print(f"  [{cat}] 高价段覆盖率无 0% 属性")
        return 0
    attrs = spec.get("attributes") or []
    by = {str(a.get("name")): a for a in attrs if isinstance(a, dict)}
    changed = 0
    meta = side.get("文案提及率") or side.get("差异化") or {}
    hi_label = "+".join(meta.get("高价段") or [])
    for name in zeros:
        a = by.get(name)
        if a is None:
            continue
        if a.get("疑似幻觉") is True:
            continue
        a["疑似幻觉"] = True
        a["疑似幻觉原因"] = (
            f"高价段({hi_label})文案提及率 0%，可能是模型凭空补充、原文并未出现的维度")
        changed += 1
    print(f"  [{cat}] 高价段 0% 属性 {len(zeros)} 个，spec 里实际标到 {changed} 个")
    for n in zeros:
        print(f"      - {n}")
    if apply and changed:
        shutil.copy2(spec_p, spec_p.with_suffix(spec_p.suffix + ".bak-halluc"))
        spec_p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"      ✅ 已写盘（备份 {spec_p.name}.bak-halluc）")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--cat", default="", help="只处理指定类目")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="data 根目录")
    args = ap.parse_args()
    root = Path(args.data)
    if not root.is_dir():
        print(f"data 目录不存在: {root}")
        return 1
    total = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in ("archive", "_catalog"):
            continue
        if args.cat and d.name != args.cat:
            continue
        total += process(d, args.apply)
    print()
    print(f"合计标记 {total} 个属性")
    if not args.apply:
        print("（dry-run，未写盘；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
