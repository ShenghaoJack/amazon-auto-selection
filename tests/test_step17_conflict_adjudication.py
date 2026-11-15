from tools.attr_stability import AttrSpec, RuleSource
from tools.attr_stability.attr_runner import CallableProducer, run_round
from tools.attr_stability.attr_stability import review_queue, union_cells, deliverable_from_cells
from tools.attr_stability.conflict_adjudication import adjudicate


def _row(rid, color, src='标题', basis='red object'):
    return {'xl_row': rid, 'category_check': {'判定': '属于本类目'},
            'attributes': {'颜色': color, '颜色_来源': src, '颜色_依据': basis}}


def _setup(tmp_path, fn):
    rows = [{'xl_row': 1, '标题': 'red object', '五点描述': '', '详细参数': '', 'SKU': ''},
            {'xl_row': 2, '标题': 'blue object', '五点描述': '', '详细参数': '', 'SKU': ''},
            {'xl_row': 3, '标题': 'green object', '五点描述': '', '详细参数': '', 'SKU': ''}]
    runs = {'a1': {1: _row(1, '红'), 2: _row(2, '是'), 3: _row(3, '绿')},
            'a2': {1: _row(1, '蓝'), 2: _row(2, '否'), 3: _row(3, '绿')}}
    rules = RuleSource()
    rules.attrs = {'颜色': AttrSpec(name='颜色', values=[])}
    p = CallableProducer(fn, rows, str(tmp_path), 'a', '规则', workers=1)
    return rows, runs, rules, p


def test_third_pass_only_conflicts_and_keeps_corresponding_evidence(tmp_path):
    calls = []
    def model(prompt):
        calls.append(prompt)
        assert 'green object' not in prompt
        return {'rows': [
            {'xl_row': 1, 'attributes': {'颜色': {'value': '红', 'source': '标题', 'basis': 'red object'}}},
            {'xl_row': 2, 'attributes': {'颜色': {'value': '未明确', 'source': '', 'basis': '资料不足'}}},
        ]}
    rows, runs, rules, p = _setup(tmp_path, model)
    cells = union_cells(runs, ['a1', 'a2'])
    stats = adjudicate(cells, runs, p, rules)
    assert len(calls) == 1 and stats['target_cells'] == 2 and stats['resolved_cells'] == 1
    assert cells['1|attr.颜色']['mode'] == '红'
    assert cells['1|attr.颜色_来源']['mode'] == '标题'
    assert cells['1|attr.颜色_依据']['mode'] == 'red object'
    assert cells['2|attr.颜色']['mode'] == ''
    assert cells['3|attr.颜色']['mode'] == '绿'
    assert [x['行号'] for x in review_queue(cells)] == [2]
    delivered = {r['xl_row']: r for r in deliverable_from_cells(cells, [1, 2, 3])}
    assert delivered[1]['attributes']['颜色'] == '红' and delivered[2]['attributes']['颜色'] == ''
    again = union_cells(runs, ['a1', 'a2'])
    resumed = adjudicate(again, runs, p, rules)
    assert len(calls) == 1 and resumed['model_calls'] == 0 and again['1|attr.颜色']['mode'] == '红'


def test_bad_or_unproven_third_vote_not_applied(tmp_path):
    def model(prompt):
        return {'rows': [{'xl_row': 1, 'attributes': {'颜色': {'value': '红', 'source': '标题', 'basis': 'invented text'}}},
                         {'xl_row': 2, 'attributes': {'颜色': None}}]}
    _, runs, rules, p = _setup(tmp_path, model)
    cells = union_cells(runs, ['a1', 'a2'])
    stats = adjudicate(cells, runs, p, rules)
    assert stats['completed_cells'] == 0 and stats['unresolved_cells'] == 2
    assert cells['1|attr.颜色']['mode'] == '' and len(review_queue(cells)) == 2


def test_run_round_supports_targeted_third_pass(tmp_path):
    rules = RuleSource()
    rules.attrs = {'材质': AttrSpec(name='材质', values=['木', '塑料'])}
    rows = [{'xl_row': 1, '标题': 'wood material'}]
    calls = []
    def model(prompt):
        calls.append(prompt)
        if '待复核属性' in prompt:
            return {'rows': [{'xl_row': 1, 'attributes': {'材质': {'value': '木', 'source': '标题', 'basis': 'wood material'}}}]}
        value = '木' if len(calls) == 1 else '塑料'
        return {'rows': [{'xl_row': 1, 'category_check': {'判定': '属于本类目'},
                          'attributes': {'材质': value, '材质_来源': '标题', '材质_依据': 'wood material'}}]}
    p = CallableProducer(model, rows, str(tmp_path), 'a', 'test', workers=1)
    res = run_round(p, rules, n_runs=2, input_rows=rows, strict_gate=False,
                    require_complete=True, require_basis=True, max_retry=0, resolve_conflicts=True, log=lambda _: None)
    assert res.conflict_adjudication['resolved_cells'] == 1 and len(calls) == 3
    assert res.deliverable[0]['attributes']['材质'] == '木'


def test_partial_batch_is_resumed_without_reasking_completed_cell(tmp_path):
    calls = []
    def model(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return {'rows': [{'xl_row': 1, 'attributes': {'颜色': {'value': '红', 'source': '标题', 'basis': 'red object'}}}]}
        assert 'red object' not in prompt and 'blue object' in prompt
        return {'rows': [{'xl_row': 2, 'attributes': {'颜色': {'value': '未明确', 'source': '', 'basis': '无法确定'}}}]}
    _, runs, rules, p = _setup(tmp_path, model)
    first = adjudicate(union_cells(runs, ['a1', 'a2']), runs, p, rules)
    assert first['completed_cells'] == 1
    second = adjudicate(union_cells(runs, ['a1', 'a2']), runs, p, rules)
    assert second['completed_cells'] == 2 and len(calls) == 2


def test_two_identical_passes_need_no_model_and_old_parameter_does_not_skip(tmp_path):
    from core.config import Config
    from steps.step17_attr_annotation import Step17AttrAnnotation
    from types import SimpleNamespace
    _, runs, rules, p = _setup(tmp_path, lambda _: (_ for _ in ()).throw(AssertionError('unexpected model call')))
    runs['a2'] = {rid: _row(rid, row['attributes']['颜色']) for rid, row in runs['a1'].items()}
    result = adjudicate(union_cells(runs, ['a1', 'a2']), runs, p, rules)
    assert result['target_cells'] == 0
    assert not Step17AttrAnnotation().skip_when(SimpleNamespace(config=Config({'ATTR_STAB_RUNS': 0})))
