"""归档同步 (3.4/FR-17)。

- 每步产物先写实时数据目录, 再即时同步归档(每步完成后)
- 手动完整快照: 新建带时间戳归档, 按 00-19 步骤目录归位
"""
from __future__ import annotations

import os
import shutil

from .paths import Paths


def sync_step_files(step_number: int, files: list[str], snapshot: str, paths: Paths) -> None:
    """把该步的平铺产物复制进归档步骤目录。"""
    target = paths.archive_step_dir(snapshot, step_number)
    for f in files:
        if f and os.path.exists(f):
            try:
                shutil.copy2(f, os.path.join(target, os.path.basename(f)))
            except OSError:
                continue


def sync_step_dir(step_number: int, source_dir: str, snapshot: str, paths: Paths) -> None:
    """把该步的独立子目录整体复制进归档步骤目录。"""
    if not source_dir or not os.path.isdir(source_dir):
        return
    target = paths.archive_step_dir(snapshot, step_number)
    if step_number == 19 and os.path.basename(os.path.normpath(source_dir)) == "19_VOC报告":
        # VOC source responses and multi-aspect/Need/Open outputs live below the
        # step root. Preserve the whole tree for automatic and manual snapshots.
        shutil.copytree(source_dir, target, dirs_exist_ok=True)
        return
    if step_number == 18 and os.path.basename(os.path.normpath(source_dir)) == "版本数据":
        # Keep immutable versions/proposals/jobs and the current pointer together.
        # Locks are process coordination files and can be exclusively held on
        # Windows. They are recreated on demand, not part of the snapshot.
        shutil.copytree(source_dir, os.path.join(target, "版本数据"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.lock"))
        return
    for name in os.listdir(source_dir):
        src = os.path.join(source_dir, name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(target, name))
            except OSError:
                continue


def create_snapshot(paths: Paths, step_syncs: list[tuple[int, list[str], list[str]]]) -> str:
    """手动完整快照。

    step_syncs: [(步骤号, 平铺文件列表, 独立子目录列表), ...]
    """
    snapshot = paths.new_snapshot_dir()
    for number, files, dirs in step_syncs:
        sync_step_files(number, files, snapshot, paths)
        for d in dirs:
            sync_step_dir(number, d, snapshot, paths)
    return snapshot
