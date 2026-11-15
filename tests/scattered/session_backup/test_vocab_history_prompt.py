# -*- coding: utf-8 -*-
"""自测: step17._gen_vocab_prompt 带历史轮次(批量) + build_vocab_cluster_prompt(单属性)。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

from steps.step17_attr_annotation import Step17AttrAnnotation
from tools.attr_stability.onboard import build_vocab_cluster_prompt

hist = {
    "材质": [
        {"轮次": 1, "口径建议": "把柚木/相思木合并成木制", "规范值": ["木制", "金属"],
         "归并": [{"原值": "柚木", "归入": "木制"}]},
        {"轮次": 2, "口径建议": "竹类单独叫竹制", "规范值": ["木制", "竹制", "金属"],
         "归并": [{"原值": "竹", "归入": "竹制"}]},
    ]
}
print("=== step17._gen_vocab_prompt（批量）带历史 ===")
print(Step17AttrAnnotation._gen_vocab_prompt(
    {"材质": [("柚木", 5), ("不锈钢", 3)], "雕刻文字": [("支持", 2)]},
    {"材质": "把柚木合并成木制"},
    history=hist,
))
print("\n=== build_vocab_cluster_prompt（单属性，面板用）带历史 ===")
print(build_vocab_cluster_prompt("材质", [("柚木", 5), ("不锈钢", 3)], "不锈钢/铝合金都归金属", history=hist["材质"]))
