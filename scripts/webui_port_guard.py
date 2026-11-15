# -*- coding: utf-8 -*-
"""Web 控制台**端口守卫**: 启动前处理"旧服务没退干净 → 新服务绑定失败 → 浏览器还在跟旧进程说话"。

## 为什么需要它(2026-10-10 用户实测)

重启了看板, 页面却弹:

    ⚠ 旧构建(2026-10-09.01): … 请重启服务(重启后应显示 2026-10-09.02)

不是页面缓存, 而是 **8790 仍被上一个服务进程占着** —— `start_webui.bat` 只做
`python webui\\server.py`, 端口被占时新进程绑定失败直接退出(黑窗口里有一行报错), 浏览器连的
还是旧进程。诊断旁证: `/api/bootstrap` 返回 `project_root` 是本仓库、`step_count=21`(新代码),
但 `webui_version` 仍是上一次构建的版本号。

## 用法

    python scripts/webui_port_guard.py --check      # 只看: 谁占着端口 + 它自报的 build 版本
    python scripts/webui_port_guard.py              # 占用则询问是否结束(交互)
    python scripts/webui_port_guard.py --yes        # 不问, 直接结束(start_webui.bat 用)

退出码: **0 = 端口空闲, 可以启动** / 1 = 仍被占用(或用户选择保留)

## 安全性

* 只 `taskkill /PID <占用者> /F` —— **不带 `/T`**, 因此**不会**连带杀掉该类目正在跑的流水线子进程
  (流水线是独立子进程, 继续写自己的日志与产物; 新服务能靠 `.run.lock` 找回它们, 见
  `webui/server.py::_api_stop` 的注释)。
* 占用者不是 python 时**不会**自动结束 —— 需要你显式确认(避免误杀别人的服务)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 8790
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- 找占用者

def parse_netstat(text: str, port: int) -> list[int]:
    """从 `netstat -ano` 文本里取**监听**该端口的 PID(纯函数, 便于测试)。

    只认"本地地址恰好以 `:<port>` 结尾 + 状态 LISTENING"; 因此
    `:87900`、以及对端(remote)恰好是 :8790 的连接都不会被误判。
    """
    want = ":%d" % int(port)
    pids: list[int] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        local, state = parts[1], parts[-2].upper() if len(parts) >= 5 else parts[-1].upper()
        if not local.endswith(want):
            continue
        if state != "LISTENING":
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _pids_via_psutil(port: int):
    """优先用 psutil(本机已装, 见 scripts/diag_cursor_watch.py); 不可用返回 None。"""
    try:
        import psutil                                              # type: ignore
    except Exception:                                              # noqa: BLE001
        return None
    out = []
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if getattr(conn, "status", "") != psutil.CONN_LISTEN:
                continue
            addr = getattr(conn, "laddr", None)
            if addr and int(addr.port) == int(port) and conn.pid:
                out.append(int(conn.pid))
    except Exception:                                              # noqa: BLE001 — 权限不足时退 netstat
        return None
    return sorted(set(out))


def _name_of(pid: int) -> str:
    try:
        import psutil                                              # type: ignore
        return psutil.Process(pid).name()
    except Exception:                                              # noqa: BLE001
        pass
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        m = re.match(r'\s*"([^"]+)"', r.stdout or "")
        return m.group(1) if m else "?"
    except Exception:                                              # noqa: BLE001
        return "?"


def list_listening(port: int) -> list[tuple[int, str]]:
    """返回 [(pid, 进程名)]; psutil 不可用时退到 `netstat -ano`。"""
    pids = _pids_via_psutil(port)
    if pids is None:
        try:
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15,
                               creationflags=CREATE_NO_WINDOW)
            pids = parse_netstat(r.stdout or "", port)
        except Exception:                                          # noqa: BLE001
            return []
    return [(p, _name_of(p)) for p in (pids or [])]


def live_version(port: int, timeout: float = 3.0) -> str:
    """问一下端口上的服务自报的构建版本(非本服务/读不到都返回空串)。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/bootstrap" % int(port),
                                    timeout=timeout) as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
        return str(data.get("webui_version") or "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def port_in_use(port: int, timeout: float = 0.6) -> bool:
    """端口上到底有没有人在监听(**不依赖枚举权限**): 能连上就是有人在。

    为什么需要它: 权限不足时 `psutil.net_connections()` 与 `netstat` 都可能拿不到占用者,
    此时"枚举为空"**不等于"端口空闲"** —— 直接放行会让服务启动失败得莫名其妙。
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def kill_pid(pid: int) -> tuple[bool, str]:
    """结束单个进程(**不带 /T**, 不波及流水线子进程)。"""
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True,
                               timeout=30, creationflags=CREATE_NO_WINDOW)
            ok = r.returncode == 0
            return ok, ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1] if (
                (r.stdout or r.stderr)) else ("已结束" if ok else "taskkill 失败")
        os.kill(pid, 15)
        return True, "已发送 TERM"
    except Exception as exc:                                       # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")                   # type: ignore[attr-defined]
    except Exception:                                              # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog="webui_port_guard.py",
                                 description="看板端口守卫: 启动前清理没退干净的旧服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--check", action="store_true", help="只看状态, 不结束任何进程")
    ap.add_argument("--yes", action="store_true", help="不询问, 直接结束占用者(供 start_webui.bat 调用)")
    args, _unknown = ap.parse_known_args(argv)                     # 忽略 --host 等透传参数

    port = int(args.port)
    holders = list_listening(port)
    if not holders:
        if port_in_use(port):
            ver = live_version(port)
            print("端口 %d **有服务在监听**, 但本进程无权枚举占用者(权限不足)。" % port)
            if ver:
                print("  它自报 build = %s" % ver)
            print("  处理: ① 用管理员权限重跑本脚本; 或 ② 在任务管理器里结束 python 进程;")
            print("        或 ③ 命令行 `netstat -ano | findstr :%d` 找到 PID 后 `taskkill /PID <pid> /F`。" % port)
            return 1
        print("端口 %d 空闲 ✓ 可以启动服务。" % port)
        return 0

    ver = live_version(port)
    print("端口 %d 已被占用 —— 新服务会因绑定失败而退出, 浏览器仍连着这个旧进程:" % port)
    for pid, name in holders:
        print("   PID %-7d %-16s %s" % (pid, name, ("自报 build = " + ver) if ver else "(读不到版本)"))
    if ver:
        print("   ⚠ 旧进程自报的 build = %s; 若与你刚改的代码不符, 就是它没退干净。" % ver)
    if args.check:
        print("\n只检查不处理。要清理请运行: python scripts/webui_port_guard.py --yes")
        return 1

    non_python = [p for p, n in holders if "python" not in (n or "").lower()]
    if non_python and not args.yes:
        print("\n⚠ 占用者里含**非 python** 进程 %s —— 可能是别的软件在用这个端口, 请自行确认后再结束。"
              % non_python)
    if not args.yes:
        try:
            ans = input("结束以上进程并继续启动? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消(端口仍被占用)。")
            return 1

    for pid, name in holders:
        ok, msg = kill_pid(pid)
        print("   %s PID %d (%s): %s" % ("✓" if ok else "✗", pid, name, msg))
    for _ in range(20):                                            # 最多等 10 秒端口释放
        if not list_listening(port):
            print("端口 %d 已释放 ✓ 可以启动服务(标注: 流水线子进程未被波及, 仍在继续跑)。" % port)
            return 0
        time.sleep(0.5)
    print("✗ 端口 %d 仍被占用 —— 请用任务管理器手动结束, 或改端口: python webui\\server.py --port 8791"
          % port)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
