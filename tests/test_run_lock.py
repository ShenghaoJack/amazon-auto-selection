# 单元测试: 运行锁(2026-09-14 起为 **OS 独占文件锁**, B1 方案)
#
# 背景: 旧实现只"写 pid 文件 + 查 pid 存活", 在"禁止子进程管道"的受限环境里 tasklist
#       捕获会失败 → 静默判"已死" → 锁形同虚设(双跑); 且有 pid 复用误判、残留锁需人工删。
# 现机制: 进程**持有 OS 独占文件锁**(Windows msvcrt.locking / POSIX fcntl.flock), 进程一死
#       内核自动释放 → 自动接管; release 只解锁**不删文件**(避免"删掉被持有的文件"导致双跑)。
#       文件锁不可用时才降级到 pid 三态 + 活跃度超时(TAKEOVER_MINUTES=30, 硬编码)。
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.run_lock import (
    TAKEOVER_MINUTES,
    _pid_alive,
    _pid_state,
    acquire_run_lock,
    probe_lock,
)

_HOLDER = Path(__file__).resolve().parent / "_lock_holder.py"


def test_pid_alive_reused_pid_judged_dead(tmp_path):
    """锁写入时刻早于该 pid 的真实启动时间 = pid 被复用(原进程已死) -> 判不存活。"""
    pid = os.getpid()
    lock_ts = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    assert _pid_alive(pid, lock_ts) is False


def test_pid_alive_normal_still_true(tmp_path):
    """锁写入时刻不早于进程启动(≈现在) -> 正常判存活(不误杀真运行)。"""
    pid = os.getpid()
    lock_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    assert _pid_alive(pid, lock_ts) is True
    future = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert _pid_alive(pid, future) is True       # 时钟截断容差内仍存活


def test_pid_state_is_tri_state_when_unavailable(monkeypatch):
    """"查不出存活"必须返回 None, 不能悄悄当成 False(旧实现就是这么丢保护的)。"""
    import core.run_lock as rl
    monkeypatch.setattr(rl, "_pid_exists", lambda pid: None)
    assert _pid_state(os.getpid()) is None
    monkeypatch.setattr(rl, "_pid_exists", lambda pid: False)
    assert _pid_state(os.getpid()) is False


def test_lock_acquire_release_keeps_file_and_frees_lock(tmp_path):
    """acquire→release: 文件**保留**(不再删除), 但锁已释放 -> 第二个实例能立刻拿到。"""
    root = tmp_path / "cat-root"
    lock = acquire_run_lock(str(root), "test")
    assert (root / ".run.lock").exists()
    assert lock.holds_os_lock is True
    lock.release()
    assert (root / ".run.lock").exists(), "release 不应删除锁文件(删了会让别人另建文件双跑)"
    again = acquire_run_lock(str(root), "second")
    assert again.holds_os_lock is True          # 锁已释放, 可再次获取
    again.release()


# ---------- OS 锁互斥(跨句柄 = 跨进程同一语义, 本环境可直接验证) ----------

def test_lock_is_exclusive_across_handles(tmp_path):
    """持锁期间**另一个句柄**锁同一区间会失败(Windows LockFile/POSIX flock 的互斥语义)。

    这等价于"第二个进程拿不到锁"; 用同进程第二个句柄验证, 是为了在**无法派生子进程**的
    受限环境里也能把互斥语义钉住(子进程版本见下面两个用例, 不可派生时自动 skip)。
    """
    from core.run_lock import _try_lock, _unlock
    root = tmp_path / "cat-root"
    lock = acquire_run_lock(str(root), "first")
    try:
        with open(root / ".run.lock", "r+b") as fh2:
            assert _try_lock(fh2) is False, "持锁期间第二个句柄不应拿到锁"
        alive, started, unknown = probe_lock(str(root))
        assert alive is True and unknown is False and started
    finally:
        lock.release()
    with open(root / ".run.lock", "r+b") as fh3:
        assert _try_lock(fh3) is True, "释放后应能再次获取"
        _unlock(fh3)


def test_probe_reports_stale_file_as_not_alive(tmp_path):
    """锁文件残留但没人持有 OS 锁 -> probe 判"无存活进程"(旧实现只能靠查 pid 猜)。"""
    root = tmp_path / "cat-root"
    root.mkdir(parents=True)
    (root / ".run.lock").write_text("\n999999\nrun 假类目\n2020-01-01 00:00:00\n", encoding="utf-8")
    alive, started, unknown = probe_lock(str(root))
    assert alive is False and unknown is False and started == "2020-01-01 00:00:00"


# ---------- 真子进程: 持锁互斥 + 强杀后自动接管(需本机可派生 python 子进程) ----------

