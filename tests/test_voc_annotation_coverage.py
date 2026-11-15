import pytest

from tests.test_voc_opportunity import config, row
from tools.voc import annotate as a


class Response:
    def __init__(self, data):
        self.data = data

    def chat_json(self, *args, **kwargs):
        return self.data


@pytest.mark.parametrize('wrap', [lambda x: x, lambda x: {'results': x},
                                  lambda x: {'items': x}, lambda x: {'data': x}])
def test_array_and_known_object_envelopes_preserve_comment_indices(wrap):
    got = a.classify_batch(Response(wrap([{'i': '1'}, {'i': 0}])), [row('a'), row('b')], 0)
    assert [x['i'] for x in got] == [1, 0]


@pytest.mark.parametrize('data', [[], [{'i': 0}, {'i': 0}], [{'i': 1}, {'i': 2}],
                                 [{'i': 0}, {'i': 1}, {'i': 9}],
                                 [{'i': False}, {'i': 1}], [{'i': 0.5}, {'i': 1}],
                                 {'unknown': [{'i': 0}, {'i': 1}]}])
def test_missing_duplicate_and_invalid_indices_reject_whole_response(data):
    with pytest.raises(a.AnnotationCoverageError) as error:
        a.classify_batch(Response(data), [row('a'), row('b')], 0)
    assert error.value.response == data


def test_invalid_batches_split_and_resume_success_without_cross_variant_rows(tmp_path, monkeypatch):
    calls = []
    rows = [row(str(i), str(i), variant_asin='V' if i < 4 else 'W') for i in range(5)]
    monkeypatch.setattr(a, 'LLMClient', lambda *args, **kwargs: object())

    def classify(client, batch, number, **kwargs):
        calls.append([r['review_id'] for r in batch])
        assert len({r['variant_asin'] for r in batch}) == 1
        if len(batch) > 1:
            raise a.AnnotationCoverageError('duplicate', [{'i': 0}, {'i': 0}])
        if batch[0]['review_id'] == '2':
            return []
        return [{'i': 0, 'quote': batch[0]['review_body']}]

    monkeypatch.setattr(a, 'classify_batch', classify)
    cfg = config()
    cfg.set('VOC_RETRY_ROUNDS', 1)
    cache = str(tmp_path / 'cache.jsonl')
    labels, stats = a.run_annotation(rows, cfg, batch=4, workers=1, cache_path=cache, log=lambda _: None)
    assert stats['failed_reviews'] == 1
    assert [bool(x) for x in labels] == [True, True, False, True, True]
    assert list((tmp_path / '兼容标注失败').glob('*.json'))
    calls.clear()

    def success(client, batch, number, **kwargs):
        calls.extend(r['review_id'] for r in batch)
        return [{'i': i, 'quote': r['review_body']} for i, r in enumerate(batch)]

    monkeypatch.setattr(a, 'classify_batch', success)
    labels, stats = a.run_annotation(rows, cfg, batch=4, workers=1, cache_path=cache, log=lambda _: None)
    assert calls == ['2']
    assert stats['failed_reviews'] == 0
    assert [x['quote'] for x in labels] == [str(i) for i in range(5)]
