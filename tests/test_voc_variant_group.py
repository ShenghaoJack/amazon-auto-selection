import json

import pytest

from tests.test_voc_opportunity import FakeLLM, config, row
from tools.voc.opportunity import Analyzer, prepare_reviews


class RecordingLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.batches = []

    def chat_json(self, prompt, **kwargs):
        payload = json.loads(prompt.split('\n')[-1])
        if isinstance(payload, list):
            self.batches.append(payload)
        return super().chat_json(prompt, **kwargs)


def test_interleaved_variants_never_mix_and_writeback_is_local(tmp_path):
    llm = RecordingLLM()
    a = Analyzer(config(), tmp_path, 'US', llm=llm, log=lambda _: None)
    rows = [row('A1', variant_asin='A'), row('B1', variant_asin='B'), row('A2', variant_asin='A')]
    a.finalize(rows)
    assert [[p['review_key'] for p in b] for b in llm.batches] == [['US::A1', 'US::A2'], ['US::B1']]
    tags = json.loads((tmp_path / '逐条评论分类.json').read_text(encoding='utf-8'))
    assert len(tags) == 3
    assert all(t['好评大类'] and t['痛点大类'] and t['text'] == 'Fast but noisy.' for t in tags)


def test_new_review_invalidates_only_its_group_and_attribution_move_invalidates(tmp_path):
    llm = RecordingLLM()
    a = Analyzer(config(), tmp_path, 'US', llm=llm, log=lambda _: None)
    rows = [row('A1', variant_asin='A'), row('B1', variant_asin='B')]
    a.extract(rows)
    llm.batches.clear()
    rows.append(row('A2', variant_asin='A'))
    a.extract(rows)
    assert len(llm.batches) == 1 and len(llm.batches[0]) == 2
    llm.batches.clear()
    rows[-1]['variant_asin'] = 'B'
    a.extract(rows)
    assert {b[0]['variant_asin'] for b in llm.batches} == {'B'}  # A1 reuses its original complete-group cache.
    assert len(llm.batches[0]) == 2


def test_unknown_attribution_is_never_grouped_by_request_and_split_preserves_all(tmp_path):
    llm = RecordingLLM()
    cfg = config()
    cfg.set('VOC_LLM_BATCH', 2)
    a = Analyzer(cfg, tmp_path, 'US', llm=llm, log=lambda _: None)
    rows = [row(f'R{i}', variant_asin='A') for i in range(5)]
    rows += [row(f'U{i}', source='sellersprite', variant_asin='REQUESTED') for i in range(2)]
    result = a.extract(rows)
    assert sorted(len(b) for b in llm.batches) == [1, 1, 1, 2, 2]
    assert all(len({p['extraction_group'] for p in b}) == 1 for b in llm.batches)
    assert len(result['coverage']) == 7 and all(c['status'] == 'reviewed' for c in result['coverage'])


def test_same_review_conflicting_variant_is_rejected():
    with pytest.raises(ValueError, match='归属冲突'):
        prepare_reviews([row(variant_asin='A'), row(variant_asin='B')], 'US')


def test_blank_variant_is_unknown_and_case_is_normalized():
    from tools.voc.opportunity import extraction_group
    aa = prepare_reviews([row('A', variant_asin='  '), row('B', variant_asin='  '), row('C', variant_asin=' abc ')], 'US')
    assert aa[0]['asin_role'] == 'unknown'
    assert extraction_group(aa[0]) != extraction_group(aa[1])
    assert extraction_group(aa[2]) == 'US::variant::ABC'
