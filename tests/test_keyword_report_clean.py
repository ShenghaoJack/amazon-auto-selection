# -*- coding: utf-8 -*-
"""关键词报告脏数据清理机制测试(2026-10-10)。

背景: `长尾词` sheet 里混着 `搜索配置` 方法行(全仓库 35/35 份报告都这样), 用户要求做成**机制**:
写入端(step8)不再产出 + 存量报告可清理 + 规则单一事实来源。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import keyword_report_clean as kc                            # noqa: E402


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _mk_report(path: Path, words=("dog dryer for pets", "dog dryer for salon"),
               with_methods: bool = True, with_method_sheet: bool = True) -> Path:
    """造一份与真实结构一致的迷你关键词报告。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "结论总览"
    ws.append(["项目", "内容"])
    ws = wb.create_sheet("长尾词")
    ws.append(["长尾词", "维度", "说明"])
    for w in words:
        ws.append([w, "—", "来自 FR-04 关键词文件(人工清单保留)"])
    if with_methods:
        ws.append(["搜索配置", "搜索页数", "核心大词每词搜索 5 页, 长尾词每词搜索 4 页"])
        ws.append(["搜索配置", "广告处理", "只取自然位, 跳过含 Sponsored 标记的广告条目"])
        ws.append(["搜索配置", "清单合并", "搜索结果 ASIN 并入双榜清单后全量去重"])
    if with_method_sheet:
        m = wb.create_sheet("方法说明")
        m.append(["项目", "说明"])
        m.append(["筛选标准", "月搜索量>=3万 ..."])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ---------------------------------------------------------------- 规则

def test_is_method_row_rules():
    # 方法名 / 方法标签 / 同表重复 / 空 → 方法行
    assert kc.is_method_row("搜索配置") is True
    assert kc.is_method_row("搜索配置", "搜索页数") is True
    assert kc.is_method_row("随便一个词", "清单合并") is True
    assert kc.is_method_row("重复标签", "—", "", 2) is True
    assert kc.is_method_row("") is True
    # 真实长尾词不是方法行(即使 `维度` 是 `—` 这种空占位)
    assert kc.is_method_row("dog dryer for pets", "—") is False
    assert kc.is_method_row("cutting board for home", "", "来自 FR-04 关键词文件") is False


def test_split_and_filter():
    rows = [("dog dryer for pets", "—", "人工清单保留"),
            ("搜索配置", "搜索页数", "5 页"),
            ("dog dryer for salon", "—", "人工清单保留"),
            ("搜索配置", "广告处理", "只取自然位")]
    words, methods = kc.split_longtail_rows(rows)
    assert [w["keyword"] for w in words] == ["dog dryer for pets", "dog dryer for salon"]
    assert [m["dim"] for m in methods] == ["搜索页数", "广告处理"]
    # 写入端防御过滤: 关键词列表里混进方法名也会被剔除
    assert kc.filter_longtail_words(["ok word", "搜索配置", "ok word"]) == ["ok word", "ok word"]


# ---------------------------------------------------------------- 体检

def test_find_dirty_detects_three_method_rows(tmp_path):
    p = _mk_report(tmp_path / "a-keyword-report.xlsx")
    found = kc.find_dirty(p)
    assert set(found) == {"长尾词"}
    assert [h["row"] for h in found["长尾词"]] == [4, 5, 6]        # 表头=1, 两个真实词=2,3
    assert {h["dim"] for h in found["长尾词"]} == {"搜索页数", "广告处理", "清单合并"}
    clean_p = _mk_report(tmp_path / "b-keyword-report.xlsx", with_methods=False)
    assert kc.find_dirty(clean_p) == {}
    assert kc.find_dirty(tmp_path / "nope.xlsx") == {}


def test_clean_workbook_dry_run_does_not_touch_file(tmp_path):
    p = _mk_report(tmp_path / "a-keyword-report.xlsx")
    before = _sha(p)
    out = kc.clean_workbook(p, apply=False)
    assert out["dirty"] == 3 and out["applied"] is False and out["removed"] == 0
    assert _sha(p) == before, "dry-run 绝不能改文件"
    assert not p.with_suffix(".xlsx.bak").exists()


# ---------------------------------------------------------------- 清理

