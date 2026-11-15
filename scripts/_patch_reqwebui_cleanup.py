# -*- coding: utf-8 -*-
"""一次性补丁: 清理 _docs_sync/req-webui.md 里残留的「步骤10 注入词」章节 + 卡片顺序。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "_docs_sync" / "req-webui.md"

NEW_FR29 = """- FR-2.9 ~~**步骤10 注入词编辑面板**~~ **已于 2026-10-06 下线, 功能并入 FR-2.13「关键词清单」面板的「核心大词」列**。不变量保持不变: 达标关键词**不是预先写死在过滤提示词里**, 而是**每次运行步骤10 时、在第二遍过滤/自动复核开始前**由步骤10 动态注入(`enrich_prompt_with_keywords`; **步骤3 不注入**, 用 related_title 兜底)。**注入源** = 「关键词清单」面板「核心大词」列的最终生效词 —— 原理与原注入词清单完全一致: 有人工核心清单(`filter-<CAT>-core-words.txt`)就用清单词(**不再看 FR-07**), 否则自动(US 站 = FR-07 判"达标"的词全集 / 非 US 站 = 站内月搜索量前 5)。**长尾词不参与注入**(只用于步骤9 抓取与报告)。**注入词可核对**: 运行日志完整列出实际注入词(`步骤10 注入判定参考词: 新增 +N 个…实际注入词: 词1 | 词2 | …`), 并落盘 `<CAT>-injected-keywords.txt`(逐行一词、每次 run 覆写、**只写不读**), 人工核对过滤口径以日志/该文件为准; 归档时随步骤10 进 `archive/<CAT>-<时间戳>/10_父项变体/`(extra_archive_files, 不参与 DONE 校验)。

"""

EDITS: list[tuple[str, str, str]] = [
    # ① V1.7 修订记录里的卡片顺序
    ("_docs_sync/req-webui.md",
     "位置在「步骤10 注入词」面板**之前**(卡片顺序变为 状态 → **关键词清单** → 注入词 → 属性集 → 标准化 → 日志 → 监控 → 配置, 序号 03–09 同步重编)",
     "卡片顺序为 状态 → **关键词清单** → 属性集 → 标准化 → 日志 → 监控 → 配置(序号 03–08); "
     "原「步骤10 注入词」卡片已于 **2026-10-06 下线**, 其功能并入本面板的「核心大词」列"),
    # ② FR-2.13 位置段
    ("_docs_sync/req-webui.md",
     "**位置**: 运行台卡片顺序为 01 运行控制 → 02 流程状态 → **03 关键词清单** → 04 步骤10 注入词 → 05 步骤16 属性集 → 06 步骤18 标准化 → 07 实时日志 → 08 监控告警 → 09 配置中心(V1.7 起整体重编; 「关键词清单」在「注入词」**之前**, 因为它是更上游的取词口径)。",
     "**位置**: 运行台卡片顺序为 01 运行控制 → 02 流程状态 → **03 关键词清单** → 04 步骤16 属性集 → "
     "05 步骤18 标准化 → 06 实时日志 → 07 监控告警 → 08 配置中心(原 04「步骤10 注入词」卡片已于 "
     "**2026-10-06 下线**, 其功能并入本面板的「核心大词」列)。"),
    # ③ FR-2.13 两份清单段: 说明核心大词列即注入源
    ("_docs_sync/req-webui.md",
     "**人工词不再受 FR-07 判定约束**(不达标词与手填新词都照常进下游); 人工**长尾**词按原样使用(不再做五维实体组装与配件词过滤)。",
     "**人工词不再受 FR-07 判定约束**(不达标词与手填新词都照常进下游)。**「核心大词」列的最终生效词 = "
     "步骤10 的注入词(判定参考词)** —— 在该列勾选/手填即等于选注入词, 这就是原「步骤10 注入词」面板的全部功能; "
     "人工**长尾**词按原样使用(不再做五维实体组装与配件词过滤), 但**长尾词不参与步骤10 注入**。"),
    # ④ webui/README API 行
    ("webui/README.md",
     "**这也是步骤10 的判定参考词来源**(2026-10-06 起)",
     "**其「核心大词」列的生效词就是步骤10 的判定参考词来源**(2026-10-06 起; 长尾词不参与注入)"),
]

# 需要清理的整段(FR-2.9a 起 → 前端实现那行为止)
START = "- FR-2.9a **注入时机与 UI 生效语义"
END = "  - 前端实现: `renderInjectInjected()`"


def main() -> int:
    bak = P.with_name(P.name + ".bak-before-inject-core-only")
    if not bak.is_file():
        print(f"缺少备份 {bak}, 中止")
        return 1
    text = P.read_text(encoding="utf-8")

    for rel, old, new in EDITS:
        path = ROOT / rel
        t = path.read_text(encoding="utf-8")
        n = t.count(old)
        if n != 1:
            print(f"!! [{rel}] 命中 {n} 次 -> 中止: {old[:46]!r}")
            return 1
        path.write_text(t.replace(old, new, 1), encoding="utf-8")
        print(f"OK  {rel}: {old[:40]}…")

    lines = text.splitlines(keepends=True)
    i = next((k for k, s in enumerate(lines) if s.startswith(START)), -1)
    j = next((k for k, s in enumerate(lines) if s.startswith(END)), -1)
    if i < 0 or j < 0 or j < i:
        print(f"FR-2.9 段落锚点失败: start={i} end={j} -> 中止(未写盘)")
        return 1
    print(f"OK  清理 FR-2.9a/2.9b 段落  行 {i + 1}..{j + 1}({j - i + 1} 行) "
          f"-> 1 行下线说明")
    text = "".join(lines[:i] + [NEW_FR29] + lines[j + 1:])
    P.write_text(text, encoding="utf-8")

    left = [l for l in text.splitlines()
            if "inject-words" in l or "renderInjectInjected" in l or "步骤10 注入词" in l]
    print(f"\n剩余相关行 {len(left)} 条:")
    for l in left:
        print("   " + l.strip()[:100])
    return 0


if __name__ == "__main__":
    sys.exit(main())
