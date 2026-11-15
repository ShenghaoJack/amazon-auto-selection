# -*- coding: utf-8 -*-
"""把交付表「厚度」与「尺寸规格」两列的单位从 **cm 换成 inch**(2026-09-24)。

口径
====
· 厚度: `1.524cm` → `0.6inch`; 多件 `大号:1.524cm,小号:0.7874cm` → `大号:0.6inch,小号:0.31inch`; `缺` 不变。
· 尺寸规格: `38.1x25.4x1.524 cm` → `15x10x0.6 inch`;
  多件 `大号:45.72x30.48x1.524 cm,…` → `大号:18x12x0.6 inch,…`; `缺` 不变。
· 换算 1 inch = 2.54 cm; 数值按 `%g` 输出(最多 3 位小数、去尾零) ——
  实测绝大多数值本来就是从英寸来的, 换算回去是 `15`/`10`/`0.6` 这种整齐数。

只动这两列的**数值与单位**, 标签(大号/中号/XL…)与「来源」列原样保留。
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STD = "18_标准化"
CM_PER_INCH = 2.54
NUM = re.compile(r'(\d+(?:\.\d+)?)')
UNIT_RE = re.compile(r'\s*(?:cm|\u5398\u7c73)\b', re.I)
KEEP = ("", "缺", "未明确", "None")


def to_inch(x: float) -> str:
    return "%g" % round(x / CM_PER_INCH, 3)


def conv_numbers(body: str, space: bool = True) -> str:
    """把一小段里的数字按 cm→inch 换算, 并把单位写成 inch。

    `space=True` 写 `15x10x0.6 inch`(尺寸规格原有写法), `False` 写 `0.6inch`(厚度原有写法)。
    """
    out = NUM.sub(lambda m: to_inch(float(m.group(1))), body)
    if UNIT_RE.search(body):
        out = UNIT_RE.sub(" inch" if space else "inch", out)
    return out


def conv_cell(s: str, space: bool = True) -> str:
    """整格转换: 保留 `标签:` 前缀与 `缺`。"""
    raw = str(s or "").strip()
    if raw in KEEP:
        return raw
    segs = []
    for seg in re.split(r'([,，])', raw):          # 保留分隔符
        if seg in (",", "，") or not seg.strip():
            segs.append(seg)
            continue
        lab, sep, body = "", "", seg
        for colon in (":", "："):
            if colon in seg:
                lab, _, body = seg.partition(colon)
                sep = colon
                break
        if sep:
            segs.append("%s%s%s" % (lab, sep, conv_numbers(body, space)))
        else:
            segs.append(conv_numbers(seg, space))
    return "".join(segs).strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="厚度/尺寸规格 单位 cm → inch")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    import openpyxl
    path = os.path.join(cat_dir, STD, "%s-属性标注-标准化交付.xlsx" % args.cat)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    main_sheet = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    hdr = sheets[main_sheet][0]
    cols = [c for c in ("厚度", "尺寸规格") if c in hdr]
    idx = {c: hdr.index(c) for c in cols}

    stat = collections.Counter()
    sample = []
    for k in range(1, len(sheets[main_sheet])):
        for c, i in idx.items():
            old = str(sheets[main_sheet][k][i] or "").strip()
            new = conv_cell(old, space=(c == "尺寸规格"))
            stat[("%s 改" % c) if old != new else ("%s 不变" % c)] += 1
            if old != new and len(sample) < 10:
                sample.append("%-12s %s: %s → %s"
                              % (sheets[main_sheet][k][hdr.index("变体ASIN")], c, old[:52], new[:52]))
            if args.apply and old != new:
                sheets[main_sheet][k][i] = new
    print("列:", cols)
    for k, v in stat.most_common():
        print("   %-14s %d" % (k, v))
    for s in sample:
        print("   " + s)

    if not args.apply:
        print("(加 --apply 写回)")
        return 0
    import shutil
    bak = path + ".bak-before-inch.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    out = openpyxl.Workbook()
    first = out.active
    first.title = main_sheet
    for j, name in enumerate([main_sheet] + [s for s in sheets if s != main_sheet]):
        ws = first if j == 0 else out.create_sheet(name)
        for row in sheets[name]:
            ws.append(row)
    out.save(path)
    print("[写回] %s (备份 %s)" % (path, os.path.basename(bak)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
