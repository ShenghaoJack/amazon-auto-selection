# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import io

p = "data/Cutting-Boards/17_备份与监控/pipeline-progress.log"
lines = io.open(p, encoding="utf-8-sig", errors="replace").read().splitlines()
start = end = None
for i, ln in enumerate(lines):
    if "=== 步骤 14 流量拆分 开始 ===" in ln and "09:25" in ln:
        start = i
    if start is not None and "步骤 14 完成(DONE)" in ln:
        end = i
        break
seg = lines[start : end + 1] if start is not None else []
# 只保留 Step14 相关行, 去掉"补同步归档"噪音
for ln in seg:
    if "补同步归档" in ln:
        continue
    # 清理: 时间戳+级别+正文
    print(ln)
