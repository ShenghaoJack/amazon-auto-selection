# -*- coding: utf-8 -*-
"""验证：模型返回另一种 schema 时能被归一成契约结构。"""
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.attr_runner import _normalize_got_row  # noqa: E402

# ① 用户报错里的真实形状
weird = {
    "row_id": "2",
    "attributes": {
        "是否带漏斗": {"value": "否", "source": "五点描述",
                       "basis": "Gravity Refillable Battery Operated Button"},
    },
}
print("① 另一种 schema →")
print("   ", _normalize_got_row(weird))

# ② 契约形状应原样通过
normal = {"xl_row": 5, "是否带漏斗": "是", "是否带漏斗_来源": "标题"}
print("② 契约形状 →")
print("   ", _normalize_got_row(normal))

# ③ attributes 里也有扁平值（混合）
mixed = {"row_id": "7", "attributes": {"容量": "50ml", "主体材质": {"value": "不锈钢"}}}
print("③ 混合形状 →")
print("   ", _normalize_got_row(mixed))
