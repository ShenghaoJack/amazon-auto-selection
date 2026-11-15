import json


def test_phase_usage_uses_current_process_and_marks_missing(tmp_path):
    from tools.voc.phase_usage import summarize
    path = tmp_path / 'usage.jsonl'
    base = {'type': 'call', 'step': 19, 'run_id': 'r', 'pid': 12, 'purpose': 'VOC:方面分类',
            'in_hit': 10, 'in_miss': 20, 'out': 5, 'cost_cny': 0.01, 'priced': True}
    rows = [base, {**base, 'purpose': 'chat_json', 'usage_missing': True, 'priced': False},
            {**base, 'pid': 13}]
    path.write_text('\n'.join(json.dumps(x) for x in rows), encoding='utf-8')
    report = summarize(path, 'r', 12)
    assert report['phases']['方面分类']['input_tokens'] == 30
    assert report['phases']['其他或未标记']['missing_usage'] == 1
    assert sum(p['calls'] for p in report['phases'].values()) == 2
