"""Shared pytest fixtures for workspace tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_amz_crawl_lock_dir(tmp_path, monkeypatch):
    """隔离测试期亚马逊并发锁目录，避免与本机正在运行的实测/生产任务互斥。"""
    monkeypatch.setenv("PIPELINE_AMZ_LOCK_DIR", str(tmp_path / "amz_locks"))
