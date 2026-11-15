# -*- coding: utf-8 -*-
import sys, io
sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
from tools.attr_stability.attr_runner import _normalize_sources
out = io.StringIO()
tests = ["标题", "正文", "五点", "标题；五点描述", "参数+图片", "未知来源", "图片识别", "sku", ""]
for t in tests:
    out.write("%r -> %r\n" % (t, _normalize_sources(t)))
open(r"D:\deepseek工作区\auto-workflow-python\_weekly\source_norm_result.txt", "w", encoding="utf-8").write(out.getvalue())
print("done")
