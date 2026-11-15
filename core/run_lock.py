# -*- coding: utf-8 -*-
"""跨进程"每类目单实例运行锁"(2026-09-08 引入; 2026-09-14 改为 OS 独占文件锁, 即 B1)。

背景: web RUNNER 只防"自己启动的子进程", CLI/其它入口起的 python 不可见,
失败重跑/冻结僵尸会叠成多个进程共写同一类目文件。
此锁按 `data/<CAT>/.run.lock` 判定"该类目是否已有存活运行进程"——
**不同类目互不干扰(为将来的多类目并行留余地), 同类目第二实例启动即拒绝**。

## 主机制(2026-09-14 起): 进程**持有 OS 独占文件锁**, 而不是"查 pid 还在不在"

    acquire: 打开 .run.lock -> 非阻塞独占锁 1 字节(Windows msvcrt.locking/LK_NBLCK;
             POSIX fcntl.flock) -> 拿到就把 pid/入口/时间写进去, 并**持有句柄直到进程结束**
    release: 解锁 + 关句柄(**不删除文件**)

为什么这样更可靠:
  · 进程一死(正常退出/崩溃/SIGKILL/断电), **内核自动释放锁** → 下一个进程立刻拿到,
    **不存在"残留锁需要人工删除"**;
  · 不再依赖"能否查询 pid 存活"(旧实现 Windows 只有 `tasklist` 且必须捕获子进程输出,
    管道被禁的环境里会静默返回"已死" → 锁形同虚设 → 双跑)。实测沙箱/受限环境复现过;
  · 不再有 pid 复用误判(pid 被系统回收后分给别的进程)。
  · **进程被挂起/机器休眠期间锁不会释放**(持锁进程确实还在), 这是正确行为。

⚠️ 不要手工删除 `.run.lock`: POSIX 上"删掉被持有的文件"会让新进程另建新文件并加锁成功
   → 双跑(Windows 上文件被占用删不掉, 反而安全)。所以 release() 只解锁不删文件。

## 降级路径(仅当文件系统不支持字节锁: 网络盘 SMB/NFS、受限挂载)

    ① 先按 pid 判活(psutil 优先, 其次 tasklist 捕获, 非 Windows 用 os.kill):
       有确定答案就用确定答案(活着→拒绝; 已死→接管);
    ② 只有"无法判定"时才用**活跃度超时**: 活跃度 = max(锁文件 mtime, 锁内记录时间,
       pipeline-progress.log mtime, step-status.txt mtime);
         未达阈值 → 拒绝启动(提示最晚何时可自动接管);
         达到阈值 → 判定残留, **自动接管 + WARN**。
    阈值 `TAKEOVER_MINUTES = 30`(**按用户 2026-09-14 决定硬编码在程序里, 不提供配置键**):
    正常运行时每步都会持续写日志, 30 分钟没有任何新证据在"又查不出存活"的前提下足以判残留;
    设太大等于要人工删锁, 设太小(几分钟)会误伤长跑步骤(实测步骤10 曾连续跑 6h44m,
    轮间冷却 60s、还有备用账号窗口挂起, 几分钟静默是正常的)。

## 与旧锁文件的兼容

旧版本只写文件、不持有 OS 锁。因此"看起来空闲(拿到 OS 锁)"时**再做一次 psutil 复核**:
若文件里记录的 pid 确实存活(且其创建时间不晚于锁写入时间, 即非复用), 仍判"有人",
以免升级瞬间与旧进程双跑; 复核不确定则按"空闲"处理(OS 锁是权威)。

用法(在 main.py run/step 入口):
    lock = acquire_run_lock(cat_root, f"{func} {cat}")   # 已有存活进程 → RuntimeError
    try:
        ... 运行 ...
    finally:
        lock.release()
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

# 降级路径的"自动接管"阈值(分钟): 硬编码, 不提供配置键(用户 2026-09-14 决定)
TAKEOVER_MINUTES = 30
# 抢锁重试(消掉"探测/启动"之间的微小竞态): 次数 × 间隔秒
_LOCK_TRIES = 3
_LOCK_RETRY_SLEEP = 0.2
# 锁定位置: **远高于正文**的固定偏移(1MiB)。
# 为什么不用第 0 字节: Windows 的 LockFile 是**强制锁**, 锁住某字节后其它句柄连**读**都会被拒
#   (实测: 锁 byte0 时, 另一个句柄 read 整个文件直接 PermissionError) → 那样"谁在跑"的
#   pid/入口/时间就永远读不出来(错误提示与看板都拿不到诊断信息)。
#   锁一个正文永远够不着的高偏移, 正文就始终可被其它进程正常读取; 锁的互斥语义不变。
_LOCK_OFFSET = 1 << 20


def _warn(msg: str, log=None) -> None:
    line = f"[单实例锁] {msg}"
    try:
        (log(line) if log else print(line, flush=True))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- pid 判定(降级路径 + 诊断)

def _pid_exists(pid: int) -> bool | None:
    """pid 是否存活: True/False; **None = 本环境查不出来**(与"已死"区分开)。

    2026-09-14: 优先 psutil(纯进程查询, 不起子进程/不开管道); 其次 Windows `tasklist`
    字节流捕获(旧实现, 在"禁止子进程管道"的环境里会抛 PermissionError → 返回 None 而不是
    False, 避免把"查不出"当成"已死"); 非 Windows 用 os.kill(pid, 0)。
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # noqa: WPS433
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return None
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout or b""
            text = out.decode("utf-8", errors="replace")
            return str(pid) in text
        os.kill(pid, 0)
        return True
    except PermissionError:
        return None
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return None


