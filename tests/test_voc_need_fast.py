"""Need speed changes must preserve coverage, paid results, scope and retries."""
import copy
import json
from pathlib import Path
import threading

import pytest

from core.errors import LLMError
from tests.test_voc_opportunity import aspect, config
from tools.voc.opportunity import Analyzer, digest, save, VERSION
from tools.voc.classification import review_context
from tools.voc.need_fast import (NeedStore, canonical_groups, eligible, import_legacy, node_id,
                                 prepare_batch, synthesize, PROMPT, ContextIndex, merge_needs)
from tools.voc.solutions import NEED_ONLY


def data(count=96):
    aspects, reviews = [], []
    for i in range(count):
        key = f"US::R{i}"
        aspects.append(dict(aspect('Fast'), evidence_id=f"evidence-{i:04d}", review_key=key,
                            start=0, end=4, admissibility='usable', need_eligible=True, feature_id='F'))
        reviews.append({'review_key': key, 'title': '', 'text': 'Fast but noisy.'})
    return {'aspects': aspects, 'reviews': reviews}


class Model:
    def __init__(self):
        self.payloads = []

    def chat_json(self, prompt, **kwargs):
        p = json.loads(prompt.split('\n')[-1])
        self.payloads.append(p)
        if p['task'] == 'voc_need_only':
            return {'groups': [{'existing_need_id': '', 'outcome': '有效完成任务且可接受',
                               'scene': '', 'user_object': '', 'open': None,
                               'evidence_ids': [a['evidence_id'] for a in p['evidence']]}]}
        assert p['task'] == 'voc_need_merge_review'
        return {'decisions': [{'id': s['id'], 'target': '', 'reason': '保守保留独立需求'} for s in p['subjects']]}


def engine(path, llm):
    return Analyzer(config(), path, 'US', catalog={}, llm=llm, log=lambda _: None)


