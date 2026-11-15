# -*- coding: utf-8 -*-
"""验证 步骤18 取值筛选默认是并集。"""
import json
import sys

sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
sys.stdout.reconfigure(encoding="utf-8")
from tools.attr_stability.step18_workspace import Workspace  # noqa: E402

VP = (r"data\Salt and Pepper Mill Sets-US-202608\18_标准化\版本数据\versions"
      r"\V827afbe584c14b6485f152100fddbeba.json")
state = json.load(open(VP, encoding="utf-8"))
ws = Workspace.__new__(Workspace)          # 只要 select，不需要真实 store

attrs = ["是否带漏斗", "是否可调节粗细", "是否可观察余量"]
sel = {"attributes": attrs,
       "values": {a: ["", "未明确"] for a in attrs}}

_, rows = ws.select(state, sel)
print("默认（并集）选中行数:", len(rows))

_, rows_all = ws.select(state, {**sel, "match": "all"})
print("显式 match=all（交集）选中行数:", len(rows_all))

_, rows_none = ws.select(state, {"attributes": attrs})
print("不选取值（全部）选中行数:", len(rows_none))
