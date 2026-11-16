import copy
import json
from pathlib import Path

import pytest

from tools.attr_stability.feature_groups import (expand_attributes, group_boolean_attributes,
                                                 restore_confirmed_groups)
from tools.attr_stability.spec_io import (assert_rule_alignment, commit_attr_spec,
                                           validate_attr_spec, verify_annotation_contract,
                                           make_annotation_contract)
from tools.attr_stability.step18_workspace import Workspace


def attrs():
    return [{'name': '是否甲', 'hint': '形态：是否；甲独立三态', 'source': 'llm', 'runs': 4},
            {'name': '是否乙', 'hint': '形态：是否；乙独立三态', 'source': 'llm', 'runs': 3},
            {'name': '是否丙', 'hint': '形态：是否；丙独立三态', 'source': 'llm', 'runs': 3},
            {'name': '尺寸', 'hint': '实际尺寸', 'source': 'llm', 'runs': 3}]


class Model:
    def __init__(self, response): self.response = response
    def chat_json(self, *args, **kwargs): return self.response


def test_group_ratio_preserves_children_and_their_entire_definitions():
    initial = attrs()
    proposal = {'groups': [{'name': '设备功能', 'members': ['是否甲', '是否乙', '是否丙'], 'reason': '同一设备'}]}
    grouped, audit = group_boolean_attributes(initial, Model(proposal))
    assert len(grouped) == 2 and len(audit) == 1
    assert {a['name']: a for a in expand_attributes(grouped)} == {a['name']: a for a in initial}
    assert initial == attrs()
    assert grouped[0]['group_members'][0]['hint'] == '形态：是否；甲独立三态'


def test_manual_and_wrong_shape_and_invalid_proposals_are_not_absorbed():
    initial = attrs()
    initial[1]['modified'] = True
    initial[2]['hint'] = '形态：多选；不同状态'
    proposal = {'groups': [{'name': '功能', 'members': ['是否甲', '是否乙'], 'reason': 'x'},
                           {'name': '功能2', 'members': ['是否甲', '是否丙'], 'reason': 'x'},
                           {'name': '功能3', 'members': ['是否甲', '不存在'], 'reason': 'x'}]}
    assert group_boolean_attributes(initial, Model(proposal)) == (initial, [])


def test_spec_commit_keeps_rules_physical_and_preserves_all_columns(tmp_path):
    root = tmp_path / 'data' / 'Sample-US-202610'
    group, _ = group_boolean_attributes(attrs(), Model({'groups': [
        {'name': '设备功能', 'members': ['是否甲', '是否乙', '是否丙'], 'reason': '同一设备'}]}))
    result = commit_attr_spec(root, {'cat': root.name, 'status': 'pending_confirm', 'attributes': group})
    spec = result['spec']
    rules = json.loads(Path(result['rules_path']).read_text(encoding='utf-8'))
    assert len(spec['attributes']) == 2
    assert {a['name'] for a in rules['attributes']} == {a['name'] for a in attrs()}
    assert_rule_alignment(spec, rules)
    spec['status'] = 'confirmed'
    source = tmp_path / 'upstream.xlsx'; source.write_bytes(b'test')
    rows = [{'xl_row': 2}]
    contract = make_annotation_contract(spec, rows, source_path=source)
    contract['completed'] = True
    delivered = {'xl_row': 2, 'attributes': {}}
    for a in attrs():
        delivered['attributes'][a['name']] = '未明确'
        delivered['attributes'][a['name'] + '_来源'] = ''
    verify_annotation_contract(spec, rules, {'annotation_contract': contract, 'deliverable': [delivered]}, rows, source_path=source)
    del delivered['attributes']['是否乙']
    with pytest.raises(ValueError, match='缺少必填属性'):
        verify_annotation_contract(spec, rules, {'annotation_contract': contract, 'deliverable': [delivered]}, rows, source_path=source)


def test_validation_rejects_duplicate_child_and_top_level_collision():
    group = {'name': '功能', 'group_members': [{'name': '是否甲'}, {'name': '是否乙'}]}
    with pytest.raises(ValueError, match='冲突'):
        validate_attr_spec({'attributes': [group, {'name': '是否甲'}]})
    duplicate = copy.deepcopy(group); duplicate['name'] = '功能二'
    with pytest.raises(ValueError, match='重复'):
        validate_attr_spec({'attributes': [group, duplicate]})


def test_rerun_restores_prior_group_and_does_not_lose_absent_child():
    group = {'name': '功能', 'hint': '功能组', 'group_members': [attrs()[0], attrs()[1]]}
    refreshed = [attrs()[0], attrs()[2], attrs()[3]]
    out = restore_confirmed_groups(refreshed, [group])
    assert {a['name'] for a in expand_attributes(out)} == {a['name'] for a in attrs()}
    assert [a['name'] for a in out[0]['group_members']] == ['是否甲', '是否乙']


def test_new_group_name_cannot_collide_with_existing_child():
    previous = {'name': '旧组', 'group_members': [{'name': '设备功能'}, {'name': '是否甲'}]}
    incoming = [previous, {'name': '是否乙'}, {'name': '是否丙'}, {'name': '是否丁'}]
    proposal = {'groups': [{'name': '设备功能', 'members': ['是否乙', '是否丙'], 'reason': '同类'}]}
    assert group_boolean_attributes(incoming, Model(proposal)) == (incoming, [])


