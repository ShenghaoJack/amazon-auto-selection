import json
import subprocess
import sys


def test_sqlite_checkpoint_survives_process_restart_for_two_threads(tmp_path):
    # This test belongs to the isolated workflow environment, not the legacy environment.
    outputs = []
    for phase in ('prepare', 'resume'):
        result = subprocess.run([sys.executable, '-m', 'workflow.checkpoint_smoke', '--directory', str(tmp_path), '--phase', phase],
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        assert result.returncode == 0, result.stderr
        outputs.append(json.loads(result.stdout))
    assert all(not row['standardized'] for row in outputs[0]['results'])
    assert all(row['standardized'] and row['prepared_calls'] == 1 for row in outputs[1]['results'])
