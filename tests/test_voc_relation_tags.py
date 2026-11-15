import copy

import pytest

from tools.voc.relation_tags import classify_relations, review_relations


def chain():
    return {'aspects': [
        {'evidence_id': 'E1', 'review_key': 'R1', 'taxonomy_category': '体验', 'canonical_attribute': '效率'},
        {'evidence_id': 'E2', 'review_key': 'R2', 'taxonomy_category': '体验', 'canonical_attribute': '噪音'},
        {'evidence_id': 'E3', 'review_key': 'R3', 'taxonomy_category': '维护', 'canonical_attribute': '清洁'}],
        'needs': [{'need_id': 'N1', 'outcome': '顺利完成任务', 'evidence_ids': ['E1', 'E2']},
                  {'need_id': 'N2', 'outcome': '省力维护', 'evidence_ids': ['E3']}],
        'opportunities': [{'open_id': 'O1', 'need_ids': ['N1'], 'evidence_ids': ['E2'],
                           'remaining_gap': '仍影响体验', 'gap_type': 'both', 'mechanisms': ['待验证方案']} ]}


def test_multilabel_categories_do_not_merge_need_ids_or_broadcast_open():
    c = chain()
    original = copy.deepcopy(c)
    classify_relations(c)
    assert c['needs'][0]['attribute_labels'] == ['体验：效率', '体验：噪音']
    assert [n['need_id'] for n in c['needs']] == ['N1', 'N2']
    assert [n['outcome'] for n in c['needs']] == [n['outcome'] for n in original['needs']]
    first = review_relations(c, 'R1')
    assert first['Need细分类'] == '体验：效率'
    assert first['Open数'] == 0 and first['候选Open'] == ''
    second = review_relations(c, 'R2')
    assert second['Open数'] == 1 and second['Open细分类'] == '体验：噪音'
    assert 'E2' in second['候选Open'] and 'E1' not in second['候选Open']
    assert review_relations(c, 'R3')['Need细分类'] == '维护：清洁'
    assert review_relations(c, 'unknown')['Need数'] == 0


@pytest.mark.parametrize('fault', ['evidence', 'need', 'cross_need'])
def test_invalid_relation_cannot_be_written_back(fault):
    c = chain()
    op = c['opportunities'][0]
    if fault == 'evidence':
        op['evidence_ids'] = ['missing']
    elif fault == 'need':
        op['need_ids'] = ['missing']
    else:
        op['evidence_ids'] = ['E3']
    with pytest.raises(ValueError):
        classify_relations(c)
