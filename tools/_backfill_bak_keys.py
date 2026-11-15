# -*- coding: utf-8 -*-
"""一次性: 给**类目内** config-backups/ 的历史备份补写步骤10 的两个新增键。

范围(用户 2026-09-16 定): 只动 `data/<CAT>/config-backups/*.bak`;
  * **不动**集中兜底目录 `.webui/config-backups/`(那里 10 份是无头部的旧命名历史留档);
  * 不动 `data/archive/**` 的归档快照;
  * 已有该键的(本次新生成的 12 份)跳过。

诚实性: 每个被补写的备份在头部加一行 `# 备注:` 说明"这两个键是 2026-09-16 补写的、
其余内容为该版本原样、取值等于代码默认" —— 避免日后把它误当成"当时实况快照"。
(`restore_backup` 恢复时会剥掉文件开头所有 `# ` 行, 所以这行不会进配置; 正文里的注释会保留。)
"""
import io
import os
import re
import sys
from pathlib import Path

import core.config_backup as cb

BLOCK = """# 浏览器补抓轮数(2026-09-16): 直接请求轮用尽后残余**全部**转本机浏览器补抓; 补抓后仍有失败
#   → 自动**再补一轮**(只补未解决的 ASIN), 全解决或轮数用尽即停, 仍失败才舍弃
#   (每 ASIN 每轮最多试 1 次 URL 形态; 风控严的站点可调到 3)
BROWSER_FALLBACK_ROUNDS: 2
# 浏览器补抓轮间冷却秒数(仅第 2 轮及以后生效, 默认 30)
BROWSER_ROUND_PAUSE: 30
"""
NOTE = ("# 备注: 2026-09-16 补写 BROWSER_FALLBACK_ROUNDS / BROWSER_ROUND_PAUSE 两个新增键\n"
        "#       (其余内容为该版本原样; 两键取值等于代码默认, 恢复后行为不变)\n")

updated = skipped_has = skipped_nohdr = 0
for d in cb.backup_dirs():
    if d == cb.BACKUP_DIR:          # 只补类目内
        continue
    for p in sorted(d.glob("*.bak")):
        text = io.open(p, encoding="utf-8-sig").read()
        # ⚠️ 只能看**正文**是否已有该键: 头部"备份原因"里可能就写着键名
        #    (如本批 12 份的 reason = "新增步骤10 浏览器补抓轮数/轮间冷却两个键(BROWSER_FALLBACK_ROUNDS / …)"),
        #    第一版按全文匹配 → 这 12 份被误判成"已含键"而漏补。
        lines0 = text.split("\n")
        j = 0
        while j < len(lines0) and lines0[j].startswith("# "):
            j += 1
        body_text = "\n".join(lines0[j:])
        body_text = "\n".join(ln for ln in body_text.split("\n")
                              if not ln.startswith("# "))
        if re.search(r"^BROWSER_FALLBACK_ROUNDS\s*:", body_text, re.M):
            skipped_has += 1
            continue
        if not text.lstrip().startswith("#"):
            skipped_nohdr += 1
            print(f"[跳过-无头部] {p}")
            continue
        lines = text.split("\n")
        # 1) 头部备注: 插在开头连续的 "# " 行之后
        i = 0
        while i < len(lines) and lines[i].startswith("# "):
            i += 1
        lines.insert(i, NOTE.rstrip("\n"))
        # 2) 正文键块: 紧跟 FETCH_STALL_ROUNDS 之后(与当前类目配置同位置); 没有则追加到末尾
        out, inserted = [], False
        for ln in lines:
            out.append(ln)
            if not inserted and re.match(r"^FETCH_STALL_ROUNDS\s*:", ln):
                out.extend(BLOCK.rstrip("\n").split("\n"))
                inserted = True
        if not inserted:
            if out and out[-1].strip() == "":
                out.pop()
            out.extend(BLOCK.rstrip("\n").split("\n"))
        new = "\n".join(out)
        if not new.endswith("\n"):
            new += "\n"
        p.write_text(new, encoding="utf-8", newline="\n")
        updated += 1

print(f"\n已补写 {updated} 份; 跳过(已含键) {skipped_has} 份; 跳过(无头部) {skipped_nohdr} 份")
