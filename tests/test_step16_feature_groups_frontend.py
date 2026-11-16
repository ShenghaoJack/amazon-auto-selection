import shutil
import subprocess
from pathlib import Path

import pytest


def test_group_render_edit_and_delete_cancel():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('step16_feature_groups_frontend.cjs'))],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
