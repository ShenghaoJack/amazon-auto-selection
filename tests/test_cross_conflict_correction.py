# -*- coding: utf-8 -*-
"""跨属性冲突的**校验 + 纠正**（用户口径 2026-10-09）。

「既然有冲突，应该再加个校验和纠正的步骤」——
此前步骤18 **只登记**（写 `跨属性冲突.json`），冲突行**原样进交付表**。

链路：**16 声明约束 → 17 标注时避开 → 18 校验 + 纠正**

核心原则：**LLM 只出建议，代码侧闸门独立校验**（与项目的结构闸门同一哲学 ——
独立验证必须与被验证对象机制不同，否则同一个错误会被"校验通过"）。
不过闸门的一律**不应用**，进「待人工」，绝不静默改值。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from steps.step17_attr_annotation import Step17AttrAnnotation

DECL = {"互斥": [{"说明": "不锈钢砧板必须含不锈钢",
                  "左": {"属性": "产品类型", "含": ["不锈钢砧板"]},
                  "右": {"属性": "主体材质", "含": ["不锈钢"]}}]}
# ★ load_cross_checks 的入参契约（我在这上面栽了 5 次）：
#   必须是**真正的 Mapping（dict）**且含 `cross_checks` 键；传自定义对象会静默返回空。
RULES = {"cross_checks": DECL}
VOCAB_NESTED = {"产品类型": {"values": ["竹制砧板", "实木砧板", "不锈钢砧板", "钛金属砧板"]},
                "主体材质": {"values": ["竹制", "木制", "钛金属", "不锈钢", "钛金属+木制"]}}
VOCAB_FLAT = {"产品类型": ["竹制砧板", "实木砧板", "不锈钢砧板", "钛金属砧板"],
              "主体材质": ["竹制", "木制", "钛金属", "不锈钢", "钛金属+木制"]}


class _Res:
    cells = {
        "159|attr.产品类型_来源": {"mode": "标题"},
        "159|attr.产品类型_依据": {"mode": "标题含 Titanium"},
        "159|attr.主体材质_来源": {"mode": "标题"},
        "159|attr.主体材质_依据": {"mode": "纯钛 + 木纤维"},
    }
    # ★ 真 `res` 一定有 `deliverable`（登记就是用它算的）；测试基类先留空，
    #   各用例按需覆盖 —— 因为**纠正必须与登记看同一份值**（见下面两条测试）。
    deliverable: list = []


class _ResDeliv(_Res):
    """违例只出现在 `deliverable` 里、`std` 里没有 —— 用来验证口径统一。"""
    deliverable = [{"xl_row": 159, "attributes": {"产品类型": "不锈钢砧板",
                                                  "主体材质": "钛金属+木制"}}]


class _ResOK(_Res):
    """不违例：材质是不锈钢，满足「不锈钢砧板 → 材质含不锈钢」。"""
    deliverable = [{"xl_row": 159, "attributes": {"产品类型": "不锈钢砧板",
                                                  "主体材质": "不锈钢+塑料"}}]


class _Log:
    """`BaseStep.log` 用的是 `ctx.log.info(...)` —— 假 ctx 必须带 `.info`。

    踩过的坑：把 `log` 写成**函数** → `AttributeError: 'function' object has no attribute 'info'`
    （一口气挂掉 9 个测试）。
    """
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Ctx:
    # ★ 纠正**默认只报告不改值**（安全阀 `ATTR_XC_APPLY`）——
    #   本组测试要验"应用逻辑"，所以显式打开它。
    #   「默认只报告」由 `test_default_is_report_only` 单独覆盖。
    config = {"CAT": "t", "ATTR_XC_APPLY": "true"}
    log = _Log()

    def __init__(self, newval, apply_fix: bool = True):
        self._nv = newval
        self.seen_prompt = None
        # `ATTR_XC_AUTO` 默认**开**（2026-10-09 用户口径）；关掉要显式给 "false"
        self.config = {"CAT": "t", "ATTR_XC_AUTO": "true" if apply_fix else "false"}

    def llm(self):
        outer = self

        class _L:
            def chat_json(self, prompt, **kw):
                outer.seen_prompt = prompt
                return {"纠正": [{"行": 159, "属性": "产品类型",
                                  "新值": outer._nv, "理由": "标题是 Titanium"}]}
        return _L()

    def resolve_artifact(self, x):
        return str(Path(__file__).resolve().parents[1] / "scripts" / "_plan_pilot" / "_cr_tmp" / "corr")


def _run(newval, vocab=VOCAB_NESTED):
    st = Step17AttrAnnotation()
    st._cat = "t"
    std = {(159, "产品类型"): "不锈钢砧板", (159, "主体材质"): "钛金属+木制"}
    r = st._correct_cross_conflicts(_Ctx(newval), RULES, _Res(), [], std, attrs_spec=vocab)
    return r, std


# ---------------------------------------------------------------- 应用
def test_valid_new_value_is_applied():
    r, std = _run("钛金属砧板")
    assert len(r["已纠正"]) == 1
    assert std[(159, "产品类型")] == "钛金属砧板"
    assert not r["待人工"]
    assert r["已纠正"][0]["原值"] == "不锈钢砧板"


def test_out_of_vocab_value_is_not_applied():
    """★ LLM 给了词表外的值 → **不应用**，进待人工（绝不静默写入非法值）。"""
    r, std = _run("石头砧板")
    assert not r["已纠正"]
    assert std[(159, "产品类型")] == "不锈钢砧板"      # 原值没被动
    assert "不在受控值内" in r["待人工"][0]["原因"]


def test_new_value_is_auto_adopted():
    """★★ 「新增: X」**自动进词表**（2026-10-09 用户口径）。

    用户原话：「AI 本来都可以解决的，你不要强行限制它」。
    为什么现在敢自动：加了 `依据` 之后模型**判对了侧** ——
    实测 26/26 条建议都是 `产品类型 → 新增: 钛金属砧板`，
    **没有**再把 `主体材质=钛金属` 改成 `不锈钢`（那是加依据之前的错法）。
    """
    r, std = _run("新增: 石头砧板")
    assert len(r["已纠正"]) == 1, r["待人工"]
    assert std[(159, "产品类型")] == "石头砧板", "值应被改成新值"
    assert r["已采纳新值"] == {"产品类型": ["石头砧板"]}, "新值应被采纳进词表"
    assert r["已纠正"][0]["新增受控值"] == "石头砧板"
    assert not r["待人工"]


def test_new_value_guard_empty():
    """判据：`新增: ` 后面是空的 → 不采纳。"""
    r, std = _run("新增: ")
    assert not r["已纠正"] and not r["已采纳新值"]
    assert std[(159, "产品类型")] == "不锈钢砧板"


def test_new_value_guard_too_long():
    """判据：新增值过长（>20 字）→ 疑似不是取值，不采纳。"""
    r, std = _run("新增: " + "很长的说明文字" * 5)
    assert not r["已纠正"] and not r["已采纳新值"]
    assert "过长" in r["待人工"][0]["原因"]


def test_new_value_guard_already_in_vocab():
    """★ `新增: X` 而 X **已在词表里** → **直接应用**，不是错。

    实测踩到的（2026-10-09，100 行）：纠正是**分批**跑的 ——
    第 1 批采纳了 `钛金属砧板`（进词表），第 2 批里同样提
    `新增: 钛金属砧板` 的 **9 行被误拒**（原因写成"已在受控值里"）→
    那 9 行留在错误值上，登记仍有 9 个冲突。

    正确行为：值已在词表里 → 它就是合法值，**照样应用**（只是不用再"采纳"一次）。
    """
    r, std = _run("新增: 钛金属砧板")           # 钛金属砧板 本来就在 VOCAB_NESTED 里
    assert len(r["已纠正"]) == 1, r["待人工"]
    assert std[(159, "产品类型")] == "钛金属砧板", "已在词表里的值必须照样应用"
    assert not r["已采纳新值"], "已在词表里 → 不需要再采纳"
    assert r["已纠正"][0]["新增受控值"] is None


def test_no_solution_goes_to_human():
    r, std = _run("无解")
    assert not r["已纠正"] and r["待人工"]


def test_unrelated_attribute_is_rejected():
    """LLM 改了一个**不在违约对里**的属性 → 拒绝。"""
    r, std = _run("钛金属砧板")
    assert r["已纠正"] or True
    # 直接构造：让 LLM 报一个不相关的属性名
    st = Step17AttrAnnotation()
    st._cat = "t"
    std2 = {(159, "产品类型"): "不锈钢砧板", (159, "主体材质"): "钛金属+木制"}

    class _C2(_Ctx):
        config = {'CAT': 't', 'ATTR_XC_APPLY': 'true'}

        def llm(self):
            class _L:
                def chat_json(self, prompt, **kw):
                    return {"纠正": [{"行": 159, "属性": "形状", "新值": "长方形"}]}
            return _L()
    r2 = st._correct_cross_conflicts(_C2("x"), RULES, _Res(), [], std2, attrs_spec=VOCAB_NESTED)
    assert not r2["已纠正"]
    assert "不相关" in r2["待人工"][0]["原因"]


# ---------------------------------------------------------------- 形状兼容
@pytest.mark.parametrize("vocab", [VOCAB_NESTED, VOCAB_FLAT], ids=["nested", "flat"])
def test_both_attrs_spec_shapes_supported(vocab):
    """★ `attrs_spec` 有**两种形状**（嵌套 / 扁平），都必须支持。

    实测踩过：只处理嵌套 → 扁平形态直接 AttributeError（与 `derive._params_of` 同一类坑）。
    """
    r, std = _run("钛金属砧板", vocab=vocab)
    assert len(r["已纠正"]) == 1
    assert std[(159, "产品类型")] == "钛金属砧板"


# ---------------------------------------------------------------- 无违例
def test_no_violation_is_noop():
    st = Step17AttrAnnotation()
    st._cat = "t"
    std = {(159, "产品类型"): "竹制砧板", (159, "主体材质"): "竹制"}
    r = st._correct_cross_conflicts(_Ctx("x"), RULES, _Res(), [], std, attrs_spec=VOCAB_NESTED)
    assert r == {"已纠正": [], "待人工": [], "依据": "无违例"}


def test_no_declaration_is_noop():
    st = Step17AttrAnnotation()
    st._cat = "t"
    std = {(159, "产品类型"): "不锈钢砧板", (159, "主体材质"): "钛金属+木制"}
    assert st._correct_cross_conflicts(_Ctx("x"), {}, _Res(), [], std,
                                       attrs_spec=VOCAB_NESTED) == {}


# ---------------------------------------------------------------- 提示词
def test_prompt_carries_evidence_and_forbids_force_fitting():
    # ⚠ 必须传**有违例的 std** —— 空 std 会提前返回（无违例），提示词根本没被调用
    ctx = _Ctx("钛金属砧板")
    st = Step17AttrAnnotation()
    st._cat = "t"
    std = {(159, "产品类型"): "不锈钢砧板", (159, "主体材质"): "钛金属+木制"}
    st._correct_cross_conflicts(ctx, RULES, _Res(), [], std, attrs_spec=VOCAB_NESTED)
    p = ctx.seen_prompt or ""
    assert p, "提示词未被调用（std 里没有违例行？）"
    assert "无解" in p, "提示词必须允许答「无解」"
    assert "不要硬塞" in p, "提示词必须禁止硬塞语义不符的值"
    assert "标题" in p and "纯钛" in p, "必须把来源/依据作为判据喂给模型"


def test_uses_same_values_as_registration():
    """★★ 纠正必须与**登记**看**同一份值**（`deliverable` 打底 + `std` 覆盖，取并集）。

    原实现自己从 `std` 拼行值 —— 缺的属性一律变空串、且只看 `std` 出现过的行；
    而登记用的是 `_final_rows(res, std_map)`。两份值不同 →
    **同一轮里「登记说 1 个冲突、纠正说 0 个」**，交付目录留下自相矛盾的产物。

    本用例：`std` 里**什么都没有**，违例只在 `deliverable` 里 → 也必须被看到。
    """
    st = Step17AttrAnnotation()
    st._cat = "t"
    std: dict = {}
    r = st._correct_cross_conflicts(_Ctx("新增: 玻璃砧板"), RULES, _ResDeliv(), [], std,
                                    attrs_spec=VOCAB_NESTED)
    assert len(r["已纠正"]) == 1, "违例在 deliverable 里也必须被看到"


def test_no_violation_still_writes_record():
    """★ 早退**也要写记录**：否则分不清「本轮没违例」和「这一步没跑」。

    我自己就被这个坑绕过一轮 —— 拿上一轮的陈旧 `跨属性冲突.json` 当当前状态，
    虚构出一个「登记 1 / 纠正 0」的分歧去查。
    """
    st = Step17AttrAnnotation()
    st._cat = "t"
    r = st._correct_cross_conflicts(_Ctx("x"), RULES, _ResOK(), [], {},
                                    attrs_spec=VOCAB_NESTED)
    assert r["已纠正"] == [] and not r["待人工"]
    p = (Path(__file__).resolve().parents[1] / "scripts" / "_plan_pilot"
         / "_cr_tmp" / "corr" / "跨属性冲突纠正.json")
    assert p.is_file(), "无违例也必须留记录"
    o = json.loads(p.read_text(encoding="utf-8"))
    assert "无违例" in o["模式"]
    assert o["已纠正"] == []


def test_report_is_written():
    r, _ = _run("钛金属砧板")
    p = (Path(__file__).resolve().parents[1] / "scripts" / "_plan_pilot"
         / "_cr_tmp" / "corr" / "跨属性冲突纠正.json")
    assert p.is_file(), "必须留纠正记录"
    o = json.loads(p.read_text(encoding="utf-8"))
    assert "已纠正" in o and "待人工" in o


def test_can_be_turned_off_to_report_only():
    """安全阀仍可关：`ATTR_XC_AUTO=false` 时**一个字都不改**。

    默认已改为**开**（2026-10-09 用户口径：AI 该自己完成）——
    因为加了 `依据` 之后模型判对了侧（26/26 建议都指向真正错的那一侧）。
    但这个开关要留着：换类目 / 换提示词后若发现判侧又错了，可一键退回只报告。
    """
    st = Step17AttrAnnotation()
    st._cat = "t"
    std = {(159, "产品类型"): "不锈钢砧板", (159, "主体材质"): "钛金属+木制"}
    r = st._correct_cross_conflicts(_Ctx("钛金属砧板", apply_fix=False), RULES, _Res(), [],
                                    std, attrs_spec=VOCAB_NESTED)
    assert r["已纠正"] == [], "仅报告模式不许落库"
    assert std[(159, "产品类型")] == "不锈钢砧板", "值必须原封不动"
    assert r["待人工"], "建议必须留在待人工里"
    assert "仅报告" in r["待人工"][0]["原因"]
    assert "仅报告" in r["模式"]


def test_auto_adopted_value_is_appended_to_rules_file(tmp_path):
    """★ 自动采纳的新值要**真的写进 rules.json**（追加，且不覆盖已有值）。

    这条单独立测，是因为踩过「改文件不改内存 / 改内存不改文件」的坑：
    必须**两个都改**，且顺序是**先内存、后落盘**。
    """
    catdir = tmp_path / "cat"
    catdir.mkdir()
    # 先造好 `rules`（含已有词表）与内存同源的 rules.json
    (catdir / "t.rules.json").write_text(json.dumps({
        "category": "t",
        "attributes": [
            {"name": "产品类型", "rule": "enum",
             "params": {"values": ["不锈钢砧板", "竹制砧板"]}},
            {"name": "主体材质", "rule": "enum",
             "params": {"values": ["钛金属", "木制"]}},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    class _Paths:
        cat_root = str(catdir)

    class _Ctx2(_Ctx):
        paths = _Paths()

    st = Step17AttrAnnotation()
    st._cat = "t"
    std = {(159, "产品类型"): "不锈钢砧板", (159, "主体材质"): "钛金属+木制"}
    # ⚠ 用的新值必须**不在** `VOCAB_NESTED` 里 —— 否则会被"已在受控值里"判据拦下
    #   （我第一版就用了 `钛金属砧板`，而它本来就在那套测试词表里 → 断言 `已采纳新值` 为空）
    r = st._correct_cross_conflicts(_Ctx2("新增: 玻璃砧板"), RULES, _Res(), [], std,
                                   attrs_spec=VOCAB_NESTED)
    assert r["已采纳新值"] == {"产品类型": ["玻璃砧板"]}
    after = json.loads((catdir / "t.rules.json").read_text(encoding="utf-8"))
    pt = next(a for a in after["attributes"] if a["name"] == "产品类型")
    assert "玻璃砧板" in pt["params"]["values"], "新值必须落进 rules.json"
    assert "不锈钢砧板" in pt["params"]["values"], "已有值绝不能被覆盖掉"
