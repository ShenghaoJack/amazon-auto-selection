from tools.attr_stability.attr_runner import CallableProducer


def producer(tmp_path, fn):
    row = {"xl_row": 2, "标题": "stainless bowl", "五点描述": "dishwasher safe",
           "详细参数": "steel", "SKU": "Blue", "图片链接": "https://example.test/secret.jpg"}
    return CallableProducer(fn, [row], str(tmp_path), "a", "完整标注规则",
                            workers=1, input_fields=["xl_row", "标题", "五点描述", "详细参数", "SKU"],
                            compact_attributes=["主体材质", "是否可洗碗机清洗"])


def test_compact_output_roundtrip_keeps_evidence_and_omits_image_url(tmp_path):
    def llm(prompt):
        assert "https://example.test/secret.jpg" not in prompt
        assert '"p1":"主体材质"' in prompt and '"p2":"是否可洗碗机清洗"' in prompt
        assert "dishwasher safe" in prompt and '"SKU":"Blue"' in prompt
        return {"rows": [{"xl_row": 2, "category_check": {"判定": "属于本类目", "信号": []},
                          "attributes": {"p1": ["不锈钢", "标题", "stainless bowl"],
                                         "p2": ["是", "五点描述", "dishwasher safe"]}}]}
    p = producer(tmp_path, llm)
    row = p._produce_chunk(p.rows, "a1", None)[2]
    assert row["attributes"] == {"主体材质": "不锈钢", "主体材质_来源": "标题",
                                  "主体材质_依据": "stainless bowl", "是否可洗碗机清洗": "是",
                                  "是否可洗碗机清洗_来源": "五点描述", "是否可洗碗机清洗_依据": "dishwasher safe"}
    assert row["category_check"]["判定"] == "属于本类目"


def test_bad_code_or_triple_falls_back_to_original_contract(tmp_path):
    for bad in ({"p1": ["木", "标题"]}, {"p999": ["木", "标题", "wood"]},
                {"p1_来源": "标题"}):
        prompts = []
        def llm(prompt):
            prompts.append(prompt)
            attrs = bad if len(prompts) == 1 else {"主体材质": "木", "主体材质_来源": "标题", "主体材质_依据": "wood"}
            return {"rows": [{"xl_row": 2, "attributes": attrs}]}
        p = producer(tmp_path, llm)
        result = p._produce_chunk(p.rows, "a1", None)
        assert result[2]["attributes"]["主体材质_依据"] == "wood"
        assert len(prompts) == 2
        assert "属性编号映射" in prompts[0] and "属性编号映射" not in prompts[1]


def test_compact_does_not_invent_missing_fields_and_versions_cache(tmp_path):
    def llm(prompt):
        return {"rows": [{"xl_row": 2, "attributes": {"p1": ["未明确", "", "原文未说明"]}}]}
    p = producer(tmp_path, llm)
    row = p._produce_chunk(p.rows, "a1", None)[2]
    assert "是否可洗碗机清洗" not in row["attributes"]
    old = CallableProducer(llm, p.rows, str(tmp_path), "a", "完整标注规则",
                           input_fields=p.input_fields)
    assert p._input_key() != old._input_key()


def test_compact_runs_through_existing_gate_and_delivery(tmp_path):
    from tools.attr_stability import AttrSpec, RuleSource
    from tools.attr_stability.attr_runner import run_round

    rules = RuleSource()
    rules.attrs = {"供电方式": AttrSpec(name="供电方式", values=["电池供电", "USB充电"])}
    source = {"xl_row": 1, "标题": "battery powered bowl"}
    def llm(prompt):
        return {"rows": [{"xl_row": 1, "category_check": {"判定": "属于本类目"},
                          "attributes": {"p1": ["电池供电", "标题", "battery powered"]}}]}
    p = CallableProducer(llm, [source], str(tmp_path), "a", "P", workers=1,
                         input_fields=["xl_row", "标题"], compact_attributes=["供电方式"])
    result = run_round(p, rules, n_runs=2, max_retry=0, input_rows=[source],
                       require_complete=True, require_basis=True, log=lambda _: None)
    assert result.passed == ["a1", "a2"]
    assert all(g.required_errors == 0 for g in result.gates.values())
    assert result.deliverable
