# -*- coding: utf-8 -*-
"""一次性补丁: 更新受「步骤10 词源改为关键词清单同步」影响的测试。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAK_SUFFIX = ".bak-before-inject-sync"
log: list[str] = []


def load(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines(keepends=True)


def save(p: Path, lines: list[str]) -> None:
    p.write_text("".join(lines), encoding="utf-8")


def repl_line(lines: list[str], prefix: str, new: str, tag: str) -> list[str]:
    hits = [k for k, s in enumerate(lines) if s.startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit(f"[{tag}] 前缀 {prefix!r} 命中 {len(hits)} 行(期望1) -> 中止")
    lines[hits[0]] = new
    log.append(f"OK  {tag}  行 {hits[0] + 1}")
    return lines


def del_line(lines: list[str], prefix: str, tag: str) -> list[str]:
    hits = [k for k, s in enumerate(lines) if s.startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit(f"[{tag}] 前缀 {prefix!r} 命中 {len(hits)} 行(期望1) -> 中止")
    log.append(f"OK  {tag}  删除行 {hits[0] + 1}")
    return lines[:hits[0]] + lines[hits[0] + 1:]


def insert_after(lines: list[str], prefix: str, new_lines: list[str], tag: str) -> list[str]:
    hits = [k for k, s in enumerate(lines) if s.startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit(f"[{tag}] 前缀 {prefix!r} 命中 {len(hits)} 行(期望1) -> 中止")
    log.append(f"OK  {tag}  在行 {hits[0] + 1} 后插入 {len(new_lines)} 行")
    return lines[:hits[0] + 1] + new_lines + lines[hits[0] + 1:]


# ============================================================ A. test_non_us_keywords.py
p = ROOT / "tests" / "test_non_us_keywords.py"
L = load(p)
L = repl_line(
    L, "#      且步骤10 注入词与步骤9 用",
    "#      且步骤10 的判定参考词与步骤9 用**同一批词**(2026-10-06 起直接同步「关键词清单」的\n"
    "#      生效核心大词 + 长尾词, 避免\"搜索用前5、判定参考词为空\"的错配)。\n",
    "非US: 更新模块 docstring")
L = repl_line(
    L, "def test_step10_manual_list_still_wins_on_non_us(",
    "def test_step10_follows_manual_core_list_on_non_us(ws_tmp):\n",
    "非US: 测试改名")
L = repl_line(
    L, '    """人工清单优先级最高:',
    '    """人工核心清单优先级最高: 非 US 站也一样(用户手填的词不被"前5"覆盖)。\n',
    "非US: 更新 docstring")
L = repl_line(
    L, "    with open(ctx.resolve_artifact(f\"filter-{ctx.config.cat}-inject-words.txt\"),",
    "    with open(ctx.resolve_artifact(f\"filter-{ctx.config.cat}-core-words.txt\"),\n",
    "非US: 改读人工核心词清单")
save(p, L)

# ============================================================ B. test_step10_parallel.py
p = ROOT / "tests" / "test_step10_parallel.py"
L = load(p)
L = repl_line(
    L, "def test_final_keywords_manual_inject_file_preferred(",
    "def test_final_keywords_follows_manual_core_words(ws_tmp):\n",
    "step10: 测试改名")
L = repl_line(
    L, '    """存在人工清单 filter-<CAT>-inject-words.txt',
    '    """存在人工核心清单 filter-<CAT>-core-words.txt 时, 判定参考词用清单词\n'
    '    (2026-10-06: 步骤10 词源与「关键词清单」同步, 不再单独维护注入词清单)。\n',
    "step10: 更新 docstring(首行)")
L = repl_line(
    L, "    inject = os.path.join(root, \"filter-t-inject-words.txt\")",
    "    inject = os.path.join(root, \"filter-t-core-words.txt\")\n",
    "step10: 改文件名")
L = del_line(L, "    # 人工清单优先(即使 screen 也有 pepper grinder); 自定义词保留",
             "step10: 删旧注释行")
L = repl_line(
    L, "    assert out == [\"pepper grinder\", \"自定义词 custom set\"]",
    "    # 人工核心清单优先(即使 screen 也有 pepper grinder); 手填自定义词保留\n"
    "    assert out == [\"pepper grinder\", \"自定义词 custom set\"]\n",
    "step10: 补注释")

NEW_TEST = '''
def test_final_keywords_includes_longtail_words(ws_tmp):
    """2026-10-06 用户口径: 判定参考词 = 生效核心大词 + **长尾词**(与关键词清单同步)。

    长尾词同样是"本类目产品"的证据 -> 步骤10 必须一并注入(原来只有 FR-07 达标核心词)。
    """
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "cutting board", "verdict": "达标"},
                   {"keyword": "knife", "verdict": "不达标"}], fh)
    with open(os.path.join(root, "t-keywords.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\\ncutting board\\n[长尾词]\\n"
                 "cutting board for kitchen\\ntravel cutting board\\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["cutting board", "cutting board for kitchen", "travel cutting board"]


def test_final_keywords_manual_longtail_with_core(ws_tmp):
    """人工长尾清单也生效(独立于核心词): 核心=自动FR-07 + 长尾=人工清单。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "cutting board", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-longtail-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("my own long tail\\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["cutting board", "my own long tail"]

'''
L = insert_after(L, '    assert s._final_keywords(ctx, "t") == []',
                 NEW_TEST.splitlines(keepends=True), "step10: 新增长尾词测试")
save(p, L)

# ============================================================ C. test_webui_status_cfg.py
p = ROOT / "tests" / "test_webui_status_cfg.py"
L = load(p)
L = repl_line(
    L, '               "/api/inject-words?file=", "/api/log?file="):',
    '               "/api/keyword-lists?file=", "/api/log?file="):\n',
    "status_cfg: 接口断言换成 keyword-lists")
save(p, L)

# ============================================================ D. test_webui_keyword_lists.py
p = ROOT / "tests" / "test_webui_keyword_lists.py"
L = load(p)
L = repl_line(
    L, "# 面板把\"步骤10 注入词\"那套(人工清单文件 + 删除即回自动)推广到核心大词/长尾词, 并加",
    "# 面板用\"人工清单文件 + 删除即回自动\"那套(源自已下线的步骤10 注入词面板)管理核心大词/\n"
    "# 长尾词, 并加",
    "kw_panel: 更新注释(1)")
L = repl_line(
    L, "#   · 面板的自动核心词**直接复用 inject_panel() 的 auto_qualified** —— 保证",
    "#   · 面板的自动核心词复用 keyword_candidates() 的 auto_qualified —— 保证",
    "kw_panel: 更新注释(2)")
L = repl_line(
    L, "#     \"面板 / 步骤9 搜索 / 步骤10 注入词\"三处永远是同一批词(这是本面板存在的意义)。",
    "#     \"面板 / 步骤9 搜索 / 步骤10 判定参考词\"三处永远是同一批词(这是本面板存在的意义);\n"
    "#     2026-10-06 起步骤10 直接同步本面板的「生效核心大词 + 长尾词」, 注入词面板已下线。\n",
    "kw_panel: 更新注释(3)")
save(p, L)

print("\n".join(log))
print("\n全部测试补丁写完")
