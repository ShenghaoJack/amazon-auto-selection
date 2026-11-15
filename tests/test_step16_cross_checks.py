# -*- coding: utf-8 -*-
"""步骤16 产出「跨属性约束」——**行为测试**（不是只查代码里有没有那个字符串）。

用户口径：跨属性约束属于"属性集怎么定"，应当在步骤16 由 LLM 提出并写进 rules.json，
供步骤17 标注时避开、步骤18 校验。此前是我手写的 —— 那不对。

★ 本文件存在的理由：代码审核关卡只能查"`cross_checks` 是否在 step16 源码里出现"，
   **查不出 I/O 契约不匹配**。实测就漏了一个：提示词让 LLM 输出 `{"互斥": [...]}`，
   而 `load_cross_checks` 要 `{"cross_checks": {"互斥": [...]}}` → 少一层包装 →
   解析成空 → LLM 答对了却一条都没落地。所以必须有一个**喂假 LLM、断言落盘**的测试。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step16_attribute_annotation import Step16AttributeAnnotation

CAT = "t"

ATTRS = [
    {"name": "主体材质", "hint": "竹/木/不锈钢"},
    {"name": "产品类型", "hint": "竹制砧板/实木砧板"},
]


class _LLM:
    """假 LLM：按提示词要求返回 `{"互斥": [...]}`（**不带外层包装**）。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, prompt, **kw):
        self.calls += 1
        return self.payload


def _ctx(tmp_path: Path) -> StepContext:
    cfg = Config({"CAT": CAT, "DATA_ROOT": str(tmp_path), "DRY_RUN": True, "MARKET": "amazon.com"})
    ctx = StepContext(cfg, Paths(cfg))
    os.makedirs(ctx.paths.cat_root, exist_ok=True)
    return ctx


def _write_rules(root: Path, extra: dict | None = None) -> Path:
    p = root / CAT / "t.rules.json"
    obj = {"category": "t", "attributes": [{"name": a["name"], "rule": "enum"} for a in ATTRS]}
    obj.update(extra or {})
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


GOOD = {"互斥": [
    {"说明": "竹子不是实木",
     "左": {"属性": "主体材质", "含": ["竹"]},
     "右": {"属性": "产品类型", "不得含": ["实木"]}},
]}


def _force_pass_screen(st):
    """打桩 `_xc_screen`：假装候选都过了数据过检。

    为什么要打桩：真实 `_xc_screen` 需要一份**上一轮交付表**才能过检，
    没有就一律不采纳（这是刻意的保守设计，由
    `test_screen_rejects_when_no_delivery_table` 单独覆盖）。
    本组测试要验的是**写入逻辑**，不该被"没有交付表"挡住。
    """
    st._xc_screen = lambda ctx, items: (list(items), {}, "打桩：全部过检")


def test_writes_declaration_with_outer_wrapper(tmp_path):
    """★ 核心回归：LLM 返回 `{"互斥": …}`（无外层包装）也必须能落盘。"""
    ctx = _ctx(tmp_path)
    rp = _write_rules(tmp_path)
    llm = _LLM(GOOD)
    ctx._llm = llm
    st = Step16AttributeAnnotation()
    st._cat = CAT
    _force_pass_screen(st)
    st._propose_cross_checks(ctx, ATTRS)

    assert llm.calls == 1
    decl = json.loads(rp.read_text(encoding="utf-8"))["cross_checks"]
    assert decl["互斥"] == GOOD["互斥"]
    assert "步骤16" in decl["_来源"]
    # 「人工」在 _说明 里（告诉人改完怎么避免被覆盖），不在 _来源 里 ——
    # 我一开始把断言写在 _来源 上，是**测试写错了**，不是代码错。
    assert "人工" in decl["_说明"]
    assert "过检" in decl["_说明"]          # 说明里要写明"用真实交付表过检"


def test_human_edited_is_not_overwritten(tmp_path):
    """人工改过的（`_来源` 含「人工」）不许被覆盖 —— 这是保护，不是 bug。"""
    ctx = _ctx(tmp_path)
    human = {"_来源": "人工", "互斥": [{"说明": "人手写的", "左": {"属性": "主体材质", "含": ["竹"]},
                                        "右": {"属性": "产品类型", "不得含": ["木"]}}]}
    rp = _write_rules(tmp_path, {"cross_checks": human})
    ctx._llm = _LLM(GOOD)
    st = Step16AttributeAnnotation()
    st._cat = CAT
    st._propose_cross_checks(ctx, ATTRS)
    assert json.loads(rp.read_text(encoding="utf-8"))["cross_checks"] == human


