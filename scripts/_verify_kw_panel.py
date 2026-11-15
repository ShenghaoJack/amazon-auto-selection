# -*- coding: utf-8 -*-
"""复核 webui 关键词面板：id 去重、两列结构对称、内联 JS 可解析。"""
from __future__ import annotations

import collections
import io
import os
import re

HTML = os.path.join("webui", "index.html")
s = io.open(HTML, encoding="utf-8").read()

ids = re.findall(r'id="([^"]+)"', s)
dup = {k: v for k, v in collections.Counter(ids).items() if v > 1}
print("重复 id =", dup or "无 ✅")
for k in ("kwCandChips", "kwCandCount", "kwTailPool", "kwCoreChips", "kwTailChips"):
    print("   %-14s 出现 %d 次" % (k, ids.count(k)))

marker = '<div style="display:flex;gap:18px;flex-wrap:wrap">'
row = s[s.index(marker):]
row = row[:row.index("说明: 人工清单保存后")]
print("\n两列结构检查（同一 flex 行内）：")
print("   候选词池在行内 =", 'id="kwCandChips"' in row)
print("   长尾词池在行内 =", 'id="kwTailPool"' in row)
print("   候选词池在长尾词池之前 =", row.index('id="kwCandChips"') < row.index('id="kwTailPool"'))
# 每个池必须在自己列的 </div> 之前
seg = row[row.index("<div", row.index(marker) + len(marker)):]
print("   左列内含候选词池 =",
      'id="kwCandChips"' in row[:row.index('id="kwTailPool"')])
print("   右列内含长尾词池 =", 'id="kwTailPool"' in row)

box = ('style="max-height:200px;overflow:auto;border:1px solid var(--line);'
       'border-radius:10px;padding:6px"')
print("\n容器样式一致性：")
print('   kwTailPool 同款 =', ('id="kwTailPool" ' + box) in s)
print('   kwCandChips 同款 =', ('id="kwCandChips" ' + box) in s)

blocks = re.findall(r"<script[^>]*>(.*?)</script>", s, re.S)
io.open(os.path.join("scripts", "_webui_inline.js"), "w", encoding="utf-8").write(
    "\n;\n".join(blocks))
print("\n已提取内联 JS %d 块 / %d 字符 -> scripts/_webui_inline.js"
      % (len(blocks), sum(len(b) for b in blocks)))
