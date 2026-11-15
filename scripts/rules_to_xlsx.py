# -*- coding: utf-8 -*-
"""把类目 rules.json(归类+标准化规则) 整理成可读 xlsx，便于人工核对。

用法:
    python scripts/rules_to_xlsx.py [rules.json ...]
    (不传参数时自动找 data/<CAT>/*.rules.json)

每个 rules.json 在其同目录输出 `<cat_id>.rules.xlsx`，含 3 个 sheet:
  · 属性规则    属性名 / 规则类型 / 子列 / 受控值 / 别名 / 其它参数
  · 改判清单    row_overrides: ASIN / 属性 / 原值 / 改判 / 备注
  · 全局配置    category/marketplace/id_column/unknown_values/value_order/value_aliases
"""
from __future__ import annotations

import argparse
import glob
import json
import os


def _norm(v):
    return "" if v is None else str(v)


def _join(values):
    if isinstance(values, dict):
        return "；".join("%s→%s" % (k, v) for k, v in values.items())
    if isinstance(values, list):
        return "、".join(_norm(x) for x in values)
    return _norm(values)


def _controlled_values(params: dict) -> str:
    """取「受控值」：enum/multi 用 values, material 用 main_map 的键(规范值)。"""
    if not isinstance(params, dict):
        return ""
    if params.get("values"):
        return _join(params["values"])
    if params.get("main_map"):
        return _join(list(params["main_map"].keys()))
    return ""


def _aliases(params: dict) -> str:
    if not isinstance(params, dict):
        return ""
    return _join(params.get("aliases") or {})


def _rest_params(params: dict) -> str:
    """除 values/aliases/main_map 外的其它参数(分档/单位/判定规则等)压缩成 JSON。"""
    if not isinstance(params, dict):
        return ""
    skip = {"values", "aliases", "main_map"}
    rest = {k: v for k, v in params.items() if k not in skip}
    if not rest:
        return ""
    s = json.dumps(rest, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= 400 else s[:397] + "..."


def convert(path: str) -> str:
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    out = os.path.splitext(path)[0] + ".xlsx"

    import openpyxl
    wb = openpyxl.Workbook()

    # ① 属性规则
    ws = wb.active
    ws.title = "属性规则"
    ws.append(["属性名", "规则类型(rule)", "子列(subcols)", "受控值/规范值", "别名(aliases)", "其它参数"])
    for a in data.get("attributes") or []:
        if not isinstance(a, dict):
            continue
        params = a.get("params") or {}
        ws.append([
            _norm(a.get("name")),
            _norm(a.get("rule")),
            _join(a.get("subcols")),
            _controlled_values(params),
            _aliases(params),
            _rest_params(params),
        ])
    for col, width in zip("ABCDEF", (16, 16, 28, 40, 34, 60)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    # ② 改判清单
    ws2 = wb.create_sheet("改判清单")
    ws2.append(["ASIN", "属性", "原值(when)", "改判(then)", "备注(note)"])
    for r in data.get("row_overrides") or []:
        if not isinstance(r, dict):
            continue
        then = r.get("then")
        then = json.dumps(then, ensure_ascii=False) if isinstance(then, (dict, list)) else _norm(then)
        ws2.append([_norm(r.get("asin")), _norm(r.get("attr")),
                    _norm(r.get("when")), then, _norm(r.get("note"))])
    for col, width in zip("ABCDE", (16, 14, 26, 32, 70)):
        ws2.column_dimensions[col].width = width
    ws2.freeze_panes = "A2"

    # ③ 全局配置
    ws3 = wb.create_sheet("全局配置")
    ws3.append(["键", "值"])
    for key in ("category", "marketplace", "id_column", "meta_end_column", "unknown_values",
                "value_order", "value_aliases"):
        if key in data:
            ws3.append([key, _join(data[key]) if isinstance(data[key], (list, dict)) else _norm(data[key])])
    if data.get("_说明"):
        ws3.append(["_说明", _norm(data["_说明"])])
    ws3.column_dimensions["A"].width = 22
    ws3.column_dimensions["B"].width = 90

    wb.save(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args()
    paths = list(a.paths)
    if not paths:
        paths = sorted(glob.glob(os.path.join("data", "*", "*.rules.json")))
    if not paths:
        print("未找到 rules.json")
        return 1
    for p in paths:
        out = convert(p)
        print("已生成 %s -> %s" % (p, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