def _pid_state(pid: int, started_before: str | None = None) -> bool | None:
    """三态判活: True=确定存活 / False=确定已死 / None=无法判定。

    含 2026-09-09 的 pid 复用校验: 若该 pid 的**真实创建时间晚于锁写入时间**, 说明 pid 被
    系统回收后分给了别的进程(原流水线早已退出) → 判死。
    """
    exists = _pid_exists(pid)
    if exists is None or exists is False:
        return exists
    if started_before:
        try:
            import psutil  # noqa: WPS433
            created = psutil.Process(pid).create_time()
            lock_ts = datetime.strptime(started_before.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
            if created > lock_ts + 1.0:     # +1s 容忍时钟截断
                return False                # pid 被复用: 原进程已死
        except Exception:  # noqa: BLE001
            pass                            # 取不到创建时间 -> 退回"存在即存活"
    return True


def _pid_alive(pid: int, started_before: str | None = None) -> bool:
    """兼容旧调用方(webui 看板/测试): 只有**确定存活**才返回 True。"""
    return _pid_state(pid, started_before) is True


# ---------------------------------------------------------------- OS 独占文件锁

def _try_lock(fh) -> bool | None:
    """非阻塞独占锁(锁 `_LOCK_OFFSET` 处的 1 字节): True=拿到 / False=被持有 / None=该文件系统不支持(降级)。"""
    try:
        fh.seek(_LOCK_OFFSET)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        # WinError 33/36 = 已被锁定(正常"被占用"); 其余(网络盘/不支持) → 降级
        if getattr(exc, "winerror", None) in (33, 36) or getattr(exc, "errno", None) in (11, 13, 26, 33):
            return False
        return None
    except Exception:  # noqa: BLE001
        return None


def _unlock(fh) -> None:
    try:
        fh.seek(_LOCK_OFFSET)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass


def _read_lock_info(path: Path) -> dict:
    """读锁文件里的诊断信息(兼容旧格式: 旧版无前导空行)。"""
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()]
    except OSError:
        return {}
    lines = [ln for ln in lines if ln] or [""]
    pid = int(lines[0]) if lines and lines[0].isdigit() else -1
    return {"pid": pid,
            "label": lines[1] if len(lines) > 1 else "",
            "started": lines[2] if len(lines) > 2 else "",
            "raw": lines[:3]}