def test_step18_import_keeps_child_cells_independent(tmp_path):
    root = tmp_path / 'data' / 'Sample-US-202610'
    group, _ = group_boolean_attributes(attrs(), Model({'groups': [
        {'name': '设备功能', 'members': ['是否甲', '是否乙', '是否丙'], 'reason': '同一设备'}]}))
    committed = commit_attr_spec(root, {'cat': root.name, 'status': 'pending_confirm', 'attributes': group})
    spec = committed['spec']; spec['status'] = 'confirmed'
    spec_path = Path(committed['path']); spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
    source = root / '14_流量拆分/流量拆分数据-需人工补充.xlsx'
    source.parent.mkdir(parents=True); source.write_bytes(b'upstream')
    rows = [{'xl_row': 2, '标题': '样本'}]
    indir = root / '17_属性标注'; indir.mkdir()
    (indir / 'input.json').write_text(json.dumps({'rows': rows}, ensure_ascii=False), encoding='utf-8')
    contract = make_annotation_contract(spec, rows, source_path=source); contract['completed'] = True
    cell_values = {'是否甲': '是', '是否乙': '否', '是否丙': '未明确', '尺寸': '2cm'}
    cells = {}
    for name, value in cell_values.items():
        cells.update({name: value, name + '_来源': '标题', name + '_依据': name + '的原文'})
    result = {'annotation_contract': contract, 'deliverable': [{'xl_row': 2, 'attributes': cells}]}
    (indir / '稳定性结果.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    workspace = Workspace(root, spec_path=spec_path, rules_path=committed['rules_path'])
    baseline = workspace._baseline()
    assert {a['name'] for a in baseline['attributes']} == set(cell_values)
    for name, value in cell_values.items():
        assert baseline['rows'][0]['attributes'][name] == {'value': value, 'source': '标题', 'basis': name + '的原文'}
    initial = workspace.open()
    preview = workspace.propose({'expected_version': initial['version'], 'requirement': '归类',
                                 'selection': {'attributes': ['是否甲'], 'values': {'是否甲': ['是']}},
                                 'mapping': {'是否甲': {'是': '否'}}, 'mode': 'text'})
    applied = workspace.apply(preview['id'], initial['version'])
    assert applied['rows'][0]['attributes']['是否甲']['value'] == '否'
    assert applied['rows'][0]['attributes']['是否乙']['value'] == '否'
    assert applied['rows'][0]['attributes']['是否丙']['value'] == '未明确'


def test_web_save_keeps_group_children_and_updates_only_edited_hint(tmp_path, monkeypatch):
    from webui import server
    from tools.attr_stability.spec_io import spec_revision
    root = tmp_path / 'data' / 'Sample-US-202610'
    group, _ = group_boolean_attributes(attrs(), Model({'groups': [
        {'name': '设备功能', 'members': ['是否甲', '是否乙', '是否丙'], 'reason': '同一设备'}]}))
    committed = commit_attr_spec(root, {'cat': root.name, 'status': 'pending_confirm', 'attributes': group})
    spec = committed['spec']; handler = server.Handler.__new__(server.Handler)
    monkeypatch.setattr(handler, '_attr_spec_resolve', lambda _: (root.name, Path(committed['path'])))
    monkeypatch.setattr(handler, '_commit_attr_spec', lambda _file, draft, revision:
                        commit_attr_spec(root, draft, expected_revision=revision))
    monkeypatch.setattr(server, 'push_cat', lambda *args, **kwargs: None)
    for method in ('_excluded_list', '_candidates_list', '_spec_runs_info', '_stab_min_rate_info'):
        monkeypatch.setattr(handler, method, lambda *_: [])
    response = handler._api_attr_spec({'file': ['unused']})
    assert [c['name'] for c in response['attributes'][0]['group_members']] == ['是否甲', '是否乙', '是否丙']
    payload = copy.deepcopy(group)
    payload[0]['group_members'][0]['hint'] = '手工修正的依据'
    handler._api_attr_spec_save({'attributes': payload, 'expected_revision': spec_revision(spec)})
    updated = json.loads(Path(committed['path']).read_text(encoding='utf-8'))
    children = updated['attributes'][0]['group_members']
    assert children[0]['hint'] == '手工修正的依据' and children[0]['modified'] is True
    assert children[1:] == group[0]['group_members'][1:]
    payload[0]['group_members'].pop()
    with pytest.raises(ValueError, match='子属性已变化'):
        handler._api_attr_spec_save({'attributes': payload, 'expected_revision': spec_revision(updated)})


def test_web_can_ungroup_without_losing_child_metadata(tmp_path, monkeypatch):
    from webui import server
    from tools.attr_stability.spec_io import spec_revision
    root = tmp_path / 'data' / 'Sample-US-202610'
    grouped, _ = group_boolean_attributes(attrs(), Model({'groups': [
        {'name': '设备功能', 'members': ['是否甲', '是否乙', '是否丙'], 'reason': '同一设备'}]}))
    committed = commit_attr_spec(root, {'cat': root.name, 'status': 'pending_confirm', 'attributes': grouped})
    handler = server.Handler.__new__(server.Handler)
    monkeypatch.setattr(handler, '_attr_spec_resolve', lambda _: (root.name, Path(committed['path'])))
    monkeypatch.setattr(handler, '_commit_attr_spec', lambda _file, draft, revision:
                        commit_attr_spec(root, draft, expected_revision=revision))
    monkeypatch.setattr(server, 'push_cat', lambda *args, **kwargs: None)
    ungrouped = [{'name': c['name'], 'hint': c['hint']} for c in grouped[0]['group_members']] + [grouped[1]]
    handler._api_attr_spec_save({'attributes': ungrouped, 'expected_revision': spec_revision(committed['spec'])})
    updated = json.loads(Path(committed['path']).read_text(encoding='utf-8'))
    assert [a['name'] for a in updated['attributes']] == [a['name'] for a in attrs()]
    assert all(a['source'] == 'llm' and not a['modified'] for a in updated['attributes'])
    assert [a['runs'] for a in updated['attributes']] == [a['runs'] for a in attrs()]
