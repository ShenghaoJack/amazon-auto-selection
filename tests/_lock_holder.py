# -*- coding: utf-8 -*-
"""测试辅助: 持有一个类目的运行锁并保持 N 秒(供 tests/test_run_lock.py 起真子进程验证互斥/接管)。

用法: python tests/_lock_holder.py <cat_root> <hold_seconds>
行为: 获取锁 -> 写 <cat_root>/holder-ready.txt(含 pid) -> 保持 -> 到点释放退出。
注意: 被 kill 时由**内核**释放锁(这正是 B1 要验证的"自动接管")。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.run_lock import acquire_run_lock  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1])
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    lock = acquire_run_lock(str(root), "holder(测试)")
    (root / "holder-ready.txt").write_text(str(__import__("os").getpid()), encoding="utf-8")
    try:
        time.sleep(hold)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
