"""Contracts and isolation of identity versus value-rule review."""
import copy
import pytest
from scripts._plan_pilot.step16_test import run_separated_review as s
from scripts._plan_pilot.step16_test import finalize_separated_review as f


ROWS = [{'id': 'a', 'name': '录音功能', 'hint': '录音呼唤，未提录音写否', 'family': 'voice'}]


def identity():
    return {'items': [{'id': 'a', 'status': 'single', 'canonical_id': 'V_RECORD',
                       'related_ids': ['V_RECORD'], 'object': '录音模块', 'dimension': '有无录音',
                       'value_kind': 'boolean', 'evidence': [{'field': 'hint', 'quote': '录音呼唤'}],
                       'reason': '主要测量录音呼唤功能'}]}


def rules():
    return {'items': [{'id': 'a', 'issues': [{'kind': 'error', 'code': 'absence_as_negative',
                       'quote': '未提录音写否', 'reason': '未提及不等于不支持', 'suggestion': '未提及写未明确'}],
                       'summary': '未知被当成否'}]}


def test_identity_is_retained_when_filling_rule_has_an_error():
    i = s.combine(ROWS, identity(), identity(), 'identity')['a']
    v = s.combine(ROWS, rules(), rules(), 'rules')['a']
    assert i['status'] == 'single' and i['canonical_id'] == 'V_RECORD'
    assert v['status'] == 'confirmed_error'


@pytest.mark.parametrize('field,value', [('canonical_id', 'invented'), ('value_kind', 'count'),
                                        ('related_ids', ['V_RECORD', 'V_LIVE'])])
def test_identity_id_type_and_single_concept_are_closed(field, value):
    broken = identity()
    broken['items'][0][field] = value
    with pytest.raises(ValueError):
        s.validate_identity(broken, ROWS)


def test_identity_quote_must_be_in_the_stated_source_field():
    broken = identity()
    broken['items'][0]['evidence'][0]['quote'] = '原文没有这句话'
    with pytest.raises(ValueError):
        s.validate_identity(broken, ROWS)


def test_gap_cannot_be_promoted_to_quoted_error():
    broken = rules()
    broken['items'][0]['issues'][0]['quote'] = None
    with pytest.raises(ValueError):
        s.validate_rules(broken, ROWS)
    broken['items'][0]['issues'][0].update(kind='gap', code='missing_conflict_policy')
    s.validate_rules(broken, ROWS)
    actual = s.combine(ROWS, broken, broken, 'rules')['a']
    assert actual['status'] == 'incomplete' and actual['confirmed_error_codes'] == []


def test_one_sided_error_is_not_claimed_as_two_independent_confirmations():
    clear = {'items': [{'id': 'a', 'issues': [], 'summary': '未见明确错误'}]}
    actual = s.combine(ROWS, rules(), clear, 'rules')['a']
    assert actual['status'] == 'review_disagreement'
    assert actual['unconfirmed_error_codes'] == ['absence_as_negative']


def test_compound_has_no_single_canonical_id_and_cannot_silently_vote_twice():
    value = identity()
    value['items'][0].update(status='compound', canonical_id=None, related_ids=['V_RECORD', 'V_LIVE'])
    s.validate_identity(value, ROWS)
    actual = s.combine(ROWS, value, copy.deepcopy(value), 'identity')['a']
    assert actual['canonical_id'] is None and actual['status'] == 'compound'


def test_duplicate_records_and_fabricated_error_quotes_are_rejected():
    bad = rules()
    bad['items'].append(copy.deepcopy(bad['items'][0]))
    with pytest.raises(ValueError):
        s.validate_rules(bad, ROWS)


def test_source_adjudication_cannot_invent_evidence_or_promote_reference_answers():
    decision = {'id': 'a', 'status': 'uncertain', 'canonical_id': None,
                'related_ids': ['V_RECORD', 'V_LIVE'], 'quote': '录音呼唤', 'reason': '范围待澄清'}
    f.validate_decisions(ROWS, [decision], [])
    for change in [{'quote': '原文没有'}, {'canonical_id': 'V_RECORD'}, {'status': 'single'}]:
        with pytest.raises(ValueError):
            f.validate_decisions(ROWS, [{**decision, **change}], [])


def test_adjudicated_rule_error_requires_closed_code_and_exact_quote():
    decision = {'id': 'a', 'kind': 'error', 'code': 'absence_as_negative',
                'quote': '未提录音写否', 'reason': '无负证据', 'suggestion': '未提写未知'}
    f.validate_decisions(ROWS, [], [decision])
    with pytest.raises(ValueError):
        f.validate_decisions(ROWS, [], [{**decision, 'code': 'invented'}])
    bad = rules()
    bad['items'][0]['issues'][0]['quote'] = '编造的默认值'
    with pytest.raises(ValueError):
        s.validate_rules(bad, ROWS)
