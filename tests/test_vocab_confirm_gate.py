# -*- coding: utf-8 -*-
"""方案 A 的「停②」：**标准化词表确认闸门**（用户口径 2026-10-09）。

链路
===============================================================================
    16 提取属性 →[停①]确认属性集 → 17 标注 **+ 产词表** →[停②]确认词表 → 18 应用词表

为什么把词表生成从步骤18 移到步骤17
===============================================================================
步骤18 原先**自己**生成词表（`_generate_vocab`），于是「步骤17 跑完」时
**根本没有新词表可调** —— 用户要的"停下来调词表再继续"落不了地。

为什么要有闸门
===============================================================================
词表是**业务口径**（哪些值算同一个），模型聚出来的只是初稿，必须让人过一眼。
与「属性集规格未确认」那套**同构**（项目已有模式，不新造机制）。
确认方式：Web「标准化词表」面板**保存即确认**（保存这个动作本身就意味着"我审过了"）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from steps.step17_attr_annotation import (
    VOCAB_CONFIRMED,
    VOCAB_PENDING,
    VOCAB_STATUS_KEY,
    Step17AttrAnnotation,
)
from steps.step18_standardization import Step18Standardization


class _Paths:
    def __init__(self, root: Path):
        self.cat_root = str(root)


class _Ctx:
    """够用的假 ctx：`_rules_json_path` 只读 `ctx.paths.cat_root`。"""

    def __init__(self, root: Path):
        self.paths = _Paths(root)

    class _Log:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

    log = _Log()


def _rules_file(tmp: Path, values=("不锈钢砧板", "竹制砧板")) -> Path:
    p = tmp / "t.rules.json"
    p.write_text(json.dumps({
        "category": "t",
        "attributes": [
            {"name": "产品类型", "rule": "enum", "params": {"values": list(values)}},
            {"name": "主体材质", "rule": "enum", "params": {"values": []}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_status_roundtrip(tmp_path):
    """置状态 → 读回来一致，且返回覆盖的属性数。"""
    _rules_file(tmp_path)
    st = Step17AttrAnnotation()
    st._cat = "t"
    ctx = _Ctx(tmp_path)
    assert st._vocab_status(ctx) == "", "初始应无状态"
    n = st._set_vocab_status(ctx, VOCAB_PENDING)
    assert n == 1, "只有 产品类型 有非空 values"
    assert st._vocab_status(ctx) == VOCAB_PENDING
    st._set_vocab_status(ctx, VOCAB_CONFIRMED)
    assert st._vocab_status(ctx) == VOCAB_CONFIRMED


def test_status_write_is_atomic(tmp_path):
    """原子写：不留 `.tmp` 残骸，且已有词表内容不被破坏。"""
    p = _rules_file(tmp_path)
    st = Step17AttrAnnotation()
    st._cat = "t"
    st._set_vocab_status(_Ctx(tmp_path), VOCAB_PENDING)
    assert not (tmp_path / "t.rules.json.tmp").exists(), "不该留 .tmp"
    after = json.loads(p.read_text(encoding="utf-8"))
    pt = next(a for a in after["attributes"] if a["name"] == "产品类型")
    assert pt["params"]["values"] == ["不锈钢砧板", "竹制砧板"], "词表内容不许被动"


def test_status_key_is_declared():
    assert VOCAB_STATUS_KEY == "vocab_status"
    assert VOCAB_PENDING == "pending_confirm" and VOCAB_CONFIRMED == "confirmed"


def test_confirmation_scope_is_all_attributes(tmp_path):
    """★★ 确认范围 = **全部属性**（用户口径 2026-10-09）。

    为什么这条要单独锁：它是"**一个全局标记**"而不是逐属性确认 ——
    所以"我审过了"覆盖**整份词表**，包括本次没动过的属性。
    如果有人后来把它改成逐属性（`attributes[i].vocab_status`），这条会红。

    判据：
      ① `vocab_status` 是 `rules.json` 的**顶层键**（不在 `attributes` 里）；
      ② 置状态返回的计数 = **所有**有非空 `values` 的属性数（不是"变动过的"）；
      ③ 置状态**不改动**任何属性的词表内容。
    """
    p = _rules_file(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    # 再造一个有值的属性，验证计数覆盖"全部"而不是某一个
    data["attributes"].append({"name": "形状", "rule": "enum",
                               "params": {"values": ["长方形"]}})
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    st = Step17AttrAnnotation()
    st._cat = "t"
    ctx = _Ctx(tmp_path)
    n = st._set_vocab_status(ctx, VOCAB_CONFIRMED)
    assert n == 2, "有值的属性是 产品类型 + 形状（主体材质 为空）→ 计数要覆盖全部"

    after = json.loads(p.read_text(encoding="utf-8-sig"))
    assert after[VOCAB_STATUS_KEY] == VOCAB_CONFIRMED, "必须是顶层全局键"
    for a in after["attributes"]:
        assert VOCAB_STATUS_KEY not in a, "不该变成逐属性状态"
    # 内容一字未动
    assert next(a for a in after["attributes"]
                if a["name"] == "形状")["params"]["values"] == ["长方形"]



def test_step18_refuses_when_vocab_unconfirmed(tmp_path, monkeypatch):
    """★★ 核心：词表未确认时步骤18 **必须拒绝**，且给出可操作提示。

    抛的是 `WaitingForHuman`（不是 `StepDataError`）—— 编排器据此**不标 FAILED、
    不发 CRIT 告警、退出码用 3**：这是在等人，不是在报错。
    """
    from core.errors import WaitingForHuman
    st = Step18Standardization()
    # 直接测闸门那段：伪造 resolve_artifact 让前置检查先过
    d17 = tmp_path / "17_属性标注"
    d17.mkdir()
    (d17 / "稳定性结果.json").write_text("{}", encoding="utf-8")
    (d17 / "input.json").write_text("{}", encoding="utf-8")
    (tmp_path / "t-attribute-spec.json").write_text(
        json.dumps({"attributes": [{"name": "产品类型"}]}), encoding="utf-8")
    _rules_file(tmp_path)                       # 有 rules.json，但没有 vocab_status

    class _Ctx2(_Ctx):
        def resolve_artifact(self, rel):
            return str(tmp_path / rel)

        # `validate_upstream` 只用到 config.cat 与 paths
        class _Cfg:
            cat = "t"

        config = _Cfg()

    with pytest.raises(WaitingForHuman) as ei:
        st.validate_upstream(_Ctx2(tmp_path))
    msg = str(ei.value)
    assert "标准化词表未确认" in msg
    assert "标准化词表" in msg and "保存" in msg, "提示必须告诉用户去哪、怎么做"
    assert ei.value.hint, "必须带可照做的下一步（hint）"


def test_step18_passes_gate_when_confirmed(tmp_path):
    """词表确认且输入、属性与标注版本一致时放行。"""
    from tools.attr_stability.spec_io import make_annotation_contract
    st = Step18Standardization()
    st._cat = "t"
    d17 = tmp_path / "17_属性标注"
    d17.mkdir()
    rows = [{"xl_row": 2, "标题": "demo"}]
    (d17 / "input.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    definition = {"status": "confirmed", "definition_revision": "test-revision",
                  "attributes": [{"name": "产品类型"}, {"name": "主体材质"}]}
    (tmp_path / "t-attribute-spec.json").write_text(
        json.dumps(definition), encoding="utf-8")
    source = tmp_path / "14_流量拆分" / "流量拆分数据-需人工补充.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"source for version check")
    contract = make_annotation_contract(definition, rows, source_path=source)
    contract["completed"] = True
    (d17 / "稳定性结果.json").write_text(json.dumps({
        "annotation_contract": contract,
        "deliverable": [{"xl_row": 2, "attributes": {"产品类型": "demo", "产品类型_来源": "标题",
                                                    "主体材质": "木制", "主体材质_来源": "标题"}}],
    }), encoding="utf-8")
    _rules_file(tmp_path)

    class _Ctx2(_Ctx):
        def resolve_artifact(self, rel):
            return str(tmp_path / rel)

        class _Cfg:
            cat = "t"

        config = _Cfg()

    ctx = _Ctx2(tmp_path)
    Step17AttrAnnotation()._set_vocab_status(ctx, VOCAB_CONFIRMED)
    st.validate_upstream(ctx)                   # 不抛 = 放行
    assert st._spec_attributes, "顺带应把 spec 属性读进来"