def test_invalid_refs_are_dropped(tmp_path):
    """引用不存在的属性 / 左右同属性 → 丢弃，不写脏数据。"""
    ctx = _ctx(tmp_path)
    rp = _write_rules(tmp_path)
    bad = {"互斥": [
        {"说明": "引用不存在的属性", "左": {"属性": "不存在", "含": ["x"]},
         "右": {"属性": "产品类型", "不得含": ["y"]}},
        {"说明": "左右同一个属性", "左": {"属性": "主体材质", "含": ["竹"]},
         "右": {"属性": "主体材质", "不得含": ["木"]}},
    ]}
    ctx._llm = _LLM(bad)
    st = Step16AttributeAnnotation()
    st._cat = CAT
    st._propose_cross_checks(ctx, ATTRS)
    assert "cross_checks" not in json.loads(rp.read_text(encoding="utf-8"))


def test_mixed_keeps_only_valid(tmp_path):
    ctx = _ctx(tmp_path)
    rp = _write_rules(tmp_path)
    mixed = {"互斥": GOOD["互斥"] + [
        {"说明": "左右同属性", "左": {"属性": "主体材质", "含": ["竹"]},
         "右": {"属性": "主体材质", "不得含": ["木"]}}]}
    ctx._llm = _LLM(mixed)
    st = Step16AttributeAnnotation()
    st._cat = CAT
    _force_pass_screen(st)
    st._propose_cross_checks(ctx, ATTRS)
    decl = json.loads(rp.read_text(encoding="utf-8"))["cross_checks"]
    assert len(decl["互斥"]) == 1 and decl["互斥"][0]["说明"] == "竹子不是实木"


@pytest.mark.parametrize("payload", [{}, {"互斥": []}, {"互斥": "不是数组"}, None])
def test_no_usable_constraints_is_noop(tmp_path, payload):
    ctx = _ctx(tmp_path)
    rp = _write_rules(tmp_path)
    ctx._llm = _LLM(payload)
    st = Step16AttributeAnnotation()
    st._cat = CAT
    st._propose_cross_checks(ctx, ATTRS)
    assert "cross_checks" not in json.loads(rp.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 包装约定（我栽过 3 次）
def test_wrapper_contract_is_pinned():
    """★ 把两个函数的**包装约定**钉死 —— 我在同一个约定上栽过 3 次：

      · `load_cross_checks(rules)`  ｜ 入参 = **整个 rules 对象**
      · `check_rows(rows, decl)`    ｜ 入参 = **load_cross_checks 的输出** {"互斥":[…]}

    搞错方向会**静默解析成空**（少包一层）或 TypeError（多包一层），都不会报"你包错了"。
    """
    from tools.attr_stability.cross_checks import check_rows, load_cross_checks

    rule = {"说明": "竹子不是实木",
            "左": {"属性": "主体材质", "含": ["竹"]},
            "右": {"属性": "产品类型", "不得含": ["实木"]}}
    rules_obj = {"cross_checks": {"互斥": [rule]}}

    # ① load_cross_checks 要整个 rules 对象
    assert load_cross_checks(rules_obj) == {"互斥": [rule]}
    # 少了外层包装 → 空（这就是"LLM 答对了却一条没落地"的那个 bug）
    assert load_cross_checks({"互斥": [rule]}) == {}

    # ② check_rows 要 load_cross_checks 的**输出**（已解包的）
    rows = [{"xl_row": 2, "attributes": {"主体材质": "竹制", "产品类型": "实木砧板"}}]
    assert len(check_rows(rows, {"互斥": [rule]})) == 1
    # 多包一层 → 会把 "互斥" 这个**字符串**当规则遍历 → TypeError（不是静默出错）
    with pytest.raises(TypeError):
        check_rows(rows, rules_obj)


def test_screen_rejects_when_no_delivery_table(tmp_path):
    """首次运行（没有上一轮交付表）→ **全部不自动采纳**，宁可不自动也不写可能错的。"""
    ctx = _ctx(tmp_path)
    rp = _write_rules(tmp_path)
    ctx._llm = _LLM(GOOD)
    st = Step16AttributeAnnotation()
    st._cat = CAT
    st._propose_cross_checks(ctx, ATTRS)
    assert "cross_checks" not in json.loads(rp.read_text(encoding="utf-8"))
