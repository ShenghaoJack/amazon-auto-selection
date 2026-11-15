from pathlib import Path

import pytest

from tools.attr_stability.export_state import export_operation, export_status, paths
from tools.attr_stability.spec_io import write_json_atomic
from tests.test_step18_workspace import workspace as workspace_factory
from tests.test_webui_run_override_leak import server as server_factory, CAT


@pytest.fixture
def workspace_case(tmp_path):
    return workspace_factory.__wrapped__(tmp_path)


@pytest.fixture
def server_case(ws_tmp, monkeypatch):
    return server_factory.__wrapped__(ws_tmp, monkeypatch)


def test_active_status_blocks_duplicate_and_releases_on_success(tmp_path):
    with export_operation(tmp_path, 'V1'):
        assert export_status(tmp_path)['status'] == 'running'
        with pytest.raises(ValueError, match='正在导出'):
            with export_operation(tmp_path, 'V2'):
                pytest.fail('duplicate export entered')
    assert export_status(tmp_path)['status'] == 'complete'


def test_failed_export_preserves_failure_and_can_retry(tmp_path):
    with pytest.raises(OSError):
        with export_operation(tmp_path, 'V1'):
            raise OSError('disk full')
    assert export_status(tmp_path)['status'] == 'failed'
    assert export_status(tmp_path)['error'] == 'disk full'
    with export_operation(tmp_path, 'V1'):
        pass
    assert export_status(tmp_path)['status'] == 'complete'


def test_unlocked_running_marker_is_interrupted_not_forever_busy(tmp_path):
    lock, state = paths(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b'\0')
    write_json_atomic(state, {'status': 'running', 'version': 'V1'})
    assert export_status(tmp_path)['status'] == 'interrupted'
    with export_operation(tmp_path, 'V1'):
        assert export_status(tmp_path)['status'] == 'running'


def test_real_workspace_export_has_observable_lifecycle(workspace_case, monkeypatch):
    workspace = workspace_case
    state = workspace.open()
    actual = workspace._export
    def observed(expected):
        assert export_status(workspace.root)['status'] == 'running'
        return actual(expected)
    monkeypatch.setattr(workspace, '_export', observed)
    workspace.export(state['version'])
    assert export_status(workspace.root)['status'] == 'complete'


def test_run_api_refuses_export_in_other_window_but_other_category_can_run(server_case, ws_tmp):
    server = server_case
    root = Path(ws_tmp) / 'data' / CAT
    handler = server.Handler.__new__(server.Handler)
    with export_operation(root, 'V1'):
        with pytest.raises(server.UserFacingError, match='正在导出'):
            handler._api_run({'config_file': f'data/{CAT}/config.yaml', 'mode': 'step', 'step': 18})
        assert not server._test_runner.started
        assert server.run_start_guard('Other-US-202609', str(root.parent)) == []
    assert handler._api_run({'config_file': f'data/{CAT}/config.yaml', 'mode': 'step', 'step': 18})['ok']
