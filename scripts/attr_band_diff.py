# -*- coding: utf-8 -*-
"""价格段差异化属性工具（只读现有数据，不改主流程）。

数据源（步骤16 已产出）:
  data/<CAT>/16_产品属性标注/属性-价格段.json   → {属性: [出现过的价格段]}

输出:
  · 仅中/高段出现的属性（"中高价段特有"）
  · 各属性覆盖的段数 / 中高段占比
  · 若给 --rate-file（步骤17 标注结果 xlsx），再算每段出现率 → 差异化排序
用法:
  python scripts/attr_band_diff.py --cat "Dog Dryer-US-202608"
  python scripts/attr_band_diff.py --cat "..." --rate-file "data/<CAT>/17_属性标注/属性标注结果.xlsx"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse_band(label: str) -> float:
    """从 '$16-40' / '$163-286' 里取下限；解析不出返回 +inf（排到最后）。"""
    m = re.search(r"(\d+(?:\.\d+)?)", str(label))
    return float(m.group(1)) if m else float("inf")


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8-sig"))


def split_bands(bands: list[str], split: str = "") -> dict[str, list[str]]:
    """把段按价格切成 低/中/高。split='2,2,3' 表示前 2 低、中 2、后 3 高；留空则按三等分。"""
    bands = sorted(bands, key=parse_band)
    n = len(bands)
    if split:
        a = [int(x) for x in split.split(",") if x.strip()]
        if len(a) >= 3:
            lo, mi = a[0], a[1]
            return {"低": bands[:lo], "中": bands[lo:lo + mi], "高": bands[lo + mi:]}
    k = max(1, n // 3)
    return {"低": bands[:k], "中": bands[k:2 * k], "高": bands[2 * k:]}


def rate_from_annotation(path: Path, bands_by_attr: dict) -> dict:
    """可选: 从步骤17 标注结果按段算出现率（该属性有值 / 该段商品数）。"""
    import pandas as pd
    df = pd.read_excel(path)
    price_col = next((c for c in df.columns if str(c) in ("价格($)", "价格", "price", "售价")), None)
    if price_col is None:
        return {}
    prices = pd.to_numeric(df[price_col].astype(str).str.replace(r"[^\d.]", "", regex=True), errors="coerce")
    df = df.assign(__price=prices).dropna(subset=["__price"])
    # 段定义复用它自己的标签（$16-40 → 16..40）
    seg = []
    for b in sorted({b for bs in bands_by_attr.values() for b in bs}, key=parse_band):
        m = re.match(r"[^\d]*(\d+)\D+(\d+)", str(b))
        if m:
            seg.append((b, float(m.group(1)), float(m.group(2))))
    out = {}
    for attr in bands_by_attr:
        if attr not in df.columns:
            continue
        row = {}
        for b, lo, hi in seg:
            sub = df[(df["__price"] >= lo) & (df["__price"] < hi)]
            if len(sub) == 0:
                continue
            vals = sub[attr].astype(str).str.strip()
            row[b] = round(float((vals.ne("") & vals.ne("nan")).mean()), 3)
        out[attr] = row
    return out


def keyword_rates(spec_file: Path, split_file: Path, groups: dict[str, list[str]]) -> dict:
    """按段算"关键词出现率"(该段里提到该属性关键词的商品占比)。

    不需要步骤17 标注结果 —— 直接从拆分表原文 + spec 的 hint 关键词算，
    解决"属性-价格段.json 只记有/无、不记多寡"的问题。
    """
    import pandas as pd
    from steps.step16_attribute_annotation import _hint_keywords
    spec = json.loads(spec_file.read_text(encoding="utf-8-sig"))
    df = pd.read_excel(split_file, sheet_name="AI已补充")
    price_col = next((c for c in ("价格($)", "价格(€)", "价格(£)", "价格", "售价", "price")
                      if c in df.columns), None)
    if price_col is None:
        return {}
    prices = pd.to_numeric(df[price_col].astype(str).str.replace(r"[^\d.]", "", regex=True),
                           errors="coerce")
    text_cols = [c for c in ("标题", "五点描述", "详细参数") if c in df.columns]
    if not text_cols:
        return {}
    blob = df[text_cols].astype(str).agg(" ".join, axis=1).str.lower()

    def band_range(label: str):
        m = re.search(r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)", str(label))
        return (float(m.group(1)), float(m.group(2))) if m else (0.0, float("inf"))

    out = {}
    for attr in (spec.get("attributes") or []):
        name = str(attr.get("name") or "").strip()
        kws = _hint_keywords(attr.get("hint"))
        if not name or not kws:
            continue
        row = {}
        for label in groups["低"] + groups["中"] + groups["高"]:
            lo, hi = band_range(label)
            mask = (prices >= lo) & (prices < hi)
            n = int(mask.sum())
            if not n:
                continue
            hit = blob[mask].apply(lambda t: any(k in t for k in kws)).sum()
            row[label] = round(float(hit) / n, 3)
        out[name] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cat", required=True)
    ap.add_argument("--band-file", default="", help="默认 <CAT>/16_产品属性标注/属性-价格段.json")
    ap.add_argument("--rate-file", default="", help="可选: 步骤17 标注结果 xlsx，用于算每段出现率")
    ap.add_argument("--split-file", default="", help="默认 <CAT>/14_流量拆分/流量拆分数据-需人工补充.xlsx")
    ap.add_argument("--spec-file", default="", help="默认 <CAT>-attribute-spec.json")
    ap.add_argument("--split", default="", help="低,中,高 段数，如 '2,2,3'；留空按三等分")
    ap.add_argument("--out", default="", help="输出 JSON 路径")
    args = ap.parse_args()

    cat_dir = ROOT / "data" / args.cat
    band_file = Path(args.band_file) if args.band_file else cat_dir / "16_产品属性标注" / "属性-价格段.json"
    if not band_file.exists():
        raise SystemExit("找不到 %s（先跑步骤16）" % band_file)
    data = read_json(band_file)
    bands_by_attr: dict[str, list[str]] = data.get("属性价格段") or {}
    groups = split_bands(list({b for bs in bands_by_attr.values() for b in bs}), args.split)
    lo_set, mid_set, hi_set = set(groups["低"]), set(groups["中"]), set(groups["高"])

    rows = []
    for attr, bs in bands_by_attr.items():
        s = set(bs)
        rows.append({
            "属性": attr,
            "出现段": bs,
            "段数": len(s),
            "仅低段": bool(s) and s <= lo_set,
            "仅中高段": bool(s) and s <= (mid_set | hi_set),
            "中高段占比": round(len(s & (mid_set | hi_set)) / max(1, len(s)), 2),
        })
    # 差异化: 仅中高段优先，其次中高占比、段数
    rows.sort(key=lambda r: (not r["仅中高段"], -r["中高段占比"], r["段数"]))

    out = {"cat": args.cat, "分组": groups, "属性数": len(rows), "属性": rows}
    # ★ 差异化核心: 按"关键词在每段的出现率"算 中高-低 差值（presence 太粗，必须看占比）
    split_file = (Path(args.split_file) if args.split_file
                  else cat_dir / "14_流量拆分" / "流量拆分数据-需人工补充.xlsx")
    spec_file = (Path(args.spec_file) if args.spec_file
                 else cat_dir / f"{args.cat}-attribute-spec.json")
    if split_file.exists() and spec_file.exists():
        rates = keyword_rates(spec_file, split_file, groups)
        for r in rows:
            rr = rates.get(r["属性"]) or {}
            lo = [v for k, v in rr.items() if k in set(groups["低"])]
            hi = [v for k, v in rr.items() if k in set(groups["中"]) | set(groups["高"])]
            r["低段出现率"] = round(sum(lo) / len(lo), 3) if lo else None
            r["中高段出现率"] = round(sum(hi) / len(hi), 3) if hi else None
            r["中高低差"] = (round(r["中高段出现率"] - r["低段出现率"], 3)
                            if r["低段出现率"] is not None and r["中高段出现率"] is not None else None)
        rows.sort(key=lambda r: (r.get("中高低差") is None, -(r.get("中高低差") or 0)))
        out["每段出现率"] = rates
    if args.rate_file:
        rf = Path(args.rate_file)
        if rf.exists():
            out["标注每段出现率"] = rate_from_annotation(rf, bands_by_attr)

    out_path = Path(args.out) if args.out else cat_dir / "16_产品属性标注" / "属性-价格段-差异化.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("价格段分组: 低=%s | 中=%s | 高=%s" % (groups["低"], groups["中"], groups["高"]))
    print("共 %d 个属性\n" % len(rows))
    print("%-18s %-6s %-8s %-8s %-9s %s" % ("属性", "段数", "中高低差", "低段率", "中高段率", "出现段"))
    for r in rows:
        print("%-18s %-6s %-8s %-8s %-9s %s" % (
            r["属性"], r["段数"],
            r.get("中高低差") if r.get("中高低差") is not None else "—",
            r.get("低段出现率") if r.get("低段出现率") is not None else "—",
            r.get("中高段出现率") if r.get("中高段出现率") is not None else "—",
            "、".join(r["出现段"])))
    print("\n输出: %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
