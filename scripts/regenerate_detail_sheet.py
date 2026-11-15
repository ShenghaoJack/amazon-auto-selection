# -*- coding: utf-8 -*-
"""按新逻辑(唯一变体ASIN去重+最优父体)重生成 变体明细.xlsx 的"变体明细"sheet。

不动 results.jsonl, 仅重写"变体明细" sheet(父体汇总/统计 sheet 内容原样保留),
供步骤14-17 用干净数据重跑。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from steps.step10_variants import Step10Variants

CAT = sys.argv[1] if len(sys.argv) > 1 else "salt-pepper-shaker-sets"
BASE = os.path.join("data", CAT)
RESULTS = os.path.join(BASE, f"{CAT}-results.jsonl")
XLSX = os.path.join(BASE, f"{CAT}-父项变体明细.xlsx")


def main() -> int:
    kept = []
    for line in open(RESULTS, encoding="utf-8-sig"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("ok") and rec.get("keep", True):
            kept.append(rec)
    print(f"保留记录: {len(kept)}")

    df = Step10Variants()._build_detail_df(kept)
    print(f"去重后唯一变体行: {len(df)} (唯一变体 {df['变体ASIN'].nunique()})")
    top = df.groupby("父ASIN").size().sort_values(ascending=False).head(5)
    print("父ASIN 家族规模 top5:", top.to_dict())

    xf = pd.ExcelFile(XLSX)
    sheets = {}
    for sn in xf.sheet_names:
        if sn != "变体明细":
            sheets[sn] = pd.read_excel(XLSX, sheet_name=sn)
    print("保留原 sheet:", list(sheets.keys()))

    with pd.ExcelWriter(XLSX, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="变体明细", index=False)
        for sn, odf in sheets.items():
            odf.to_excel(writer, sheet_name=sn, index=False)
    print("已重写完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
