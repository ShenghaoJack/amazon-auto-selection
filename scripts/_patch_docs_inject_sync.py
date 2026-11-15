# -*- coding: utf-8 -*-
"""一次性补丁: 同步文档(注入词面板已下线, 步骤10 判定参考词改为与关键词清单同步)。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
log: list[str] = []


def patch(rel: str, edits: list[tuple[str, str]], drops: list[str] | None = None) -> None:
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    orig = text
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            raise SystemExit(f"[{rel}] 片段命中 {n} 次(期望1): {old[:60]!r} -> 中止")
        text = text.replace(old, new, 1)
        log.append(f"OK  {rel}  替换: {old[:46]}…")
    for frag in drops or []:
        lines = text.splitlines(keepends=True)
        hits = [k for k, s in enumerate(lines) if frag in s]
        if len(hits) != 1:
            raise SystemExit(f"[{rel}] 待删行命中 {len(hits)} 次(期望1): {frag[:60]!r} -> 中止")
        text = "".join(lines[:hits[0]] + lines[hits[0] + 1:])
        log.append(f"OK  {rel}  删除行 {hits[0] + 1}: {frag[:46]}…")
    if text == orig:
        raise SystemExit(f"[{rel}] 没有任何变化 -> 中止")
    p.write_text(text, encoding="utf-8")


# ============================================================ CONFIG_GUIDE.md
patch("CONFIG_GUIDE.md", [
    ("**步骤10 注入词可用 webui「步骤10 注入词」面板人工勾选/补充**(存 `filter-<CAT>-inject-words.txt`, 优先于自动达标全集)。**2026-09-29 起另可用 webui「关键词清单」面板直接改核心大词/长尾词三态**",
     "**2026-09-29 起可用 webui「关键词清单」面板直接改核心大词/长尾词三态**"),
    ("停止标记存 `filter-<CAT>-keyword-stop.txt`(存在则步骤9 不抓取); 生效口径每次运行写进 `<CAT>-used-keywords.txt` 留痕 |",
     "停止标记存 `filter-<CAT>-keyword-stop.txt`(存在则步骤9 不抓取); 生效口径每次运行写进 `<CAT>-used-keywords.txt` 留痕。"
     "**2026-10-06: 原「步骤10 注入词」面板已下线** —— 步骤10 的判定参考词改为**直接同步本面板的生效核心大词 + 长尾词**"
     "(不再单独维护 `filter-<CAT>-inject-words.txt`), 因此\"面板 / 步骤9 搜索 / 步骤10 判定参考词\"三处永远是同一批词 |"),
])

# ============================================================ webui/README.md
patch("webui/README.md", [
    # L61 卡片顺序
    ("①运行控制 → ②流程状态 → **③关键词清单(核心大词/长尾词 三态: 自动 / 人工清单 / 人工停止, 2026-09-29 新增)** → **④步骤10 注入词(达标关键词)** → **⑤步骤16 属性集提取与确认(2026-09-21 新增: 增删/改名/改说明 + 确认弹窗)** → **⑥步骤18 标准化",
     "①运行控制 → ②流程状态 → **③关键词清单(核心大词/长尾词 三态: 自动 / 人工清单 / 人工停止, 2026-09-29 新增)** → **④步骤16 属性集提取与确认(2026-09-21 新增: 增删/改名/改说明 + 确认弹窗)** → **⑤步骤18 标准化"),
    ("⑦实时日志 → ⑧监控告警 → ⑨配置中心(2026-09 重排:",
     "⑥实时日志 → ⑦监控告警 → ⑧配置中心(2026-09 重排:"),
    ("顺序由 `reorderCards()` 显式重排(状态 → 关键词清单 → 注入词 → 属性集 → 标准化)",
     "顺序由 `reorderCards()` 显式重排(状态 → 关键词清单 → 属性集 → 标准化)"),
    # L73 关键词清单行里的注入词措辞
    ("| ③(运行台卡片) 关键词清单 | **核心大词 / 长尾词 三态编辑面板(2026-09-29 新增; 位于「步骤10 注入词」之前)**: 把「步骤10 注入词」那套人工清单模式推广到核心大词/长尾词, 并新增「人工停止」。**三态**(由文件是否存在决定, 与注入词同款\"文件即状态\")",
     "| ③(运行台卡片) 关键词清单 | **核心大词 / 长尾词 三态编辑面板(2026-09-29 新增)**: 用人工清单模式管理核心大词/长尾词, 并带「人工停止」。**三态**(由文件是否存在决定, \"文件即状态\")"),
    ("**三处口径一致**: 面板的自动核心词直接复用注入词面板的 `auto_qualified`, 且步骤6/8/9 共用 `core/keyword_lists.py` 同一 resolver —— 不会出现\"报告显示 ❌停止、实际却在跑\"。步骤04/07 完成后与注入词面板一起自动刷新(有未保存修改则跳过并提示) |",
     "**三处口径一致(2026-10-06 起更进一步)**: 面板的自动核心词来自 `webui/server.py::keyword_candidates()` 的 `auto_qualified`, "
     "且步骤6/8/9 **与步骤10** 共用 `core/keyword_lists.py` 同一 resolver —— 不会出现\"报告显示 ❌停止、实际却在跑\"; "
     "**步骤10 的判定参考词 = 本面板最终生效的核心大词 + 长尾词**(原「步骤10 注入词」面板已下线, 词表合二为一)。"
     "步骤04/07 完成后自动刷新本面板(有未保存修改则跳过并提示) |"),
], drops=["| ⑥ 步骤10 注入词 | **达标关键词注入源编辑面板**"])

# ============================================================ _docs_sync/req-webui.md
patch("_docs_sync/req-webui.md", [
    ("- M6 **步骤10 注入词编辑面板(新增卡片, 见 FR-2.9)**: 展示全部大词候选 + 站内月搜索量 + FR-07 达标标注; **自动模式**(默认)= 步骤10 注入 FR-07 全部达标词(核心=全部候选, 达标不限个数, 2026-09); **人工模式** = 勾选合适词 + 补充自定义词, 保存 `filter-<CAT>-inject-words.txt`, 步骤10 存在清单优先用清单词; 数据源 keywords-candidates/amazon-keyword-data/screen-results; 接口 GET/POST `/api/inject-words*`;",
     "- ~~M6 步骤10 注入词编辑面板~~ **已于 2026-10-06 下线**: 步骤10 的判定参考词改为**直接同步「关键词清单」面板的生效核心大词 + 长尾词**(`core/keyword_lists.py::resolve()`), 不再单独维护 `filter-<CAT>-inject-words.txt`, 也不再有人工勾选 UI;"),
], drops=["- FR-2.9 **步骤10 注入词编辑面板(独立卡片, **序号 04**",
          "| GET | `/api/inject-words?file=` |",
          "| POST | `/api/inject-words/save` |"])

print("\n".join(log))
print("\n文档补丁写完")
