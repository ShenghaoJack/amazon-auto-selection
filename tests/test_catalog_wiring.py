# -*- coding: utf-8 -*-
"""接线：让代码**真的会问解析器**（方案甲第 2 步，2026-10-10）。

接在 **4 个 choke point**（不是改 60 个调用点）：
    `core/paths.py`      `cat_file` / `resolve_cat_relative`
    `steps/base.py`      `resolve_artifact`
    `tools/attr_stability/spec.py`  `find_rules_file`

★ **只对定义层文件名生效**（属性集 / 规则 / 学习映射 / 词表聚类报告）——
  其他任何名字（交付表 / 稳定性结果 / 输入 json …）**原样返回，行为逐字不变**。
  这是"一处接线、60 个调用点自动受益"的关键，也是不把简单事变复杂的关键。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.paths import Paths
from steps.base import StepContext
from tools.attr_stability.catalog import catalog_dir_of, resolve_definition_file
from tools.attr_stability.spec import find_rules_file

CAT = "Cutting Boards-US-202608"


def _mkdata(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / CAT).mkdir(parents=True)
    return d


# ---------------------------------------------------------------- 统一入口
def test_non_definition_names_unchanged(tmp_path):
    """★★ **非定义层文件名必须逐字不变** —— 否则交付表/稳定性结果等全都会跑偏。"""
    d = _mkdata(tmp_path)
    run = d / CAT
    for n in (f"{CAT}-属性标注-标准化交付.xlsx", "17_属性标注/稳定性结果.json",
              "input.json", f"{CAT}-数值异常校验.json"):
        assert resolve_definition_file(run, n) == run / n, n


def test_definition_names_own_dir_first(tmp_path):
    d = _mkdata(tmp_path)
    run = d / CAT
    (run / f"{CAT}-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    cd = catalog_dir_of(d, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / f"{CAT}-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    assert resolve_definition_file(run, f"{CAT}-attribute-spec.json") == \
        run / f"{CAT}-attribute-spec.json", "自己目录第一优先"


def test_definition_names_inherit_from_shared(tmp_path):
    """自己目录没有（**已迁移**）→ 从共享沉淀层继承 —— 这正是迁移的意义。"""
    d = _mkdata(tmp_path)
    run = d / CAT
    cd = catalog_dir_of(d, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / f"{CAT}-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    assert resolve_definition_file(run, f"{CAT}-attribute-spec.json") == \
        cd / f"{CAT}-attribute-spec.json"


# ---------------------------------------------------------------- Paths
def _paths(tmp_path: Path) -> Paths:
    d = _mkdata(tmp_path)
    p = Paths.__new__(Paths)                                  # 不跑 __init__（避开真配置）
    p.data_root = d
    p.cat_root = d / CAT
    return p


def test_paths_cat_file_inherits(tmp_path):
    p = _paths(tmp_path)
    cd = catalog_dir_of(p.data_root, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / f"{CAT}-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    got = Path(p.cat_file(f"{CAT}-attribute-spec.json"))
    assert got == cd / f"{CAT}-attribute-spec.json"


def test_paths_cat_file_non_definition_unchanged(tmp_path):
    p = _paths(tmp_path)
    got = Path(p.cat_file("input.json"))
    assert got == p.cat_root / "input.json"


def test_paths_cat_file_ensures_parent(tmp_path):
    """★ 原来的 `cat_file` 会 `ensure` 目录（保证可写）—— 接线后**不能丢**这个性质。"""
    p = _paths(tmp_path)
    p.cat_root.rmdir()                                        # 故意删掉
    Path(p.cat_file("some-new-file.json"))
    assert p.cat_root.is_dir(), "父目录必须被建出来，否则后续写盘会失败"


def test_paths_resolve_cat_relative_inherits(tmp_path):
    p = _paths(tmp_path)
    cd = catalog_dir_of(p.data_root, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / "cutting_boards.rules.json").write_text("{}", encoding="utf-8")
    got = Path(p.resolve_cat_relative("cutting_boards.rules.json"))
    assert got == cd / "cutting_boards.rules.json"


# ---------------------------------------------------------------- StepContext
def test_resolve_artifact_routes_definition_layer(tmp_path):
    p = _paths(tmp_path)
    ctx = StepContext.__new__(StepContext)
    ctx.paths = p
    cd = catalog_dir_of(p.data_root, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / f"{CAT}-attribute-spec.json").write_text(json.dumps({"status": "confirmed", "attributes": [{"name": "材质"}]}), encoding="utf-8")
    assert Path(ctx.resolve_artifact(f"{CAT}-attribute-spec.json")) == \
        cd / f"{CAT}-attribute-spec.json"
    assert Path(ctx.resolve_artifact("18_标准化/x.xlsx")) == \
        p.cat_root / "18_标准化/x.xlsx", "非定义层逐字不变"


# ---------------------------------------------------------------- find_rules_file
def test_find_rules_file_falls_back_to_shared(tmp_path):
    """★ `find_rules_file`：自己目录没有 → 共享层兜底（迁移后 rules 就只在共享层）。"""
    d = _mkdata(tmp_path)
    run = d / CAT
    assert find_rules_file(str(run)) is None, "两处都没有 → None（原行为）"
    cd = catalog_dir_of(d, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / "cutting_boards.rules.json").write_text("{}", encoding="utf-8")
    got = find_rules_file(str(run))
    assert got and Path(got) == cd / "cutting_boards.rules.json"


def test_find_rules_file_own_dir_first(tmp_path):
    d = _mkdata(tmp_path)
    run = d / CAT
    (run / "cutting_boards.rules.json").write_text("{}", encoding="utf-8")
    cd = catalog_dir_of(d, "Cutting Boards")
    cd.mkdir(parents=True)
    (cd / "shared.rules.json").write_text("{}", encoding="utf-8")
    got = find_rules_file(str(run))
    assert Path(got) == run / "cutting_boards.rules.json", "自己的永远优先"


def test_find_rules_file_ambiguous_returns_none(tmp_path):
    """自己目录里**多个** rules.json → 返回 None（原行为：宁可无也不猜）。"""
    d = _mkdata(tmp_path)
    run = d / CAT
    (run / "a.rules.json").write_text("{}", encoding="utf-8")
    (run / "b.rules.json").write_text("{}", encoding="utf-8")
    assert find_rules_file(str(run)) is None