def test_clean_workbook_apply_moves_rows_and_backs_up(tmp_path):
    p = _mk_report(tmp_path / "a-keyword-report.xlsx")
    before = _sha(p)
    out = kc.clean_workbook(p, apply=True)
    assert out["applied"] and out["removed"] == 3 and out["moved"] == 3
    bak = Path(out["backup"])
    assert bak.is_file() and _sha(bak) == before, "备份应是**清理前**的原始文件"

    wb = load_workbook(p, data_only=True)
    ws = wb["长尾词"]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    assert rows[0][:3] == ["长尾词", "维度", "说明"]              # 表头不变
    assert [r[0] for r in rows[1:]] == ["dog dryer for pets", "dog dryer for salon"]
    assert all(str(r[0]) != "搜索配置" for r in rows[1:])
    ms = [list(r) for r in wb["方法说明"].iter_rows(values_only=True)]
    labels = [str(r[0]) for r in ms]
    assert any(l.startswith("搜索配置") for l in labels), labels
    assert any("只取自然位" in str(r[1]) for r in ms), ms        # 方法正文保留, 信息不丢
    assert labels[0] == "项目"                                   # 原表头未被破坏
    wb.close()

    # 幂等: 再跑一次不再有脏数据、不再重复追加
    assert kc.find_dirty(p) == {}
    out2 = kc.clean_workbook(p, apply=True)
    assert out2["dirty"] == 0 and out2["removed"] == 0
    wb = load_workbook(p, data_only=True)
    n_after = wb["方法说明"].max_row
    wb.close()
    assert n_after == len(ms)


def test_clean_workbook_creates_method_sheet_when_missing(tmp_path):
    p = _mk_report(tmp_path / "a-keyword-report.xlsx", with_method_sheet=False)
    out = kc.clean_workbook(p, apply=True)
    assert out["sheets_created"] is True and out["moved"] == 3
    wb = load_workbook(p, data_only=True)
    assert "方法说明" in wb.sheetnames
    vals = [list(r) for r in wb["方法说明"].iter_rows(values_only=True)]
    assert vals[0] == ["项目", "说明"] and len(vals) == 4
    wb.close()


def test_clean_workbook_no_backup_flag(tmp_path):
    p = _mk_report(tmp_path / "a-keyword-report.xlsx")
    out = kc.clean_workbook(p, apply=True, backup=False)
    assert out["backup"] is None
    assert not p.with_suffix(".xlsx.bak").exists()


# ---------------------------------------------------------------- CLI

def test_cli_check_then_apply(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ckr", ROOT / "scripts" / "clean_keyword_report.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    p = _mk_report(tmp_path / "a-keyword-report.xlsx")
    assert cli.main(["--check", str(p)]) == 2          # 脏 → 2
    assert cli.main(["--apply", str(p)]) == 0
    assert cli.main(["--check", str(p)]) == 0          # 幂等 → 0
    assert cli.main(["--check", str(tmp_path / "missing.xlsx")]) == 1


# ---------------------------------------------------------------- 写入端(step8)

def test_step08_longtail_sheet_has_no_method_rows():
    from steps.step08_keyword_report import Step08KeywordReport

    step = Step08KeywordReport()
    step._core_pages, step._tail_pages = 5, 4
    df = step._build_longtail_sheet(["auto w", "manual w", "搜索配置"],
                                    manual_tail={"manual w"}, auto_tail={"auto w"})
    assert list(df.columns) == ["长尾词", "维度", "说明"]
    assert [str(x) for x in df["长尾词"]] == ["auto w", "manual w"], list(df["长尾词"])
    assert not any("搜索配置" in str(x) for x in df["长尾词"])
    # 方法口径改在 `方法说明` sheet 里(项目/说明 两列)
    m = step._build_method_sheet()
    assert list(m.columns) == ["项目", "说明"]
    cfg = [str(x) for x in m[m["项目"] == "搜索配置"]["说明"]]
    assert len(cfg) == 3, cfg
    assert any("搜索页数" in c or "每词搜索" in c for c in cfg)
    assert any("Sponsored" in c for c in cfg)
    assert any("去重" in c for c in cfg)


def test_step08_end_to_end_report_has_clean_longtail(tmp_path):
    """端到端: step8 写出的报告里, `长尾词` sheet 不含方法行, 方法口径在 `方法说明`。"""
    from tests.test_keyword_lists import _make_ctx, _write_kw_file, CAT     # type: ignore
    from steps.step08_keyword_report import Step08KeywordReport

    ctx = _make_ctx(tmp_path, SITE="美国", MARKET="amazon.com")
    d = Path(ctx.paths.cat_root)
    _write_kw_file(d, core=["a", "b"], tail=["dog dryer for home", "dog dryer for pro"])
    (d / f"{CAT}-screen-results.json").write_text(
        '[{"keyword":"a","verdict":"达标"},{"keyword":"b","verdict":"达标"}]', encoding="utf-8")
    step = Step08KeywordReport()
    step.validate_upstream(ctx)                      # 与既有端到端测试一致: 这一步才会设置 _cat
    step.run(ctx)
    out = Path(ctx.resolve_artifact(f"{CAT}-keyword-report.xlsx"))
    assert out.is_file()
    wb = load_workbook(out, data_only=True)
    rows = [list(r) for r in wb["长尾词"].iter_rows(values_only=True)]
    assert all(str(r[0]) != "搜索配置" for r in rows[1:]), rows
    assert kc.find_dirty(out) == {}
    ms = [list(r) for r in wb["方法说明"].iter_rows(values_only=True)]
    assert any(str(r[0]) == "搜索配置" for r in ms[1:]), ms
    wb.close()
