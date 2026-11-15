import copy
from pathlib import Path

from steps.step17_attr_annotation import annotation_chunk_size, compact_annotation_prompt, Step17AttrAnnotation
from tools.attr_stability import AttrSpec, RuleSource
from tools.attr_stability.attr_runner import CallableProducer, run_round, quarantine_unknown_attributes


def test_quarantine_preserves_extra_values_and_known_cells():
    rows = {2: {"xl_row": 2, "attributes": {"材质": "木", "材质_来源": "标题", "材质_依据": "wood",
                                             "新属性": "是", "新属性_来源": "标题", "新属性_依据": "extra"}}}
    before = copy.deepcopy(rows)
    isolated = quarantine_unknown_attributes(rows, {"材质"})
    assert isolated == [{"xl_row": 2, "attributes": {"新属性": "是", "新属性_来源": "标题", "新属性_依据": "extra"}}]
    assert rows[2]["attributes"] == {k: v for k, v in before[2]["attributes"].items() if k.startswith("材质")}
    assert quarantine_unknown_attributes(rows, {"材质"}) == []


def test_grouped_repair_reuses_one_call_and_preserves_other_fields(tmp_path):
    rows = [{"xl_row": i, "标题": "wood %d" % i} for i in (1, 2, 3)]
    before = {i: {"xl_row": i, "attributes": {"颜色": "蓝"}} for i in (1, 2, 3)}
    calls = []
    def model(prompt):
        calls.append(prompt)
        return {"rows": [{"xl_row": i, "attributes": {"材质": "木", "材质_来源": "标题", "材质_依据": "wood %d" % i}}
                         for i in (1, 2, 3)]}
    prod = CallableProducer(model, rows, str(tmp_path), 'a', '规则', workers=1, input_fields=['xl_row', '标题'])
    result = prod.fill_missing(before, {1: ['材质'], 2: ['材质'], 3: ['材质']})
    assert len(calls) == 1
    assert all(result[i]['attributes']['颜色'] == '蓝' and result[i]['attributes']['材质_依据'] == 'wood %d' % i for i in (1, 2, 3))


def test_grouped_repair_falls_back_for_missing_row(tmp_path):
    rows = [{"xl_row": i, "标题": "wood"} for i in (1, 2)]
    before = {i: {"xl_row": i, "attributes": {}} for i in (1, 2)}
    calls = []
    def model(prompt):
        calls.append(prompt)
        i = 1 if len(calls) == 1 else 2
        return {"rows": [{"xl_row": i, "attributes": {"材质": "木", "材质_来源": "标题", "材质_依据": "wood"}}]}
    prod = CallableProducer(model, rows, str(tmp_path), 'a', '规则', input_fields=['xl_row', '标题'])
    result = prod.fill_missing(before, {1: ['材质'], 2: ['材质']})
    assert len(calls) == 2 and all(result[i]['attributes']['材质'] == '木' for i in (1, 2))


def test_compact_prompt_and_adaptive_batches():
    base = Path('prompts/23_llm_only_pipeline.md').read_text(encoding='utf-8')
    compact = compact_annotation_prompt(base, '独有属性说明', '')
    assert '独有属性说明' in compact and '## 4. 输出格式' not in compact
    assert '### 3.3 尺寸拆列' not in compact and '没有图片或图片 OCR' in compact
    assert compact_annotation_prompt('自定义规则', '', '').startswith('自定义规则')
    assert annotation_chunk_size(12, 59) == 12 and annotation_chunk_size(12, 80) == 9
    assert annotation_chunk_size(12, 10) == 12


def test_default_core_is_general_and_dynamic_rules_are_preserved(ws_tmp):
    from tests.test_step17_attr_standardization import _ctx
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    core = st._load_prompt(ctx)
    prompt = compact_annotation_prompt(core, '本类目：容量与粮仓', '本类目约束：区分粮仓和水仓')
    assert core.startswith('# 步骤17通用属性标注核心规则')
    assert all(term not in core for term in ('砧板', '除湿机', '汁槽', 'Cutting Board', 'Box Dryer'))
    assert '本类目：容量与粮仓' in prompt and '区分粮仓和水仓' in prompt
    assert '标题、五点描述、详细参数、SKU' in prompt
    assert '未明确' in prompt and '否定值须有直接否定证据' in prompt
    assert '尺寸保留当前商品或套装的原始数值和单位' in prompt


def test_custom_prompt_remains_available(ws_tmp):
    from tests.test_step17_attr_standardization import _ctx
    ctx = _ctx(ws_tmp, {'ATTR_STAB_PROMPT': 'custom.txt'})
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    (Path(ctx.paths.cat_root) / 'custom.txt').write_text('我的定制规则', encoding='utf-8')
    assert st._load_prompt(ctx) == '我的定制规则'


def test_gate_status_is_distinct_from_nonblocking_diagnostics(tmp_path):
    rules = RuleSource()
    rules.attrs = {'材质': AttrSpec(name='材质', values=['木'])}
    src = {'xl_row': 1, '标题': 'wood'}
    def model(prompt):
        return {'rows': [{'xl_row': 1, 'category_check': {'判定': '属于本类目'},
                          'attributes': {'p1': ['木', '标题', 'wood'], '额外': '值'}}]}
    producer = CallableProducer(model, [src], str(tmp_path), 'a', '规则', workers=1, compact_attributes=['材质'])
    res = run_round(producer, rules, n_runs=2, input_rows=[src], require_complete=True,
                    require_basis=True, strict_gate=False, log=lambda _: None)
    d = res.to_dict()
    assert d['gates']['a1']['delivery_status'] == '已纳入交付'
    assert d['gates']['a1']['required_errors'] == 0
    assert d['quarantined_attributes']['a1'][0]['attributes'] == {'额外': '值'}
    assert not any('额外' in key for key in d['cells'])
