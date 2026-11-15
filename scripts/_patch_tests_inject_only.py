# -*- coding: utf-8 -*-
"""补丁: 追加「仅注入词」(`[仅注入]` 分区)的测试。

覆盖:
  · 分区读写与向后兼容(无分区头的旧文件);
  · 仅注入词只进步骤10 注入、**不进步骤9 抓取**、不进门槛;
  · 自动态下仅注入词同样生效;
  · set_list 保存 inject 时不动核心分区(反之亦然)。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- ① core 层
P1 = ROOT / "tests" / "test_keyword_lists.py"
NEW_IMPORTS_OLD = """from core.keyword_lists import (
    MODE_AUTO,
    MODE_MANUAL,
    MODE_STOP,
    core_manual_name,
    longtail_manual_name,
    manual_first,
    read_keywords_file,
    read_sections,
    read_words,
    resolve,
    set_list,
    set_stop,
    status,
    stop_name,
    used_head,
    used_name,
    write_used,
    write_words,
)"""
NEW_IMPORTS_NEW = """from core.keyword_lists import (
    MODE_AUTO,
    MODE_MANUAL,
    MODE_STOP,
    core_manual_name,
    longtail_manual_name,
    manual_first,
    read_core_sections,
    read_keywords_file,
    read_sections,
    read_words,
    resolve,
    set_list,
    set_stop,
    status,
    stop_name,
    used_head,
    used_name,
    write_core_sections,
    write_used,
    write_words,
)"""

APPEND_CORE = '''

# ======================================================================
# 「仅注入」分区(2026-10-06 用户口径: "注入词只能和核心词同步吗, 不能添加吗")
#   仅注入词 = 只参与步骤10 判定、**不被步骤9 抓取**、不进门槛; 自动态下也生效。
# ======================================================================
def test_read_core_sections_legacy_file_has_no_inject(ws_tmp):
    """**向后兼容**: 没有分区头的旧文件(纯词表)全部算核心大词, 仅注入为空。"""
    d = _mk(ws_tmp)
    p = d / core_manual_name(CAT)
    p.write_text("a\\nb\\nc\\n", encoding="utf-8")
    core, inject = read_core_sections(p)
    assert core == ["a", "b", "c"]
    assert inject == []


def test_read_core_sections_splits_two_sections(ws_tmp):
    """有 `[核心大词]` / `[仅注入]` 两分区时按分区读。"""
    d = _mk(ws_tmp)
    p = d / core_manual_name(CAT)
    p.write_text("[核心大词]\\na\\nb\\n[仅注入]\\n手填词\\nc\\n", encoding="utf-8")
    assert read_core_sections(p) == (["a", "b"], ["手填词", "c"])


def test_write_core_sections_dedup_and_delete(ws_tmp):
    """写回: 与核心重复的仅注入词被剔除; 两分区都空则删文件(回自动)。"""
    d = _mk(ws_tmp)
    p = d / core_manual_name(CAT)
    c, i = write_core_sections(p, ["A", "b"], ["B", " 手填 "])
    assert c == ["a", "b"] and i == ["手填"]
    assert p.read_text(encoding="utf-8") == "[核心大词]\\na\\nb\\n[仅注入]\\n手填\\n"
    write_core_sections(p, [], [])
    assert not p.exists()


def test_resolve_inject_words_and_gate_ignores_them(ws_tmp):
    """仅注入词进 `plan.inject_words`, 但**不影响门槛**、模式仍可是自动。"""
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), [], ["only one", "only two"])
    plan = resolve(d, CAT, auto_core=["auto1", "auto2"], auto_longtail=[],
                   gate=2, gate_applies=True)
    assert plan.mode == MODE_AUTO                 # 只有仅注入词 -> 仍是自动态
    assert plan.core == ["auto1", "auto2"]        # 抓取/门槛只看核心大词
    assert plan.inject_only == ["only one", "only two"]
    assert plan.inject_words == ["auto1", "auto2", "only one", "only two"]
    assert plan.gate_ok is True                   # 门槛按 2 个自动核心词判, 与仅注入无关


def test_resolve_inject_words_dedup_with_core(ws_tmp):
    """核心大词与仅注入词重复时, `inject_words` 只出现一次(按核心位置)。"""
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), ["a", "b"], ["b", "c"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=1, gate_applies=True)
    assert plan.inject_words == ["a", "b", "c"]


def test_set_list_inject_keeps_core_section(ws_tmp):
    """保存仅注入清单**不动**核心分区; 清空仅注入也不动核心分区。"""
    d = _mk(ws_tmp)
    set_list(d, CAT, "core", "manual", ["c1", "c2"])
    r = set_list(d, CAT, "inject", "manual", ["x1", "x2"])
    assert r["ok"] and r["saved"] == ["x1", "x2"]
    assert read_core_sections(d / core_manual_name(CAT)) == (["c1", "c2"], ["x1", "x2"])
    # 清空仅注入 -> 文件保留(核心还在)
    r2 = set_list(d, CAT, "inject", "auto")
    assert r2["ok"] and r2["saved"] == []
    assert read_core_sections(d / core_manual_name(CAT)) == (["c1", "c2"], [])
    assert (d / core_manual_name(CAT)).exists()


def test_set_list_core_auto_keeps_inject_section(ws_tmp):
    """切回自动口径清掉核心大词时, **仅注入词必须保留**(它们是独立附加清单)。"""
    d = _mk(ws_tmp)
    set_list(d, CAT, "core", "manual", ["c1"])
    set_list(d, CAT, "inject", "manual", ["x1"])
    set_list(d, CAT, "core", "auto")
    assert read_core_sections(d / core_manual_name(CAT)) == ([], ["x1"])
    # 两个分区都空了才删文件
    set_list(d, CAT, "inject", "auto")
    assert not (d / core_manual_name(CAT)).exists()


def test_set_list_inject_limit(ws_tmp):
    """仅注入词也有数量上限(误粘贴保护)。"""
    d = _mk(ws_tmp)
    from core.keyword_lists import MAX_INJECT
    r = set_list(d, CAT, "inject", "manual", [f"w{i}" for i in range(MAX_INJECT + 1)])
    assert r["ok"] is False and "最多" in r["error"]


def test_status_reports_inject_limit(ws_tmp):
    d = _mk(ws_tmp)
    st = status(d, CAT)
    assert st["max_inject"] > 0 and st["inject_section"] == "[仅注入]"


def test_used_file_marks_inject_section(ws_tmp):
    """留痕文件也写 `[仅注入]` 分区, 便于事后核对注入口径。"""
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), ["a"], ["only"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=1, gate_applies=True)
    path = write_used(d, CAT, plan)
    text = Path(path).read_text(encoding="utf-8")
    assert "# 步骤10 注入词 2 个 = 核心大词 1 个 + 仅注入 1 个" in text
    assert "[仅注入]" in text


# ---------------------------------------------------------------- 步骤9/10 分工
def test_step09_crawl_excludes_inject_only(ws_tmp):
    """**核心断言**: 仅注入词只给步骤10 判定, 步骤9 的抓取词里**不能有**它们。"""
    from steps.step09_search_collect import Step09SearchCollect
    d = _mk(ws_tmp)
    write_core_sections(d / core_manual_name(CAT), ["core one"], ["inject only"])
    plan = resolve(d, CAT, auto_core=[], auto_longtail=[], gate=1, gate_applies=True)
    s9 = Step09SearchCollect()
    words = s9._build_collect_list(None, list(plan.core), list(plan.longtail),
                                   manual_tail=plan.longtail_manual_active)
    assert "core one" in words
    assert "inject only" not in words
'''

# ---------------------------------------------------------------- ② step10 层
P2 = ROOT / "tests" / "test_step10_parallel.py"
APPEND_STEP10 = '''

def test_final_keywords_includes_inject_only_words(ws_tmp):
    """仅注入词(`[仅注入]` 分区)进步骤10 的判定参考词 —— 这就是"能添加"的落地。

    自动态: 注入 = FR-07 达标自动核心词 + 仅注入词。
    """
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "auto one", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\\n[仅注入]\\ninject only\\n另一个仅注入词\\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["auto one", "inject only", "另一个仅注入词"]


def test_final_keywords_manual_core_plus_inject_only(ws_tmp):
    """人工核心清单 + 仅注入词 同时生效(核心在抓取里, 仅注入只在判定里)。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "auto one", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\\nmy core\\n[仅注入]\\ninject only\\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["my core", "inject only"]


def test_final_keywords_inject_only_survives_legacy_file(ws_tmp):
    """旧格式(无分区头)的文件仍按核心大词读 -> 行为与改动前一致(零回归)。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "auto one", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("legacy core\\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["legacy core"]
'''


def main() -> int:
    for path, old, new, tag in (
        (P1, NEW_IMPORTS_OLD, NEW_IMPORTS_NEW, "test_keyword_lists 导入"),
    ):
        t = path.read_text(encoding="utf-8")
        if t.count(old) != 1:
            print(f"!! [{tag}] 命中 {t.count(old)} 次 -> 中止")
            return 1
        path.write_text(t.replace(old, new, 1), encoding="utf-8")
        print(f"OK  {tag}")

    for path, block, tag in ((P1, APPEND_CORE, "test_keyword_lists 追加用例"),
                             (P2, APPEND_STEP10, "test_step10_parallel 追加用例")):
        t = path.read_text(encoding="utf-8")
        if "[仅注入]" in t:
            print(f"!! [{tag}] 已存在 -> 中止")
            return 1
        path.write_text(t.rstrip("\n") + "\n" + block, encoding="utf-8")
        print(f"OK  {tag}  +{block.count(chr(10))} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
