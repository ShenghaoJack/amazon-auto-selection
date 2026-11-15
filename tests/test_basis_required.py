# -*- coding: utf-8 -*-
"""`_依据`（原文依据）—— 每个属性都必须给（用户口径 2026-10-09）。

背景（这是"越跑越复杂"的根因之一）
===============================================================================
原设计里 `来源` 与 `依据` **被当成同一层的东西**，实际上：

  · `<属性>_来源` = **标签**（标题/五点描述/详细参数/图片OCR/图片视觉/SKU）
  · `<属性>_依据` = **原文**（"标题：Pure Titanium & Wood Fiber"）

而 `依据` **只在「尺寸」那一路被要求**。实测当时 18 个属性里
**只有 1 个（尺寸）有 `_依据`**。

后果：跨属性冲突纠正要判「两个属性矛盾时**该改哪一侧**」，但它手里只有标签 ——
冲突两侧常常**标签相同**（都是「标题」）→ 无从判别 → 模型只能挑一边 →
实测把 `主体材质=钛金属+木制`（依据就是纯钛+木纤维）改成了 `不锈钢`，
**把正确数据改错**。

本文件锁三件事：
  ① `cell_kind` 把 `_依据` 归 **free**（原文引哪句逐次不同，不能算进一致率）；
  ② 缺 `_依据` / `_依据` 为空串 → 闸门 **REJECT**（漏整列是唯一必须硬拦的方向）；
  ③ 可用 `ATTR_STAB_REQUIRE_BASIS=false` 关掉（兼容旧产物）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.attr_stability import attr_stability as A


# --------------------------------------------------------------- ① cell_kind
@pytest.mark.parametrize("path,kind", [
    ("attr.主体材质_依据", "free"),
    ("attr.主体材质_来源", "free"),
    ("attr.主体材质", "value"),
    ("尺寸_依据", "free"),
])
def test_basis_is_free_text(path, kind):
    assert A.cell_kind(path) == kind


# --------------------------------------------------------------- 闸门
def _rules(tmp: Path):
    d = tmp / "cat"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t.rules.json").write_text(json.dumps({
        "category": "t",
        "top_keys": sorted(A.DEFAULT_TOP_KEYS),
        "allow_keys": sorted(A.DEFAULT_ALLOW_KEYS),
        "attributes": [
            {"name": "主体材质", "rule": "enum", "params": {"values": []}},
            {"name": "产品类型", "rule": "enum", "params": {"values": []}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    from tools.attr_stability import RuleSource
    return RuleSource.load(str(d), "t")


def _row(with_basis=True, empty_basis=False):
    a = {"主体材质": "钛金属+木制", "主体材质_来源": "标题",
         "产品类型": "不锈钢砧板", "产品类型_来源": "标题"}
    if with_basis:
        a["主体材质_依据"] = "标题：Pure Titanium & Wood Fiber"
        a["产品类型_依据"] = "" if empty_basis else "标题：Titanium Cutting Board"
    # ⚠ 必填行级键要齐，否则 `pack_count_数值` 之类的缺键也会 REJECT，
    #   就分不出是不是「依据」这条在拦（我第一版误判过）。
    return {"xl_row": 1, "category_check": {"判定": "属于本类目", "信号": []},
            "attributes": a, "尺寸_拆列": {"长": "", "宽": "", "高": ""},
            "尺寸_依据": "无", "suitable_scene_规范串": "", "词表外词": [],
            "pack_count_数值": 1, "跨列纠错": [], "冲突": []}


def _issues(row, tmp):
    g = A.gate_run([row], _rules(tmp), tag="t")
    return getattr(g, "verdict", "?"), [str(i.why) for i in (g.issues or [])]


def test_complete_row_passes(tmp_path):
    A.set_require_basis(True)
    verdict, issues = _issues(_row(), tmp_path)
    assert verdict == "PASS", issues
    assert not issues


def test_missing_basis_is_rejected(tmp_path):
    A.set_require_basis(True)
    verdict, issues = _issues(_row(with_basis=False), tmp_path)
    assert verdict == "REJECT"
    assert any("漏依据键" in w for w in issues), issues


def test_empty_basis_is_rejected(tmp_path):
    """空串等于没写 —— 不能靠填个空字符串蒙过闸门。"""
    A.set_require_basis(True)
    verdict, issues = _issues(_row(empty_basis=True), tmp_path)
    assert verdict == "REJECT"
    assert any("依据键为空" in w for w in issues), issues


def test_can_be_disabled_for_legacy(tmp_path):
    """`ATTR_STAB_REQUIRE_BASIS=false` 时可关（兼容旧产物）。"""
    A.set_require_basis(False)
    try:
        verdict, _ = _issues(_row(with_basis=False), tmp_path)
        assert verdict == "PASS"
    finally:
        A.set_require_basis(True)


# --------------------------------------------------------------- 提示词
def test_prompt_requires_basis_for_every_attribute():
    """提示词必须有 §3.3e 那节，且明确「每个属性都要给」。"""
    p = (Path(__file__).resolve().parents[1] / "prompts" / "23_llm_only_pipeline.md")
    txt = p.read_text(encoding="utf-8")
    assert "### 3.3e" in txt
    assert "每个属性都要给" in txt
    assert "不许只写标签" in txt
    # 输出 schema 示例里也要带上依据键
    assert "产品类型_依据" in txt and "容量_依据" in txt
    # 自检清单里要问
    assert "每个属性都有 `<属性名>_依据`" in txt