def _fmt_lines(pid: int, label: str) -> str:
    """锁文件正文 = 3 行(pid/入口/时间), 从文件开头写(锁在远偏移, 不冲突, 且正文始终可读)。"""
    return f"{pid}\n{label}\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"


def _activity_ts(cat_root: Path, info: dict) -> float:
    """降级路径判残留用的"活跃度"时间戳 = max(锁 mtime, 锁内记录时间, 监控日志 mtime, 状态文件 mtime)。"""
    stamps = []
    for p in (cat_root / ".run.lock",
              cat_root / "20_备份与监控" / "pipeline-progress.log",
              cat_root / "20_备份与监控" / "step-status.txt"):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            continue
    if info.get("started"):
        try:
            stamps.append(datetime.strptime(info["started"], "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            pass
    return max(stamps) if stamps else 0.0


class RunLock:
    """锁句柄: 持有打开的文件句柄(OS 锁)直到 release(); release 只解锁不删文件。"""

    def __init__(self, path: Path, pid: int, fh=None):
        self.path = path
        self.pid = pid
        self._fh = fh

    @property
    def holds_os_lock(self) -> bool:
        return self._fh is not None

    def release(self) -> None:
        if self._fh is not None:
            _unlock(self._fh)
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def probe_lock(cat_root, data_root: str | None = None) -> tuple[bool, str | None, bool]:
    """判"该类目是否还有存活流水线进程"(给 webui 看板用, 不获取锁)。

    返回 (是否存活, 锁内启动时间, 是否"无法判定")。
    逻辑: ①试拿 OS 锁 —— 拿到=没人持有(残留锁), 立即释放;
          ②拿到后**再用 psutil 复核**锁里记录的 pid(兼容旧版"只写文件不持锁"的进程;
            只可能把"空闲"改成"有人", 不会反向, 属保守方向);
          ③文件锁不可用 → 退回 pid 三态判定: 确定 → 用它; 无法判定 → 按活跃度是否超
            `TAKEOVER_MINUTES` 推断, 并把"无法判定"标记出来供界面提示。
    """
    root = Path(str(cat_root))
    path = root / ".run.lock"
    if not path.is_file():
        return False, None, False
    info = _read_lock_info(path)
    fh = None
    try:
        fh = open(path, "r+b")
        got = _try_lock(fh)
    except OSError:
        got = None
    if got is True:
        # 没人持有 OS 锁 → 再看一眼旧进程(兼容升级前的持锁者)
        if info.get("pid", -1) > 0 and info["pid"] != os.getpid():
            if _pid_state(info["pid"], info.get("started")) is True:
                _unlock(fh)
                fh.close()
                return True, info.get("started") or None, False
        _unlock(fh)
        try:
            fh.close()
        except OSError:
            pass
        return False, info.get("started") or None, False
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass
    if got is False:
        return True, info.get("started") or None, False      # 有人持有 OS 锁
    # ③ 文件锁不可用: pid 三态
    state = _pid_state(info.get("pid", -1), info.get("started")) if info.get("pid", -1) > 0 else False
    if state is True:
        return True, info.get("started") or None, False
    if state is False:
        return False, info.get("started") or None, False
    stale = (datetime.now().timestamp() - _activity_ts(root, info)) >= TAKEOVER_MINUTES * 60
    return (not stale), info.get("started") or None, True


def acquire_run_lock(cat_root, label: str = "run", log=None) -> RunLock:
    """获取某类目运行锁; 若已有**存活**进程持有则抛 RuntimeError, 否则写入并返回句柄。"""
    root = Path(str(cat_root))
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".run.lock"
    info = _read_lock_info(path) if path.is_file() else {}
    last_exc: Exception | None = None
    fh = None
    got: bool | None = None
    for attempt in range(_LOCK_TRIES):
        try:
            fh = open(path, "r+b" if path.is_file() else "w+b")
        except OSError as exc:
            last_exc = exc
            fh = None
            break
        got = _try_lock(fh)
        if got is True:
            break
        try:
            fh.close()
        except OSError:
            pass
        fh = None
        if got is False and attempt < _LOCK_TRIES - 1:
            import time as _t  # noqa: PLC0415
            _t.sleep(_LOCK_RETRY_SLEEP)

    if got is True:
        # 兼容: 拿到 OS 锁但文件里记录的旧 pid 确实存活(旧版只写文件不持锁) → 仍判"有人"
        if info.get("pid", -1) > 0 and info["pid"] != os.getpid() \
                and _pid_state(info["pid"], info.get("started")) is True:
            _unlock(fh)
            try:
                fh.close()
            except OSError:
                pass
            raise RuntimeError(
                f"该类目已有流水线进程在运行(pid={info['pid']}, {info.get('label') or '未知入口'}, "
                f"开始于 {info.get('started') or '?'}) —— 单实例锁生效, 请先停止它再启动"
            )
        _write_lock_file(fh, label)
        return RunLock(path, os.getpid(), fh)

    if got is False:
        raise RuntimeError(
            f"该类目已有流水线进程在运行(pid={info.get('pid') or '?'}, "
            f"{info.get('label') or '未知入口'}, 开始于 {info.get('started') or '?'}) —— "
            f"单实例锁(OS 文件锁)生效, 请先停止它再启动"
        )

    # ---- 降级: 文件系统不支持字节锁(网络盘/受限挂载) ----
    state = _pid_state(info["pid"], info.get("started")) if info.get("pid", -1) > 0 else False
    if state is True:
        raise RuntimeError(
            f"该类目已有流水线进程在运行(pid={info['pid']}, {info.get('label') or '未知入口'}, "
            f"开始于 {info.get('started') or '?'}); 文件锁不可用({last_exc or '未知原因'}), "
            f"按 pid 判定为存活 → 拒绝启动"
        )
    if state is None:
        age_min = (datetime.now().timestamp() - _activity_ts(root, info)) / 60.0
        if age_min < TAKEOVER_MINUTES:
            eta = datetime.fromtimestamp(_activity_ts(root, info) + TAKEOVER_MINUTES * 60)
            raise RuntimeError(
                f"该类目锁文件存在, 但本环境既拿不到文件锁、也无法判定 pid {info.get('pid')} 是否存活; "
                f"活跃度 {age_min:.0f} 分钟 < {TAKEOVER_MINUTES} 分钟 → 保守拒绝启动"
                f"(若确无进程在跑, 最晚 {eta:%Y-%m-%d %H:%M} 可自动接管; 也不建议手工删锁)"
            )
        _warn(f"文件锁不可用且无法判定 pid {info.get('pid')} 是否存活, 活跃度 {age_min:.0f} 分钟 "
              f"≥ {TAKEOVER_MINUTES} 分钟 → 判定为残留锁, 自动接管({label})"
              f"; ⚠ 若该进程其实仍在运行, 请立即停止其中一个", log)
    # state is False(确定已死) 或 超时判残留 → 接管(降级路径没有可持有的 OS 锁, 只写文件)
    try:
        fh = open(path, "w+b")
    except OSError:
        return RunLock(path, os.getpid(), None)      # 连写都写不了: 退化为无锁(不阻断运行)
    _write_lock_file(fh, label)
    try:
        fh.close()
    except OSError:
        pass
    return RunLock(path, os.getpid(), None)


def _write_lock_file(fh, label: str) -> None:
    """把 pid/入口/时间写进已打开的锁文件(锁在 `_LOCK_OFFSET`, 正文从 0 写不受影响)。"""
    body = _fmt_lines(os.getpid(), label).encode("utf-8")
    try:
        fh.seek(0)
        fh.write(body)
        fh.truncate(len(body))
        fh.flush()
        os.fsync(fh.fileno())
    except OSError:
        pass