def test_real_batches_parallel_and_full_coverage(tmp_path):
    class Concurrent(Model):
        lock = threading.Lock()
        gate = threading.Event()
        active = peak = 0

        def chat_json(self, prompt, **kwargs):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active >= 2:
                    self.gate.set()
            assert self.gate.wait(5), 'Need grouping did not run in parallel'
            try:
                return super().chat_json(prompt, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1
    llm, extracted = Concurrent(), data()
    a = engine(tmp_path, llm)
    needs, opens = synthesize(a, extracted)
    calls = [p for p in llm.payloads if p['task'] == 'voc_need_only']
    assert llm.peak >= 2
    assert len(calls) == 4 and all(len(p['evidence']) == 24 for p in calls)
    assert all(len(p['catalog']) <= 48 for p in calls)
    assert len(needs) == 1 and not opens
    assert {r for n in needs for r in n['evidence_ids']} == {a['evidence_id'] for a in extracted['aspects']}
    for p in calls:
        assert len((PROMPT + json.dumps(p, ensure_ascii=False)).encode()) <= 48000
        assert all(e['evidence_id'].startswith('E') for e in p['evidence'])


def test_short_ids_and_duplicate_unknown_refs_are_rejected():
    node = {'outcome': '结果', 'scene': '', 'user_object': ''}
    ident = node_id(node)
    response = {'groups': [dict(node, existing_need_id='C1', evidence_ids=['E1'], open=None)]}
    result = canonical_groups(response, {'E1': 'long-hash'}, {'C1': ident}, {ident: node})
    assert result[0]['evidence_ids'] == ['long-hash'] and result[0]['need_id'] == ident
    for refs in [['E2'], ['E1', 'E1'], []]:
        bad = copy.deepcopy(response)
        bad['groups'][0]['evidence_ids'] = refs
        with pytest.raises(ValueError):
            canonical_groups(bad, {'E1': 'long-hash'}, {'C1': ident}, {ident: node})
    response['groups'][0]['existing_need_id'] = 'C999'
    with pytest.raises(ValueError, match='未知目录'):
        canonical_groups(response, {'E1': 'long-hash'}, {'C1': ident}, {ident: node})


def test_scene_or_object_difference_never_inherits_catalog(tmp_path):
    node = {'outcome': '结果', 'scene': '', 'user_object': '成人'}
    ident = node_id(node)
    response = {'groups': [dict(node, user_object='儿童', existing_need_id='C1', evidence_ids=['E1'], open=None)]}
    groups = canonical_groups(response, {'E1': 'evidence'}, {'C1': ident}, {ident: node})
    assert groups[0]['need_id'] != ident and groups[0]['user_object'] == '儿童'


def test_failed_first_batch_defers_until_other_batches_finish(tmp_path):
    class Flaky(Model):
        failed = False
        order = []

        def chat_json(self, prompt, **kwargs):
            p = json.loads(prompt.split('\n')[-1])
            if p['task'] == 'voc_need_only':
                self.order.append([a['review_key'] for a in p['evidence']])
                if p['evidence'][0]['review_key'] == 'US::R0' and not self.failed:
                    self.failed = True
                    raise LLMError('temporary network error')
            return super().chat_json(prompt, **kwargs)
    llm = Flaky()
    a = engine(tmp_path, llm)
    a.config.set('VOC_LLM_WORKERS', 1)
    needs, _ = synthesize(a, data(72))
    assert len(needs[0]['evidence_ids']) == 72
    assert [p[0] for p in llm.order] == ['US::R0', 'US::R24', 'US::R48', 'US::R0']


def test_resume_uses_evidence_receipts_after_batch_and_worker_change(tmp_path):
    class Blocked(Model):
        block = True
        def chat_json(self, prompt, **kwargs):
            p = json.loads(prompt.split('\n')[-1])
            if self.block and p['task'] == 'voc_need_only' and p['evidence'][0]['review_key'] == 'US::R0':
                raise LLMError('connection lost')
            return super().chat_json(prompt, **kwargs)
    llm = Blocked()
    a = engine(tmp_path, llm)
    with pytest.raises(RuntimeError, match='待重试'):
        synthesize(a, data(72))
    assert len(NeedStore(a, data(72)).assignments) == 48
    llm.block = False
    before = len(llm.payloads)
    a = engine(tmp_path, llm)
    a.config.set('VOC_NEED_BATCH', 12)
    a.config.set('VOC_LLM_WORKERS', 1)
    a.config.set('VOC_NEED_CATALOG_LIMIT', 4)
    synthesize(a, data(72))
    calls = [p for p in llm.payloads[before:] if p['task'] == 'voc_need_only']
    assert len(calls) == 2
    assert {e['review_key'] for p in calls for e in p['evidence']} == {f'US::R{i}' for i in range(24)}


def test_legacy_successes_migrate_without_any_model_calls(tmp_path):
    class Never:
        def chat_json(self, *args, **kwargs):
            pytest.fail('migration must not call the model')
    a, extracted = engine(tmp_path, Never()), data(80)
    catalog, events = {}, []
    for start in range(0, 80, 40):
        batch = eligible(extracted)[start:start + 40]
        aliases = {f'C{i}': n for i, n in enumerate(catalog, 1)}
        payload = {'catalog': [{'need_id': alias, **{k: catalog[n][k] for k in ('outcome', 'scene', 'user_object')}}
                               for alias, n in aliases.items()], 'evidence': batch,
                   'review_context': review_context(extracted, batch)}
        key = digest([VERSION, NEED_ONLY, a.config.get('LLM_MODEL'), a.config.get('LLM_BASE_URL'), payload])
        n = dict(outcome='有效完成任务且可接受', scene='', user_object='')
        ident = node_id(n)
        # Old cache converts the short catalog alias into the actual Need ID.
        response = {'groups': [dict(n, existing_need_id=ident if catalog else '', open=None,
                                   evidence_ids=[e['evidence_id'] for e in batch])]}
        save(tmp_path / ('Need归并-' + key + '.json'), response)
        catalog.setdefault(ident, n)
        events.append({'task_id': digest(batch), 'status': 'complete'})
    save(tmp_path / 'Need归并-任务队列.json', {'events': events})
    store = NeedStore(a, extracted)
    import_legacy(a, extracted, store)
    assert len(store.assignments) == 80 and store.metrics['legacy_batches'] == 2
    needs, _ = synthesize(a, extracted)
    assert len(needs) == 1 and len(needs[0]['evidence_ids']) == 80


def test_context_and_model_changes_invalidate_receipts(tmp_path):
    a, extracted = engine(tmp_path, Model()), data(24)
    synthesize(a, copy.deepcopy(extracted))
    assert len(NeedStore(a, extracted).assignments) == 24
    extracted['reviews'][0]['text'] += ' Important update.'
    assert not NeedStore(a, extracted).assignments
    a.config.set('LLM_MODEL', 'other-model')
    assert not NeedStore(a, data(24)).assignments


def test_huge_catalog_is_trimmed_before_evidence_batch(tmp_path):
    a, extracted = engine(tmp_path, Model()), data(24)
    store = NeedStore(a, extracted)
    for i in range(300):
        node = dict(outcome='有效完成任务且可接受' + str(i) + '边界' * 30, scene='', user_object='')
        ident = node_id(node)
        store.nodes[ident] = node
        store.index.add(ident, node)
    payload, aliases, cats, _ = prepare_batch(store.candidates, store, ContextIndex(extracted), 48000, 128)
    assert len(aliases) == 24 and len(cats) < 128
    assert len((PROMPT + json.dumps(payload, ensure_ascii=False)).encode()) + 2048 <= 48000


def test_oversized_single_quote_is_not_sent_or_truncated(tmp_path):
    a, extracted = engine(tmp_path, Model()), data(1)
    extracted['aspects'][0]['quote'] = '超长原句' * 20000
    with pytest.raises(RuntimeError, match='待重试'):
        synthesize(a, extracted)
    assert not a.llm.payloads and extracted['aspects'][0]['quote'].endswith('超长原句')


def test_unpublished_receipt_is_recovered_without_paid_calls(tmp_path, monkeypatch):
    from tools.voc import storage
    a, extracted = engine(tmp_path, Model()), data(24)
    original = storage.atomic_replace
    def blocked(source, target):
        if Path(target).parent.parent.name == 'Need逐批断点':
            raise PermissionError('receipt locked')
        return original(source, target)
    monkeypatch.setattr(storage, 'atomic_replace', blocked)
    with pytest.raises(PermissionError):
        synthesize(a, copy.deepcopy(extracted))
    before = len(a.llm.payloads)
    monkeypatch.setattr(storage, 'atomic_replace', original)
    synthesize(a, extracted)
    assert len(a.llm.payloads) == before


def test_independent_semantic_review_merges_only_approved_same_scope(tmp_path):
    class MergeModel(Model):
        def chat_json(self, prompt, **kwargs):
            p = json.loads(prompt.split('\n')[-1])
            assert p['task'] == 'voc_need_merge_review'
            self.payloads.append(p)
            return {'decisions': [{'id': s['id'], 'target': s['candidates'][0]['id'], 'reason': '同场景同对象的同义结果'}
                                  for s in p['subjects']]}
    a, extracted = engine(tmp_path, MergeModel()), data(3)
    store = NeedStore(a, extracted)
    for i, (outcome, obj) in enumerate([('有效安静工作', ''), ('安静高效运行', ''), ('安静工作', '儿童')]):
        n = dict(outcome=outcome, scene='', user_object=obj, evidence_ids=[f'evidence-{i:04d}'])
        store.commit([dict(n, need_id=node_id(n))])
    mapping = merge_needs(a, store, 48000)
    assert len(set(mapping.values())) == 2
    assert all(not c['user_object'] for p in a.llm.payloads for s in p['subjects'] for c in s['candidates'])


def test_conflicting_or_corrupt_receipts_do_not_replace_assignments(tmp_path):
    a, extracted = engine(tmp_path, Model()), data(1)
    store = NeedStore(a, extracted)
    n = dict(outcome='结果A', scene='', user_object='', evidence_ids=['evidence-0000'])
    store.commit([dict(n, need_id=node_id(n))])
    n['outcome'] = '结果B'
    with pytest.raises(ValueError, match='冲突'):
        store.commit([dict(n, need_id=node_id(n))])
    assert len(list(store.directory.glob('*.json'))) == 1
    save(store.directory / 'corrupt.json', {'fingerprint': store.fp, 'groups': [], 'groups_digest': 'bad'})
    assert len(NeedStore(a, extracted).assignments) == 1


def test_model_options_change_cannot_reuse_old_raw_request_cache(tmp_path):
    a, extracted = engine(tmp_path, Model()), data(24)
    synthesize(a, copy.deepcopy(extracted))
    before = len(a.llm.payloads)
    a.config.set('LLM_EXTRA_BODY', {'test_option': 'changed'})
    synthesize(a, copy.deepcopy(extracted))
    assert len(a.llm.payloads) == before + 1


def test_semantic_review_failure_does_not_repeat_grouping_on_resume(tmp_path):
    class ReviewFails(Model):
        block = True
        def chat_json(self, prompt, **kwargs):
            p = json.loads(prompt.split('\n')[-1])
            if p['task'] == 'voc_need_only':
                self.payloads.append(p)
                # Similar phrases from separate independent batches require a review.
                suffix = p['evidence'][0]['review_key']
                return {'groups': [{'existing_need_id': '', 'outcome': '安静有效工作' + suffix,
                                   'scene': '', 'user_object': '', 'open': None,
                                   'evidence_ids': [e['evidence_id'] for e in p['evidence']]}]}
            if self.block:
                raise LLMError('review unavailable')
            return super().chat_json(prompt, **kwargs)
    a = engine(tmp_path, ReviewFails())
    with pytest.raises(RuntimeError, match='Need同义复核'):
        synthesize(a, data(48))
    before = len([p for p in a.llm.payloads if p['task'] == 'voc_need_only'])
    a.llm.block = False
    synthesize(a, data(48))
    assert len([p for p in a.llm.payloads if p['task'] == 'voc_need_only']) == before


def test_single_oversized_merge_definition_never_sends_unbounded_prompt(tmp_path):
    a, extracted = engine(tmp_path, Model()), data(2)
    store = NeedStore(a, extracted)
    for i in range(2):
        n = dict(outcome='相同结果' * 10000 + str(i), scene='', user_object='', evidence_ids=[f'evidence-{i:04d}'])
        store.commit([dict(n, need_id=node_id(n))])
    with pytest.raises(RuntimeError, match='Need同义复核'):
        merge_needs(a, store, 48000)
    assert not a.llm.payloads