def _can_spawn_python() -> bool:
    """本环境能否派生 python 子进程(受限沙箱会以 0xC0000142 STATUS_DLL_INIT_FAILED 立刻失败)。"""
    try:
        p = subprocess.run([sys.executable, "-c", "print(1)"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def holder_supported():
    if not _can_spawn_python():
        pytest.skip("本环境无法派生子进程(0xC0000142 STATUS_DLL_INIT_FAILED), "
                    "该用例需在普通 Windows 会话/真实机器上运行")
    return True


def _spawn_holder(root: Path, hold_sec: float = 30.0) -> subprocess.Popen:
    # 受限环境禁止子进程管道: 输出重定向到**文件**(不是 PIPE), 便于失败时留证
    log = root / "holder.out"
    root.mkdir(parents=True, exist_ok=True)
    fh = open(log, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(_HOLDER), str(root), str(hold_sec)],
        stdout=fh, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=str(_HOLDER.parent.parent),
    )
    proc._log_fh = fh  # type: ignore[attr-defined]
    return proc


def _wait_ready(root: Path, timeout: float = 30.0) -> bool:
    flag = root / "holder-ready.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if flag.exists():
            return True
        time.sleep(0.1)
    return False


def test_child_holding_lock_blocks_second_instance(tmp_path, holder_supported):
    root = tmp_path / "cat-root"
    proc = _spawn_holder(root, 30)
    try:
        assert _wait_ready(root), f"子进程未能就绪(见 {root / 'holder.out'})"
        alive, started, unknown = probe_lock(str(root))
        assert alive is True and unknown is False      # 有人持锁 -> 判存活
        with pytest.raises(RuntimeError) as ei:
            acquire_run_lock(str(root), "second")
        assert "已有流水线进程在运行" in str(ei.value)
    finally:
        proc.kill()
        proc.wait(timeout=20)
        proc._log_fh.close()  # type: ignore[attr-defined]


def test_killed_holder_is_taken_over_immediately(tmp_path, holder_supported):
    """子进程被强杀 -> 内核释放 OS 锁 -> 新实例**立即**接管(没有 30 分钟等待, 无需人工删锁)。"""
    root = tmp_path / "cat-root"
    proc = _spawn_holder(root, 60)
    assert _wait_ready(root), f"子进程未能就绪(见 {root / 'holder.out'})"
    proc.kill()
    proc.wait(timeout=20)
    proc._log_fh.close()  # type: ignore[attr-defined]
    alive, _started, unknown = probe_lock(str(root))
    assert alive is False and unknown is False         # 残留文件被正确识别为"无人持锁"
    lock = acquire_run_lock(str(root), "after-kill")   # 立即接管, 不抛异常
    assert lock.holds_os_lock is True
    lock.release()


# ---------- 降级路径(文件锁不可用): pid 三态 + 活跃度超时(硬编码 30 分钟) ----------

def _degrade(monkeypatch):
    import core.run_lock as rl
    monkeypatch.setattr(rl, "_try_lock", lambda fh: None)     # 模拟"文件系统不支持字节锁"


def test_degraded_unknown_fresh_lock_refuses(tmp_path, monkeypatch, capsys):
    """降级 + pid 查不出 + 锁很新 -> 保守拒绝, 并提示最晚何时可自动接管。"""
    import core.run_lock as rl
    _degrade(monkeypatch)
    monkeypatch.setattr(rl, "_pid_state", lambda pid, started=None: None)
    root = tmp_path / "cat-root"
    root.mkdir(parents=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (root / ".run.lock").write_text(f"\n12345\nrun X\n{now}\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as ei:
        acquire_run_lock(str(root), "second")
    msg = str(ei.value)
    assert "无法判定" in msg and f"{TAKEOVER_MINUTES} 分钟" in msg and "可自动接管" in msg


def test_degraded_unknown_stale_lock_takes_over_with_warn(tmp_path, monkeypatch, capsys):
    """降级 + pid 查不出 + 活跃度 ≥ 30 分钟 -> 自动接管并 WARN(无需人工删锁)。"""
    import core.run_lock as rl
    _degrade(monkeypatch)
    monkeypatch.setattr(rl, "_pid_state", lambda pid, started=None: None)
    root = tmp_path / "cat-root"
    root.mkdir(parents=True)
    lock = root / ".run.lock"
    old = (datetime.now() - timedelta(minutes=TAKEOVER_MINUTES + 5)).strftime("%Y-%m-%d %H:%M:%S")
    lock.write_text(f"\n12345\nrun X\n{old}\n", encoding="utf-8")
    old_ts = (datetime.now() - timedelta(minutes=TAKEOVER_MINUTES + 5)).timestamp()
    os.utime(lock, (old_ts, old_ts))
    got = acquire_run_lock(str(root), "after-stale")
    assert got.holds_os_lock is False            # 降级路径没有 OS 锁可持
    out = capsys.readouterr().out
    assert "自动接管" in out and "若该进程其实仍在运行" in out


def test_degraded_pid_alive_refuses(tmp_path, monkeypatch):
    """降级 + pid 确定存活 -> 拒绝(不依赖超时)。"""
    import core.run_lock as rl
    _degrade(monkeypatch)
    monkeypatch.setattr(rl, "_pid_state", lambda pid, started=None: True)
    root = tmp_path / "cat-root"
    root.mkdir(parents=True)
    (root / ".run.lock").write_text("\n12345\nrun X\n2026-09-14 10:00:00\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as ei:
        acquire_run_lock(str(root), "second")
    assert "按 pid 判定为存活" in str(ei.value)
