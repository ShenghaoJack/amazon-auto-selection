"""Integrity guards for the isolated occurrence pilot, no model/network calls."""
import copy
import pytest
from scripts._plan_pilot.step16_test import run_optimized_semantic_pilot as p
from scripts._plan_pilot.step16_test import audit_optimized_pilot as audit


def item(rid, cid='M_FOOD', decision='match'):
    return {'id': rid, 'canonical_id': cid, 'decision': decision, 'reason': 'source', 'warnings': []}


def test_closed_ids_and_exact_coverage():
    records = [{'id': 'a'}, {'id': 'b'}]
    value = {'items': [item('a'), item('b')]}
    assert p.validate_mapping(value, records, {'M_FOOD': {}}) == value
    for broken in [{'items': [item('a'), item('a')]}, {'items': [item('a'), item('c')]},
                   {'items': [item('a'), item('b', 'invented')]},
                   {'items': [item('a'), item('b', 'M_FOOD', 'uncertain')]}]:
        with pytest.raises(ValueError):
            p.validate_mapping(broken, records, {'M_FOOD': {}})


def test_same_name_occurrences_keep_different_identity_and_local_disagreement():
    records = [{'id': 'a', 'name': '材质'}, {'id': 'b', 'name': '材质'}, {'id': 'c', 'name': '材质'}]
    mapper = {'items': [item('a', 'M_BODY'), item('b', 'M_FOOD'), item('c', 'M_BODY')]}
    reviewer = copy.deepcopy(mapper)
    reviewer['items'][2] = item('c', None, 'uncertain')
    result = p.resolve(records, mapper, reviewer)
    assert [r['canonical_id'] for r in result] == ['M_BODY', 'M_FOOD', None]
    assert result[2]['status'] == 'disagreement'


def test_uncertain_and_outside_are_different_and_null_cannot_be_a_vote():
    rows = [{'id': 'a', 'band': 'L01', 'round': 1}, {'id': 'b', 'band': 'L01', 'round': 2}]
    result = p.resolve(rows, {'items': [item('a', None, 'uncertain'), item('b', None, 'outside_scope')]},
                       {'items': [item('a', None, 'outside_scope'), item('b', None, 'outside_scope')]})
    assert [r['status'] for r in result] == ['disagreement', 'outside_scope']
    assert p.tally(result, {})['stable'] == []


def test_dedup_then_three_of_three_then_union():
    rows = [{'id': str(i), 'band': band, 'round': rnd, 'canonical_id': cid, 'status': status}
            for i, (band, rnd, cid, status) in enumerate([
                ('L01', 1, 'A', 'match'), ('L01', 1, 'A', 'match'),
                ('L01', 2, 'A', 'match'), ('L01', 3, 'A', 'uncertain'),
                ('L02', 1, 'B', 'match'), ('L02', 2, 'B', 'match'), ('L02', 3, 'B', 'match')])]
    value = p.tally(rows, {'A': {}, 'B': {}})
    assert value['by_band']['L01']['votes']['A'] == 2
    assert value['stable'] == ['B']
    assert value['candidates'] == ['A']


def test_pair_review_rejects_duplicates_unknown_ids_and_equivalents():
    defs = {'a': {}, 'b': {}}
    good = {'approved': True, 'checked_ids': ['a', 'b'],
            'pairs': [{'ids': ['a', 'b'], 'relation': 'distinct', 'reason': 'different components'}]}
    assert p.validate_definition_review(good, defs) == good
    for pair in [{'ids': ['a', 'fake'], 'relation': 'distinct', 'reason': 'x'},
                 {'ids': ['a', 'b'], 'relation': 'equivalent', 'reason': 'x'}]:
        with pytest.raises(ValueError):
            p.validate_definition_review({**good, 'pairs': [pair]}, defs)


def test_contract_quote_must_be_in_source_and_target_cannot_change():
    rows = [{'id': 'a', 'hint': '本属性同时包含食碗与水碗', 'canonical_id': 'M_FOOD'}]
    value = {'items': [{'id': 'a', 'canonical_id': 'M_FOOD', 'relation': 'not_equivalent',
                        'source_scope': '食水碗联合材质', 'quote': '同时包含食碗与水碗', 'reason': '范围较宽'}]}
    assert audit.validate(value, rows) == value
    for field, broken in [('quote', '原文没有的假证据'), ('canonical_id', 'M_WATER'),
                          ('id', 'invented'), ('relation', 'merge')]:
        changed = copy.deepcopy(value)
        changed['items'][0][field] = broken
        with pytest.raises(ValueError):
            audit.validate(changed, rows)


def test_closed_definition_ids_cannot_drift():
    value = {'definitions': {key: {field: field for field in p.old.FIELDS}
                             for key in p.SLOTS['voice']}}
    for d in value['definitions'].values():
        d['value_kind'] = 'boolean'
    value['definitions']['V_SECONDS']['value_kind'] = 'duration'
    assert p.validate_definitions(value, 'voice') == value
    value['definitions']['new_id'] = value['definitions'].pop('V_LIVE')
    with pytest.raises(ValueError):
        p.validate_definitions(value, 'voice')


def test_in_scope_unknown_cannot_be_reported_as_an_outside_control():
    with pytest.raises(ValueError):
        p.validate_mapping({'items': [item('a', None, 'outside_scope')]},
                           [{'id': 'a', 'family': 'material'}], {'M_FOOD': {}})
    row = {'id': 'a', 'family': 'negative'}
    assert p.validate_mapping({'items': [item('a', None, 'outside_scope')]}, [row], {})


def test_duplicate_checked_ids_do_not_qualify_as_independent_review_coverage():
    with pytest.raises(ValueError):
        p.validate_definition_review({'approved': True, 'checked_ids': ['a', 'a', 'b'],
                                      'pairs': [{'ids': ['a', 'b'], 'relation': 'distinct', 'reason': 'x'}]},
                                     {'a': {}, 'b': {}})
