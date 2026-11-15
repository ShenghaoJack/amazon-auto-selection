# -*- coding: utf-8 -*-
"""属性池 `_catalog/<类目>/属性池.json`（用户口径 2026-10-10）。

用户原话：「不要再复杂化了，可以把这三轮的提取的属性都给模型去判断和聚类啊」

实测依据（同一份数据、同一个提示词，四轮真冷启动）：
    runs=4/4  产品类型 · 尺寸规格 · 形状 · 是否防滑 …
    runs=1/4  **品牌** · 是否套装 · 件数 · 是否易清洁 …
单轮看不出的东西，**多轮攒起来 `runs` 就露出来了** —— 而且判定逻辑**一条没加**：
`runs >= ATTR_SPEC_MIN_RUNS` 的阈值 + 模型同义归并，本来就是现成的。

⚠ 本文件锁两条**向后兼容**铁律：
  ① 池子为空（首次跑）→ `_pool_merge` **原样返回**，行为与加池子之前逐字一致
  ② 市场过滤要真的生效（踩过坑：写入用 `"市场"`、读取用 `MARKET_KEY`(`"_市场"`)
     → `is_visible(None, m)` 恒真 → **过滤静默失效**）
"""
from __future__ import annotations

import json
from pathlib import Path

from steps.step16_attribute_annotation import Step16AttributeAnnotation
from tools.attr_stability.catalog import (
    ATTR_POOL_FILE, append_attr_pool, attr_pool_path, load_attr_pool,
)
from tools.attr_stability.spec import merge_extractions

R1 = [{"name": "产品类型", "hint": "a"}, {"name": "品牌", "hint": "b"}]
R2 = [{"name": "产品类型", "hint": "a2"}, {"name": "材质", "hint": "c"}]


def test_pool_roundtrip_and_runs(tmp_path):
    """两轮并起来 → `runs` 就是**命中轮数**（`产品类型` 2 轮、`品牌` 1 轮）。"""
    append_attr_pool(tmp_path, "Cutting Boards", R1, "US", "2026-10-10T10:00:00")
    append_attr_pool(tmp_path, "Cutting Boards", R2, "US", "2026-10-10T11:00:00")
    rounds = load_attr_pool(tmp_path, "Cutting Boards", "US")
    assert len(rounds) == 2
    got = {it["name"]: int(it.get("runs") or 1)
           for it in merge_extractions([r["属性"] for r in rounds])}
    assert got == {"产品类型": 2, "品牌": 1, "材质": 1}


def test_same_attribute_set_is_not_appended_twice(tmp_path):
    """**同市场 + 同属性集不重复追加** —— 否则重跑同一份数据会把 `runs` 灌高、阈值失真。"""
    for t in ("10:00", "11:00", "12:00"):
        append_attr_pool(tmp_path, "Cutting Boards", R1, "US", "2026-10-10T" + t + ":00")
    assert len(load_attr_pool(tmp_path, "Cutting Boards")) == 1


def test_market_filter_really_works(tmp_path):
    """★ 市场过滤要真生效（踩过坑：键名不一致 → 静默失效，US/DE 都看到全部轮次）。"""
    append_attr_pool(tmp_path, "Cutting Boards", R1, "US", "t1")
    append_attr_pool(tmp_path, "Cutting Boards", R2, "US", "t2")
    append_attr_pool(tmp_path, "Cutting Boards", [{"name": "件数", "hint": "d"}], "DE", "t3")
    assert len(load_attr_pool(tmp_path, "Cutting Boards", "US")) == 2
    assert len(load_attr_pool(tmp_path, "Cutting Boards", "DE")) == 1
    assert len(load_attr_pool(tmp_path, "Cutting Boards")) == 3, "不给市场 → 全取"


def test_pool_survives_corrupt_file(tmp_path):
    """池子文件坏了不该炸 —— 重开一份继续攒。"""
    p = attr_pool_path(tmp_path, "Cutting Boards")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 坏 json", encoding="utf-8")
    assert load_attr_pool(tmp_path, "Cutting Boards") == []
    append_attr_pool(tmp_path, "Cutting Boards", R1, "US", "t1")
    assert len(load_attr_pool(tmp_path, "Cutting Boards")) == 1


def test_keep_caps_history(tmp_path):
    for i in range(5):
        append_attr_pool(tmp_path, "Cutting Boards",
                         [{"name": "属性%d" % i, "hint": "h"}], "US", "t%d" % i, keep=3)
    assert len(load_attr_pool(tmp_path, "Cutting Boards")) == 3


def test_pool_file_shape(tmp_path):
    append_attr_pool(tmp_path, "Cutting Boards", R1, "US", "t1")
    o = json.loads(attr_pool_path(tmp_path, "Cutting Boards").read_text(encoding="utf-8"))
    assert o["轮次"][0]["_市场"] == ["US"], "市场标记按模块约定用 `_市场`"
    assert "不要手工编辑" in o["_说明"]
    assert Path(attr_pool_path(tmp_path, "Cutting Boards")).name == ATTR_POOL_FILE


# ---------------------------------------------------------------- 步骤16 侧
class _Cfg:
    cat = "Cutting Boards-US-202608"

    def get(self, k, default=None):
        return {"ATTR_SPEC_MIN_RUNS": 2}.get(k, default)


class _Paths:
    def __init__(self, r):
        self.data_root = r


class _Ctx:
    def __init__(self, r):
        self.config = _Cfg()
        self.paths = _Paths(r)


def _step() -> Step16AttributeAnnotation:
    st = Step16AttributeAnnotation()
    st._cat = "Cutting Boards-US-202608"

    def _log(_ctx, _msg):
        return None

    st.log = _log                                            # 静音
    return st


def test_pool_merge_empty_pool_is_backward_compatible(tmp_path):
    """★★ **首次跑（池子为空）→ 原样返回** —— 行为与加池子之前逐字一致。

    这条最要紧：否则上线第一天，所有类目的 `runs` 都会变成 1 → 全被判"偶然"。
    """
    st = _step()
    got = st._pool_merge(_Ctx(tmp_path), R1)
    assert got == R1, "池子为空时必须原样返回"
    assert load_attr_pool(tmp_path, "Cutting Boards") == [], "未确认提取不得写共享池"


def test_pool_merge_preserves_current_votes_and_separates_history(tmp_path):
    """旧池不再读取/追加，保留的兼容入口不改变本轮。"""
    append_attr_pool(tmp_path, "Cutting Boards", R1, "US", "t1")
    st = _step()
    got = st._pool_merge(_Ctx(tmp_path), R2)
    by = {it["name"]: it for it in got}
    assert by["产品类型"].get("runs", 1) == 1
    assert "history_runs" not in by["产品类型"], "共享历史不再参与本轮"
    assert "品牌" not in by, "历史独有项不能当成本次投票"
    assert "材质" in by
