from pathlib import Path

import pytest

from core.config import Config
from core.orchestrator import PipelineRunner
from steps.step19_voc import Step19Voc


def test_step18_archive_skips_locks_but_keeps_versions(tmp_path, monkeypatch):
    from core.archive import sync_step_dir
    import shutil
    runner = PipelineRunner(Config({'DATA_ROOT': str(tmp_path), 'CAT': 'VOC-test',
                                    'LLM_PRICES_MODE': 'builtin'}), dry_run=False)
    source = Path(runner.paths.step_dir(18)) / '版本数据'
    (source / 'versions').mkdir(parents=True)
    (source / 'attribute-spec.lock').write_text('locked', encoding='utf-8')
    (source / 'versions' / 'v.json').write_text('version', encoding='utf-8')
    original = shutil.copy2
    def reject_lock(src, dst, *args, **kwargs):
        if str(src).endswith('.lock'):
            raise PermissionError('locked')
        return original(src, dst, *args, **kwargs)
    monkeypatch.setattr(shutil, 'copy2', reject_lock)
    snapshot = tmp_path / 'archive' / 'test'
    sync_step_dir(18, str(source), str(snapshot), runner.paths)
    copied = snapshot / '18_标准化' / '版本数据'
    assert (copied / 'versions' / 'v.json').read_text(encoding='utf-8') == 'version'
    assert not (copied / 'attribute-spec.lock').exists()


def setup_case(tmp_path):
    runner = PipelineRunner(Config({'DATA_ROOT': str(tmp_path), 'CAT': 'VOC-test',
                                    'LLM_PRICES_MODE': 'builtin'}), dry_run=False)
    live = Path(runner.paths.step_dir(19))
    files = {'VOC-test-VOC报告.xlsx': b'workbook',
             '1_采集原始数据/pangolinfo_raw.jsonl': b'{"raw":true}\n',
             '1_采集原始数据/pangolinfo_reviews.jsonl': b'{"review_id":"R1"}\n',
             '2_需求机会链/逐条评论分类.csv': '原文,Need,Open\n'.encode(),
             '2_需求机会链/候选Need.csv': b'need_id\nN1\n',
             '2_需求机会链/步骤18标准化交付-hash.xlsx': b'step18-source-snapshot',
             '2_需求机会链/已有解法与解决程度.csv': '解决程度\n待验证\n'.encode(),
             '2_需求机会链/实体与变体关联明细.csv': b'variant_asin,parent_asin,brand\nA,P,B\n',
             '2_需求机会链/变体Need完整机会链.csv': b'variant_asin,need_id\nA,N\n',
             '2_需求机会链/排除评论原文.json': b'[{"exclusion_reason":"variant_not_in_step18"}]',
             '2_需求机会链/已有解法核查-任务队列.json': b'{"pending":1}',
             '2_需求机会链/阶段断点.json': b'{"stages":{}}',
             '2_需求机会链/断点数据/snapshot.json': b'{"fingerprint":"valid"}',
             '2_需求机会链/引文修复记录/failure.json': b'{"model_response":{"parts":[]}}',
             '2_需求机会链/引文修复断点/checked.json': b'{"value":{"auto_repair":"v1"}}',
             '2_需求机会链/分类逐批断点/source/response.json': b'{"items":[],"fingerprint":"source"}',
             '2_需求机会链/分类提速状态.json': b'{"classified_aspects":4819}',
             '2_需求机会链/Need逐批断点/source/receipt.json': b'{"groups":[],"fingerprint":"source"}',
             '2_需求机会链/Need提速状态.json': b'{"completed_evidence":8183}',
             '2_需求机会链/Need同义复核结果.json': b'{"decisions":[]}',
             '1_采集原始数据/采集补试队列.json': b'{"pending_parents":[]}',
             '2_需求机会链/Open与待验证方案.csv': b'open_id\nO1\n',
             '2_需求机会链/chain.json': b'{"needs":[],"opportunities":[]}',
             '2_需求机会链/cache/response.json': b'{}'}
    for name, data in files.items():
        p = live / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return runner, live, files


def assert_copy(snapshot, files):
    for name, data in files.items():
        assert (Path(snapshot) / '19_VOC报告' / name).read_bytes() == data


def test_automatic_step_sync_preserves_sources_and_nested_outputs(tmp_path):
    runner, live, files = setup_case(tmp_path)
    runner._snapshot = str(tmp_path / 'archive/automatic')
    runner._sync_after_step(Step19Voc())
    assert_copy(runner._snapshot, files)
    updated = '新原文,新Need,新Open\n'.encode()
    (live / '2_需求机会链/逐条评论分类.csv').write_bytes(updated)
    files['2_需求机会链/逐条评论分类.csv'] = updated
    runner._sync_after_step(Step19Voc())
    assert_copy(runner._snapshot, files)


def test_manual_full_snapshot_preserves_nested_sources(tmp_path, monkeypatch):
    runner, _, files = setup_case(tmp_path)
    monkeypatch.setattr('core.orchestrator.get_steps', lambda: [Step19Voc()])
    snapshot = runner.snapshot_all()
    assert_copy(snapshot, files)


def test_voc_recursive_copy_failure_is_not_silently_skipped(tmp_path, monkeypatch):
    runner, _, _ = setup_case(tmp_path)
    runner._snapshot = str(tmp_path / 'archive/failed')
    def fail(*args, **kwargs):
        raise OSError('locked source')
    monkeypatch.setattr('core.archive.shutil.copytree', fail)
    with pytest.raises(OSError, match='locked source'):
        runner._sync_after_step(Step19Voc())
