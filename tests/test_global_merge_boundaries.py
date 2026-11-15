"""Offline regression checks for full-definition, boundary-aware merge requests."""
import copy

import pytest

from tools.attr_stability import global_merge as gm


class Model:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def chat_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return copy.deepcopy(self.answers.pop(0))


@pytest.mark.parametrize("mode", ["typical", "hybrid", "full"])
@pytest.mark.parametrize("typical", ["是/否/未明确", "1个/2个", "3℃/5℃"])
def test_all_modes_keep_tail_boundaries_for_every_value_type(mode, typical):
    hint = "判定证据" * 60 + f"；典型取值：{typical}；仅统计主体，不含附件；温度不等于档位数。"
    model = Model({"mapping": {"测试属性": "测试属性", "附件属性": "附件属性"}})
    gm.global_merge(model, ["测试属性", "附件属性"], {"测试属性": hint}, hint_mode=mode)
    assert hint in model.prompts[0]
    assert "仅统计主体，不含附件" in model.prompts[0]


@pytest.mark.parametrize("merge_fn", [gm.global_merge, gm.global_merge_cot,
                                      gm.global_merge_vote, gm.global_merge_twostage])
def test_all_strategies_receive_boundary_rules_and_complete_definitions(merge_fn):
    hints = {"食碗数量": "仅数独立食碗，典型取值：1个/2个；不含托盘分格。",
             "出粮口数量": "仅数实际出口，典型取值：1个/2个；双仓不证明双出口。"}
    identity = {"mapping": {name: name for name in hints}}
    model = Model(identity, identity, identity)
    assert merge_fn(model, list(hints), hints=hints) == identity["mapping"]
    for prompt in model.prompts:
        assert all(hint in prompt for hint in hints.values())
        assert "依据重叠不等于同义" in prompt
        assert "实际温度与温度档数" in prompt
        assert "支持某配件与随附该配件" in prompt
        assert "不在名称同义mapping中表达" in prompt
        assert "同一判定词可以证明多个独立事实" in prompt
        assert "即使名字写法不同也视为同义" not in prompt


def test_twostage_keeps_both_sources_when_canonical_name_is_new():
    hints = {"碗材质": "只看食碗，不看机身；典型取值：不锈钢/陶瓷。",
             "食盆材料": "只看独立盆体；附属支架材质不计。", "机身材质": "仅看外壳。"}
    first = {"碗材质": "食碗材质", "食盆材料": "食碗材质", "机身材质": "机身材质"}
    second = {"食碗材质": "食碗材质", "机身材质": "机身材质"}
    model = Model({"mapping": first}, {"mapping": second})
    assert gm.global_merge_twostage(model, list(hints), hints) == first
    assert all(h in model.prompts[1] for h in hints.values())
    assert "【来源属性：碗材质】" in model.prompts[1]
    assert "【来源属性：食盆材料】" in model.prompts[1]


def test_convergence_rebuilds_definitions_from_originals_every_round():
    hints = {"A": "定义A的部件边界", "B": "定义B的适用条件", "C": "定义C的否定证据", "D": "定义D的单位"}
    calls = []
    answers = [{"A": "X", "B": "X", "C": "C", "D": "D"},
               {"X": "Y", "C": "Y", "D": "D"}, {"Y": "Y", "D": "D"}]

    def merge(llm, names, *, hints, hint_mode):
        calls.append((names, copy.deepcopy(hints)))
        return answers.pop(0)

    result, counts = gm.converge_merge(None, list(hints), hints, merge_fn=merge)
    assert result == {"A": "Y", "B": "Y", "C": "Y", "D": "D"}
    assert counts == [4, 3, 2, 2]
    assert hints == calls[0][1]
    assert set(calls[1][1]) == {"X", "C", "D"}
    assert set(calls[2][1]) == {"Y", "D"}
    for original in ("A", "B", "C"):
        assert calls[2][1]["Y"].count(hints[original]) == 1
        assert calls[2][1]["Y"].count(f"【来源属性：{original}】") == 1
    assert "【来源属性：X】" not in calls[2][1]["Y"]


def test_combining_definitions_deduplicates_without_discarding_shorter_boundaries():
    assert gm.combine_merge_hints("较长的原始说明", "不含附件", "较长的原始说明", "", None) == "较长的原始说明\n\n不含附件"
    assert gm.aggregate_merge_hints({"A": "X", "B": "B"}, {"A": "完整定义"}) == {
        "X": "【来源属性：A】\n完整定义", "B": ""}


def test_empty_and_missing_definitions_still_support_name_only_callers():
    assert gm.global_merge(Model(), []) == {}
    identity = {"A": "A", "B": "B"}
    assert gm.global_merge(Model({"mapping": identity}), list(identity)) == identity
    assert gm._build_body(["A", "B"], None) == "A\nB"


def test_price_band_path_passes_all_extraction_definitions_to_convergence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pandas as pd
    from core.config import Config
    from core.paths import Paths
    from steps.base import StepContext
    from steps.step16_attribute_annotation import Step16AttributeAnnotation

    cfg = Config({"CAT": "Merge-QA", "DATA_ROOT": str(tmp_path), "DRY_RUN": True,
                  "ATTR_SPEC_RUNS": 3})
    ctx = StepContext(cfg, Paths(cfg))
    ctx._llm = SimpleNamespace(config=cfg)
    step = Step16AttributeAnnotation()
    step.bind_cat(ctx)
    monkeypatch.setattr("steps.step16_attribute_annotation.choose_price_bands",
                        lambda prices: {"bands": [{"lo": 0, "hi": 100}]})
    draws = []
    def extract(*args, **kwargs):
        draws.append(len(draws) + 1)
        return ["甲", "乙"], {name: f"{name}(第{len(draws)}轮的完整排除边界)" for name in ("甲", "乙")}
    monkeypatch.setattr(step, "_extract_attrs_retry", extract)
    def band_merge(llm, names, *, hints, **kwargs):
        assert all(f"第{i}轮的完整排除边界" in hints["甲"] for i in (2, 3, 4))
        return {name: name for name in names}
    monkeypatch.setattr(gm, "global_merge", band_merge)
    class StopAfterCapture(Exception):
        pass
    def capture(llm, names, *, hints, **kwargs):
        assert names == ["乙", "甲"]
        for name in names:
            assert all(f"第{i}轮的完整排除边界" in hints[name] for i in (2, 3, 4))
            assert "第1轮" not in hints[name]  # probe is not a formal extraction
        raise StopAfterCapture
    monkeypatch.setattr(gm, "converge_merge", capture)
    with pytest.raises(StopAfterCapture):
        step._extract_spec_attrs_by_price_band(ctx, pd.DataFrame({"价格": [10, 20], "标题": ["a", "b"]}))
