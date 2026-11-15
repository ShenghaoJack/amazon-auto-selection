# -*- coding: utf-8 -*-
"""一次性: 把步骤10 浏览器补抓的两个新键写进全部类目配置(改前按仓库约定版本化备份)。

新增键(2026-09-16, 用户要求"补抓后还有失败的可以再进行一轮补抓"):
  BROWSER_FALLBACK_ROUNDS  (默认 2)  本机浏览器补抓轮数
  BROWSER_ROUND_PAUSE      (默认 30) 补抓轮间冷却秒数

只动 **当前** 类目配置(data/<CAT>/config.yaml):
  * 不动 data/archive/** 的历史快照与 *.bak 备份 —— 它们是"当时实际用的配置"的历史记录,
    改写会让历史失真(且新键取值与代码默认一致, 老备份恢复后行为不变);
  * 每个被改的配置先走 core.config_backup.make_backup()(与 webui 保存同一条路径, 版本号递增)。
"""
import io
import os
import re
import sys

from core.config_backup import make_backup

BLOCK = """# 浏览器补抓轮数(2026-09-16): 直接请求轮用尽后残余**全部**转本机浏览器补抓; 补抓后仍有失败
#   → 自动**再补一轮**(只补未解决的 ASIN), 全解决或轮数用尽即停, 仍失败才舍弃
#   (每 ASIN 每轮最多试 1 次 URL 形态; 风控严的站点可调到 3)
BROWSER_FALLBACK_ROUNDS: 2
# 浏览器补抓轮间冷却秒数(仅第 2 轮及以后生效, 默认 30)
BROWSER_ROUND_PAUSE: 30
"""
REASON = "新增步骤10 浏览器补抓轮数/轮间冷却两个键(BROWSER_FALLBACK_ROUNDS / BROWSER_ROUND_PAUSE, 2026-09-16)"

data_root = sys.argv[1] if len(sys.argv) > 1 else "data"
targets = []
for name in sorted(os.listdir(data_root)):
    if name == "archive":
        continue
    p = os.path.join(data_root, name, "config.yaml")
    if os.path.isfile(p):
        targets.append((name, p))

changed = skipped = 0
for cat, path in targets:
    with io.open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    if "BROWSER_FALLBACK_ROUNDS" in text:
        print(f"[跳过] {cat}: 已有该键")
        skipped += 1
        continue
    # 顺手修正一处过时注释(原写"每 ASIN 试 3 次", 实际 2026-09-09 定稿=1 次/轮)
    text = text.replace("补抓(每 ASIN 试 3 次)", "补抓(每 ASIN 每轮试 1 次)")
    lines = text.split("\n")
    out, inserted = [], False
    for ln in lines:
        out.append(ln)
        if not inserted and re.match(r"^FETCH_STALL_ROUNDS\s*:", ln):
            out.extend(BLOCK.rstrip("\n").split("\n"))
            inserted = True
    new_text = "\n".join(out)
    if not inserted:                       # 该配置没有那一行 → 追加到文件末尾
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += BLOCK
    backup = make_backup(path, reason=REASON, changed_to=new_text,
                         log=lambda m: print("   " + m))
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    print(f"[已更新] {cat}: 备份={backup.name if backup else '无(内容无变化)'}")
    changed += 1

print(f"\n合计: 更新 {changed} 个类目配置, 跳过 {skipped} 个(已有键)")
