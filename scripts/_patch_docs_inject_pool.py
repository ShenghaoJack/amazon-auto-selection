# -*- coding: utf-8 -*-
"""一次性补丁: 文档说明「仅注入词」改为**从核心词候选池点选**。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBS: list[tuple[str, str, str]] = [
    ("CONFIG_GUIDE.md",
     '**想加"只注入、不抓取"的词**(2026-10-06): 用面板「核心大词」列下方那条**仅注入词**行 —— 它们存在同一文件的 `[仅注入]` 分区,',
     '**想加"只注入、不抓取"的词**(2026-10-06): 用面板「核心大词」列下方的**仅注入词**区 —— '
     '下面有一个**可点的「从核心词中选择」池**(列出核心词候选里**尚未注入**的那些, 按 FR-07 判定分组, '
     '点一下即加入/移出仅注入), 候选之外的词还可用补充框手填。它们存在同一文件的 `[仅注入]` 分区,'),
    ("webui/README.md",
     '另有**仅注入词**一行(2026-10-06): 只作步骤10 判定、**不被步骤9 抓取**、不计入门槛, 自动态下也生效 —— 存在同一文件的 `[仅注入]` 分区, 与核心大词互不覆盖; 接口仍走 `POST /api/keyword-lists/save`(`list: "inject"`)',
     '另有**仅注入词**区(2026-10-06): 只作步骤10 判定、**不被步骤9 抓取**、不计入门槛, 自动态下也生效 —— '
     '**从核心词候选池点选**(池子只列尚未注入的候选, 按 FR-07 判定分组, 琥珀色 ▸ 标记已选; '
     '候选之外的词可用补充框手填), 存在同一文件的 `[仅注入]` 分区, 与核心大词互不覆盖; '
     '接口仍走 `POST /api/keyword-lists/save`(`list: "inject"`)'),
    ("_docs_sync/req-webui.md",
     '**仅注入词(2026-10-06 追加, 回答"注入词只能和核心词同步吗, 不能添加吗")**: 核心大词列下方独立一行, 用于加"只参与步骤10 判定、**不去抓取**"的词。',
     '**仅注入词(2026-10-06 追加, 回答"注入词只能和核心词同步吗, 不能添加吗"; 随后按"仅注入改成自选, '
     '可以从核心词中选择"改为点选)**: 核心大词列下方的独立小区, 用于加"只参与步骤10 判定、**不去抓取**"的词。'
     '**选取方式 = 可点的池子**(`renderKwInjectPool()`, 容器 `#kwInjectPool`): 池子内容 = 核心词候选池 '
     '`kwData.candidates` **减去已注入的**(生效核心词 `core.effective` ∪ 正在编辑的人工核心清单 `kwEdit.core`), '
     '按 FR-07 判定分组(与候选词池同构), 点词走 `toggleKwWord("inject", w)` 加入/移出; '
     '选中态用**琥珀色 + ▸ 前缀**与核心大词的绿色 ✓ 区分。候选之外的词仍可用补充框手填。'),
]


def main() -> int:
    for rel, old, new in SUBS:
        p = ROOT / rel
        t = p.read_text(encoding="utf-8")
        n = t.count(old)
        if n != 1:
            print(f"!! [{rel}] 命中 {n} 次(期望1) -> 中止: {old[:56]!r}")
            return 1
        p.write_text(t.replace(old, new, 1), encoding="utf-8")
        print(f"OK  {rel}: {old[:44]}…")
    print("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
