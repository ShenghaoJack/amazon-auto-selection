# -*- coding: utf-8 -*-
"""抓"光标自己移动"的现行: 采样鼠标坐标, 检测程序化移动, 并同时记录前台窗口与可疑进程。

**用途**: 判断"桌面光标自己移动"到底是谁干的 —— 本项目(Amazon 流水线)任何位置都
没有系统级输入控制(全仓库无 pyautogui/pynput/SetCursorPos/SendInput 等), 所以怀疑对象是
RPA(如影刀 ShadowBot)/鼠标宏/远程控制软件。本脚本只**读**坐标, 不注入任何输入, 运行期间
不影响其它程序。

判定口径(为什么这样判):
  · 单次采样位移 > `--jump`(默认 120px) → **瞬移**: 典型是程序直接定位光标(RPA 点击前定位/
    SetCursorPos), 人手几乎做不到单帧 120px 以上的直线跳变;
  · 连续 ≥ `--hold`(默认 8)次采样都有位移 → **持续轨迹**: 程序拖拽/连点宏;
  · 其余小步移动 → 视为人为(不记录, 只计数)。
每次"事件"记录: 时间/起止坐标/持续/单步最大位移/前台窗口(标题+进程)/影刀等可疑进程/
当前有 .run.lock 的类目(即本流水线是否在跑)。

用法:
  python scripts/diag_cursor_watch.py                  # 观察 10 分钟
  python scripts/diag_cursor_watch.py --seconds 1800   # 观察 30 分钟
  python scripts/diag_cursor_watch.py --jump 60 --hold 4   # 更敏感的阈值
输出: 控制台实时打印 + `logs/cursor-watch-<时间戳>.log`(便于回看与贴给我分析)
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 可疑进程名(出现即打印, 便于归因)
SUSPECT = ("shadowbot", "shadow", "uipath", "autohotkey", "autoclicker", "tinytask",
           "teamviewer", "anydesk", "todesk", "sunlogin", "rustdesk", "parsec",
           "vnc", "mstsc", "quickassist", "splashtop", "awesun", "logi", "synapse")


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos():
    pt = _POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return (pt.x, pt.y)


def _foreground():
    """返回 (窗口标题, 进程名, pid); 失败返回 ('', '', 0)。"""
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        length = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        u32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        pid = ctypes.c_ulong(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        try:
            import psutil  # 本机已装; 未装则留空
            name = psutil.Process(pid.value).name()
        except Exception:  # noqa: BLE001
            name = ""
        return title, name, int(pid.value)
    except Exception:  # noqa: BLE001
        return "", "", 0


def _suspect_procs() -> list[str]:
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["name", "pid"]):
            nm = (p.info.get("name") or "").lower()
            if any(s in nm for s in SUSPECT):
                out.append(f"{p.info.get('name')}(pid={p.info.get('pid')})")
    except Exception:  # noqa: BLE001
        pass
    return out


def _locks() -> list[str]:
    """当前持有 .run.lock 的类目(即本流水线是否在跑)。"""
    out = []
    for lock in (ROOT / "data").glob("*/.run.lock"):
        try:
            txt = lock.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            pid = txt[0].strip() if txt else "?"
            label = txt[1].strip() if len(txt) > 1 else "?"
            if pid.isdigit() and pid != str(os.getpid()):
                try:
                    import psutil
                    alive = psutil.pid_exists(int(pid))
                except Exception:  # noqa: BLE001
                    alive = True
                if alive:
                    out.append(f"{lock.parent.name}(pid={pid}, {label})")
        except OSError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="光标移动取证(只读, 不注入输入)")
    ap.add_argument("--seconds", type=float, default=600, help="观察时长(默认 600s)")
    ap.add_argument("--interval", type=float, default=0.1, help="采样间隔秒(默认 0.1)")
    ap.add_argument("--jump", type=int, default=120, help="单帧位移超过该值记为瞬移(默认 120px)")
    ap.add_argument("--hold", type=int, default=8, help="连续有位移的采样数达到该值记为持续轨迹(默认 8)")
    args = ap.parse_args()

    logdir = ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logpath = logdir / f"cursor-watch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    def emit(line: str) -> None:
        print(line, flush=True)
        try:
            with open(logpath, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    if _cursor_pos() is None:
        print("无法读取光标位置(该脚本需在 Windows 桌面会话里运行)", flush=True)
        return 2
    emit(f"# 光标取证开始 {datetime.now():%Y-%m-%d %H:%M:%S}  时长={args.seconds:.0f}s "
         f"采样={args.interval*1000:.0f}ms 瞬移阈值={args.jump}px 持续阈值={args.hold}帧")
    emit(f"# 可疑进程(开始): {', '.join(_suspect_procs()) or '无'}")
    emit(f"# 流水线运行中: {', '.join(_locks()) or '否(无存活 .run.lock)'}")

    prev = _cursor_pos()
    run_len = 0
    burst_start = None
    burst_prev = prev
    max_step = 0
    events = 0
    human_steps = 0
    t0 = time.time()
    fg_seen: dict[str, int] = {}
    try:
        while time.time() - t0 < args.seconds:
            time.sleep(args.interval)
            cur = _cursor_pos()
            if cur is None or prev is None:
                prev = cur
                continue
            step = max(abs(cur[0] - prev[0]), abs(cur[1] - prev[1]))
            moved = step > 1
            if moved:
                title, proc, _pid = _foreground()
                if proc or title:
                    fg_seen[f"{proc or '?'} | {title[:60]}"] = fg_seen.get(f"{proc or '?'} | {title[:60]}", 0) + 1
                if run_len == 0:
                    burst_start = prev
                    max_step = 0
                run_len += 1
                max_step = max(max_step, step)
                if step >= args.jump:
                    events += 1
                    emit(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f"  [瞬移 {step}px] "
                         f"{prev} -> {cur}  前台={proc or '?'}「{title[:50]}」"
                         f"  可疑进程={', '.join(_suspect_procs()) or '无'}"
                         f"  流水线={', '.join(_locks()) or '否'}")
                    run_len = 0
                    burst_start = None
                elif run_len == args.hold:
                    events += 1
                    emit(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f"  [持续轨迹 ≥{args.hold}帧] "
                         f"起{burst_start} 单帧最大 {max_step}px  前台={proc or '?'}「{title[:50]}」")
                    run_len = 0
                    burst_start = None
            else:
                if run_len and run_len < args.hold:
                    human_steps += 1
                run_len = 0
                burst_start = None
            prev = cur
    except KeyboardInterrupt:
        emit("# 手动中断")

    emit(f"# 结束 {datetime.now():%Y-%m-%d %H:%M:%S}  程序化移动事件 {events} 次; "
         f"零碎移动 {human_steps} 段(疑似人为)")
    if fg_seen:
        top = sorted(fg_seen.items(), key=lambda x: -x[1])[:5]
        emit("# 移动期间出现最多的前台窗口:")
        for name, n in top:
            emit(f"#   {n:>5} 次  {name}")
    emit("# 可疑进程(结束): " + (", ".join(_suspect_procs()) or "无"))
    emit(f"# 日志文件: {logpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
