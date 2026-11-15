from tools.attr_stability import AttrSpec, RuleSource
from tools.attr_stability.attr_runner import CallableProducer, fill_missing_sources, run_round


def test_source_is_derived_only_from_unique_original_evidence():
    rows = {
        1: {"attributes": {"材质": "塑料", "材质_依据": "五点描述：food grade plastic", "材质_来源": ""}},
        2: {"attributes": {"材质": "塑料", "材质_依据": "food grade plastic", "材质_来源": ""}},
        3: {"attributes": {"材质": "塑料", "材质_依据": "未提及材质", "材质_来源": ""}},
        4: {"attributes": {"材质": "未明确", "材质_依据": "没有说明", "材质_来源": ""}},
        5: {"attributes": {"材质": "塑料", "材质_依据": "标题：food grade plastic", "材质_来源": ""}},
    }
    sources = {1: {"标题": "food grade plastic", "五点描述": "made of food grade plastic"},
               2: {"标题": "food grade plastic", "五点描述": "food grade plastic"},
               3: {"标题": "unknown"}, 4: {"标题": "unknown"},
               5: {"标题": "bowl", "五点描述": "food grade plastic"}}
    count, review = fill_missing_sources(rows, sources, {"材质"})
    assert count == 2 and rows[1]["attributes"]["材质_来源"] == "五点描述"
    assert rows[5]["attributes"]["材质_来源"] == "五点描述"
    assert rows[2]["attributes"]["材质_来源"] == rows[3]["attributes"]["材质_来源"] == ""
    assert {x["xl_row"] for x in review} == {2, 3}


def test_blank_value_rejudged_and_source_found(tmp_path):
    rules = RuleSource()
    rules.attrs = {"材质": AttrSpec(name="材质", values=["塑料"])}
    src = {"xl_row": 1, "标题": "a bowl", "五点描述": "food grade plastic"}
    calls = []
    def model(prompt):
        calls.append(prompt)
        return {"rows": [{"xl_row": 1, "attributes": {"材质": "塑料", "材质_来源": "", "材质_依据": "food grade plastic"}}]}
    producer = CallableProducer(model, [src], str(tmp_path), "a", "规则", workers=1,
                                input_fields=["xl_row", "标题", "五点描述"])
    bad = {"xl_row": 1, "category_check": {"判定": "属于本类目"},
           "attributes": {"材质": "", "材质_来源": "", "材质_依据": ""}}
    for tag in ("a1", "a2"):
        producer._save_ckpt(producer._raw_path(tag), producer._input_key(), {1: bad})
    res = run_round(producer, rules, n_runs=2, input_rows=[src], require_complete=True,
                    require_basis=True, strict_gate=False, log=lambda _: None)
    assert len(calls) == 2 and res.passed == ["a1", "a2"]
    assert all(g.required_errors == 0 for g in res.gates.values())
    assert res.deliverable[0]["attributes"]["材质"] == "塑料"
    assert res.deliverable[0]["attributes"]["材质_来源"] == "五点描述"


def test_empty_response_from_successful_rejudge_becomes_unknown(tmp_path):
    original = {"xl_row": 1, "标题": "a bowl"}
    blank = {"xl_row": 1, "attributes": {"材质": "", "材质_来源": "", "材质_依据": ""}}
    producer = CallableProducer(lambda _: {"rows": [{"xl_row": 1, "attributes": {}}]},
                                [original], str(tmp_path), "a", "规则", workers=1)
    result = producer.fill_missing({1: blank}, {1: ["材质"]})
    assert result[1]["attributes"]["材质"] == "未明确"
    assert "补跑" in result[1]["attributes"]["材质_依据"]


def test_failed_request_does_not_turn_blank_into_unknown(tmp_path):
    original = {"xl_row": 1, "标题": "a bowl"}
    blank = {"xl_row": 1, "attributes": {"材质": "", "材质_来源": "", "材质_依据": ""}}
    def fail(_):
        raise RuntimeError("network failed")
    producer = CallableProducer(fail, [original], str(tmp_path), "a", "规则", workers=1)
    result = producer.fill_missing({1: blank}, {1: ["材质"]})
    assert result[1]["attributes"]["材质"] == ""


def test_unsupported_proposed_value_is_not_accepted(tmp_path):
    original = {"xl_row": 1, "标题": "a bowl"}
    blank = {"xl_row": 1, "attributes": {"材质": "", "材质_来源": "", "材质_依据": ""}}
    producer = CallableProducer(lambda _: {"rows": [{"xl_row": 1, "attributes": {"材质": "木", "材质_依据": "made of wood"}}]},
                                [original], str(tmp_path), "a", "规则", workers=1)
    result = producer.fill_missing({1: blank}, {1: ["材质"]})
    assert result[1]["attributes"]["材质"] == "未明确"
