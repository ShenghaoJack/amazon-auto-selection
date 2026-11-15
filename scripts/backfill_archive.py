"""一次性脚本: 把实时目录 0..N 已完成步骤的产物补同步进指定归档目录。

用于修复历史归档快照缺失早期步骤的问题(如 00-04 未进本轮归档)。
用法: python -X utf8 scripts/backfill_archive.py <快照目录名>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.orchestrator import PipelineRunner
from steps.registry import get_steps


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python backfill_archive.py <config.yaml> <快照目录绝对路径>")
        return 2
    cfg_path, snap = sys.argv[1], sys.argv[2]
    if not os.path.isdir(snap):
        print(f"快照目录不存在: {snap}")
        return 2
    cfg = Config.load(cfg_path)
    runner = PipelineRunner(cfg)
    runner._snapshot = snap
    steps = {s.number: s for s in get_steps()}
    done = [n for n in sorted(steps) if runner.state.is_finished(n)]
    if not done:
        print("没有已完成步骤需要补同步")
        return 0
    for num in done:
        runner._sync_after_step(steps[num])
        print(f"补同步: 步骤 {num:02d} {steps[num].title} -> {snap}")
    print(f"完成: 共补同步 {len(done)} 个已完成步骤")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
