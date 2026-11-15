# -*- coding: utf-8 -*-
"""只读勘察: 哪些类目具备 步骤14 / 16 / 17 的数据(决定测试范围与花费)。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

need = {
    "14_拆分xlsx": "14_流量拆分/流量拆分数据-需人工补充.xlsx",
    "16_spec": "{cat}-attribute-spec.json",
    "17_稳定性": "17_属性标注/稳定性结果.json",
    "17_input": "17_属性标注/input.json",
    "17_视觉": "17_属性标注/视觉补充.json",
    "17_单遍xlsx": "{cat}-属性标注-单遍原始.xlsx",
    "17_唯一值": "17_属性标注/唯一值统计.json",
    "18_交付xlsx": "18_标准化/{cat}-属性标注-标准化交付.xlsx",
    "rules": "{stem}.rules.json",
}
hdr = f"{'类目':<40}" + "".join(f"{k:>11}" for k in need)
print(hdr)
print("-" * len(hdr))
rows = []
for d in sorted(p for p in DATA.iterdir() if p.is_dir()):
    cat = d.name
    if cat.startswith("_") or cat.startswith("."):
        continue
    stem = cat.lower().replace(" ", "_").replace("-", "_")
    flags = {}
    for k, tpl in need.items():
        p = d / tpl.format(cat=cat, stem=stem)
        flags[k] = p.is_file()
    # rules 也可能在 categories/ 子目录
    if not flags["rules"]:
        sub = d / "categories"
        if sub.is_dir():
            rr = [f for f in os.listdir(sub) if f.endswith(".rules.json")]
            flags["rules"] = len(rr) > 0
    if not any(flags.values()):
        continue
    rows.append((cat, flags))
    print(f"{cat:<40}" + "".join(("     ✅    " if flags[k] else "     ·     ") for k in need))
print("-" * len(hdr))
print(f"共 {len(rows)} 个类目有上述任一数据")
full = [c for c, f in rows if f["16_spec"] and f["17_稳定性"] and f["17_input"]]
print(f"\n★ 同时具备 16_spec + 17_稳定性 + 17_input 的类目({len(full)}): ")
for c in full:
    print("   " + c)
