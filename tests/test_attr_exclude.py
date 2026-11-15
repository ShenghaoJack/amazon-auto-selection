# -*- coding: utf-8 -*-
"""「不要清单」`ATTR_EXCLUDE`（2026-10-09 用户口径）。

要解决的问题
===============================================================================
项目里只有 `ATTR_KEYS`（加）和 `ATTR_DESC`（改说明），**唯独没有"删"**。
于是人工在 Web 上删掉一个属性，**只管一轮** —— 下一轮步骤16 会按数据重新抽出来，
没人拦着。实测：`厚度` 被删掉后又回来了（跑后日志里明写 `＋ 新增属性: ['…','厚度','…']`）。

规则（用户确认）
===============================================================================
  ① **落点**：本轮/本市场写运行层 `config.yaml` 的 `ATTR_EXCLUDE`；
     全类目写共享层 `_catalog/<类目>/_catalog.json` 的 `排除属性` → **取并集**
  ② **加入**：**Web 删除 = 自动加入**（"我删了 = 我不要它"）
  ③ **生效**：当轮 spec 立即没有它；**下一轮步骤16 抽到也丢弃**
  ④ **冲突**：同时在 `ATTR_KEYS` 与 `ATTR_EXCLUDE` → **不要优先 + 告警**（不静默选择）
  ⑤ **沉淀保留**：只过滤不删（恢复后词表/判据立刻又能用）
  ⑥ **恢复**：移除条目 = 交回 AI 决定；同时写 `ATTR_KEYS` = 必然加回
  ⑦ **审计**：AI 又提议被排除的属性 → 记「被排除但 AI 又提议」清单
     （反复出现 = 排除可能过时，人能看到这个信号）
"""
from __future__ import annotations

import json
from pathlib import Path


from steps.step16_attribute_annotation import Step16AttributeAnnotation


class _Cfg:
    def __init__(self, d: dict):
        self._d = d
        self.cat = str(d.get("CAT") or "Cutting Boards-US-202608")

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Paths:
    def __init__(self, data_root: Path):
        self.data_root = str(data_root)
        self.cat_root = str(data_root / "Cutting Boards-US-202608")


class _Ctx:
    def __init__(self, cfg: dict, data_root: Path):
        self.config = _Cfg(cfg)
        self.paths = _Paths(data_root)


def _step() -> Step16AttributeAnnotation:
    st = Step16AttributeAnnotation()
    st._cat = "Cutting Boards-US-202608"
    return st


def test_parse_exclude_from_config_string(tmp_path):
    """`ATTR_EXCLUDE` 是 config 里的**字符串**（逗号/分号都能分隔，与 ATTR_KEYS 同风格）。"""
    st = _step()
    for raw, want in (("厚度", ["厚度"]),
                      ("厚度;形状", ["厚度", "形状"]),
                      ("厚度,形状", ["厚度", "形状"]),
                      (" 厚度 ； 形状 ；", ["厚度", "形状"]),
                      ("", [])):
        got = st._parse_attr_exclude(_Ctx({"ATTR_EXCLUDE": raw}, tmp_path))
        assert got == want, raw


def test_parse_exclude_from_config_list(tmp_path):
    """也接受列表形式（含带审计信息的字典）。"""
    st = _step()
    got = st._parse_attr_exclude(_Ctx(
        {"ATTR_EXCLUDE": ["厚度", {"属性": "形状", "时间": "2026-10-09"}]}, tmp_path))
    assert got == ["厚度", "形状"]


def test_parse_exclude_merges_catalog_level(tmp_path):
    """★ 两级取并集：运行层 config + 共享层 `_catalog/<类目>/_catalog.json` 的 `排除属性`。"""
    d = tmp_path / "_catalog" / "Cutting Boards"
    d.mkdir(parents=True)
    (d / "_catalog.json").write_text(json.dumps(
        {"类目": "Cutting Boards", "排除属性": ["形状"]}, ensure_ascii=False), encoding="utf-8")
    st = _step()
    got = st._parse_attr_exclude(_Ctx({"ATTR_EXCLUDE": "厚度"}, tmp_path))
    assert got == ["厚度", "形状"], "全类目级与运行级都要生效"


def test_parse_exclude_missing_is_empty(tmp_path):
    st = _step()
    assert st._parse_attr_exclude(_Ctx({}, tmp_path)) == []


def test_parse_exclude_dedups(tmp_path):
    st = _step()
    got = st._parse_attr_exclude(_Ctx({"ATTR_EXCLUDE": "厚度;厚度; 厚度 "}, tmp_path))
    assert got == ["厚度"]


# ---------------------------------------------------------------- 审计清单
def test_write_attr_excluded_is_append_only(tmp_path):
    """★ 「AI 又提议被排除的属性」要**追加留痕**（不覆盖）。

    为什么：排除生效后**人就看不出 AI 还在提议它**，而"反复提议"恰恰是
    排除可能过时的信号。覆盖写会把历史信号抹掉。
    """
    st = _step()
    ctx = _Ctx({}, tmp_path)
    f = st._write_attr_excluded(ctx, ["厚度"])
    st._write_attr_excluded(ctx, ["形状", "厚度"])
    o = json.loads(Path(f).read_text(encoding="utf-8"))
    assert len(o["记录"]) == 2, "两次都要留痕"
    assert o["记录"][0]["被排除但 AI 又提议"] == ["厚度"]
    assert o["记录"][1]["被排除但 AI 又提议"] == ["形状", "厚度"]
    assert "排除可能过时" in o["_说明"]


def test_write_attr_excluded_survives_corrupt_file(tmp_path):
    """文件坏了不该炸 —— 重开一份继续记。"""
    st = _step()
    ctx = _Ctx({}, tmp_path)
    f = Path(st._write_attr_excluded(ctx, ["厚度"]))
    f.write_text("{ 坏 json", encoding="utf-8")
    st._write_attr_excluded(ctx, ["形状"])                      # 不该抛
    o = json.loads(f.read_text(encoding="utf-8"))
    assert len(o["记录"]) == 1 and o["记录"][0]["被排除但 AI 又提议"] == ["形状"]
