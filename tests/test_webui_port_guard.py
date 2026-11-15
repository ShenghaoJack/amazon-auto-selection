# 单元测试: 看板端口守卫 `scripts/webui_port_guard.py`(2026-10-10)
#
# 背景(用户实测): 重启了看板, 页面仍弹「旧构建(2026-10-09.01)…请重启服务(重启后应显示 2026-10-09.02)」——
# 原因是 8790 被上一个服务进程占着, 新进程绑定失败退出, 浏览器还在跟旧进程说话。
# 本文件钉住: netstat 解析(只认 LISTENING 且**本地**端口精确匹配)、
# "枚举不到但端口确实有人 → 不能说空闲"(权限不足时的诚实兜底)、以及 kill 不带 /T(不波及流水线)。
import importlib.util
import socket
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("_port_guard", ROOT / "scripts" / "webui_port_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


g = _load()

NETSTAT = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:8790         0.0.0.0:0              LISTENING       12345
  TCP    127.0.0.1:8790         127.0.0.1:52100        ESTABLISHED     99999
  TCP    127.0.0.1:87900        0.0.0.0:0              LISTENING       777
  TCP    192.168.1.5:8790       0.0.0.0:0              LISTENING       22222
  TCP    [::]:8790              [::]:0                 LISTENING       33333
  TCP    127.0.0.1:5432         0.0.0.0:0              LISTENING       4444
"""


# --------------------------------------------------------------------------- netstat 解析

def test_parse_netstat_only_listening_and_exact_port():
    pids = g.parse_netstat(NETSTAT, 8790)
    assert 12345 in pids and 22222 in pids and 33333 in pids      # 本地 :8790(含 IPv6 与其它网卡)
    assert 99999 not in pids, "对端端口恰好是 8790 的连接不能算"
    assert 777 not in pids, ":87900 不能误判成 :8790"
    assert 4444 not in pids, "别的端口不能算"


def test_parse_netstat_empty_and_garbage():
    assert g.parse_netstat("", 8790) == []
    assert g.parse_netstat("not a netstat output\n随便几行\n", 8790) == []
    assert g.parse_netstat("  TCP  127.0.0.1:8790  LISTENING\n", 8790) == []   # 字段不足 → 跳过


def test_parse_netstat_dedupes():
    text = NETSTAT + "  TCP    127.0.0.1:8790         0.0.0.0:0              LISTENING       12345\n"
    assert g.parse_netstat(text, 8790).count(12345) == 1


# --------------------------------------------------------------------------- 端口占用探测

def test_port_in_use_true_for_live_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert g.port_in_use(port) is True
    finally:
        srv.close()


def test_port_in_use_false_for_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert g.port_in_use(port) is False


# --------------------------------------------------------------------------- CLI 行为

def test_main_reports_idle_port(monkeypatch, capsys):
    monkeypatch.setattr(g, "list_listening", lambda port: [])
    monkeypatch.setattr(g, "port_in_use", lambda port: False)
    assert g.main(["--port", "8790"]) == 0
    assert "空闲" in capsys.readouterr().out


def test_main_does_not_claim_idle_when_listener_exists_but_hidden(monkeypatch, capsys):
    """权限不足枚举不到占用者时, 必须**报占用**(否则服务照样启动失败, 用户一头雾水)。"""
    monkeypatch.setattr(g, "list_listening", lambda port: [])
    monkeypatch.setattr(g, "port_in_use", lambda port: True)
    monkeypatch.setattr(g, "live_version", lambda port, timeout=3.0: "2026-10-09.01")
    rc = g.main(["--port", "8790", "--yes"])
    out = capsys.readouterr().out
    assert rc == 1 and "无权枚举" in out and "2026-10-09.01" in out


def test_main_check_only_never_kills(monkeypatch, capsys):
    killed = []
    monkeypatch.setattr(g, "list_listening", lambda port: [(4242, "python.exe")])
    monkeypatch.setattr(g, "live_version", lambda port, timeout=3.0: "2026-10-09.01")
    monkeypatch.setattr(g, "kill_pid", lambda pid: killed.append(pid) or (True, "ok"))
    rc = g.main(["--port", "8790", "--check"])
    out = capsys.readouterr().out
    assert rc == 1 and killed == []
    assert "4242" in out and "2026-10-09.01" in out


def test_main_yes_kills_and_waits_until_free(monkeypatch, capsys):
    state = {"used": True}

    def fake_list(port):
        return [(4242, "python.exe")] if state["used"] else []

    monkeypatch.setattr(g, "list_listening", fake_list)
    monkeypatch.setattr(g, "live_version", lambda port, timeout=3.0: "2026-10-09.01")

    def fake_kill(pid):
        state["used"] = False
        return True, "SUCCESS: 已终止进程"

    monkeypatch.setattr(g, "kill_pid", fake_kill)
    rc = g.main(["--port", "8790", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0 and "已释放" in out and "4242" in out
    assert "流水线子进程未被波及" in out                      # 明确告知不杀子进程


def test_main_refuses_when_user_declines(monkeypatch, capsys):
    killed = []
    monkeypatch.setattr(g, "list_listening", lambda port: [(4242, "python.exe")])
    monkeypatch.setattr(g, "live_version", lambda port, timeout=3.0: "")
    monkeypatch.setattr(g, "kill_pid", lambda pid: killed.append(pid) or (True, "ok"))
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    rc = g.main(["--port", "8790"])
    assert rc == 1 and killed == [] and "已取消" in capsys.readouterr().out


def test_main_warns_on_non_python_holder(monkeypatch, capsys):
    monkeypatch.setattr(g, "list_listening", lambda port: [(555, "nginx.exe")])
    monkeypatch.setattr(g, "live_version", lambda port, timeout=3.0: "")
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    rc = g.main(["--port", "8790"])
    assert rc == 1 and "非 python" in capsys.readouterr().out


def test_kill_pid_has_no_tree_flag():
    """**绝不能**带 /T —— 否则会连带杀掉该类目正在跑的流水线子进程。"""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "SUCCESS", "")

    import unittest.mock as mock
    with mock.patch.object(g.subprocess, "run", fake_run):
        ok, msg = g.kill_pid(1234)
    assert ok and "/T" not in seen["cmd"] and seen["cmd"][:3] == ["taskkill", "/PID", "1234"]


# ------------------------------------------------------------------ 服务端: 拒绝静默多实例

def test_another_instance_live_empty_for_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    import webui.server as server
    assert server.another_instance_live("127.0.0.1", port) == ""


def test_another_instance_live_question_mark_for_non_http_listener():
    """端口被**非本服务**占着(连得上但读不到 /api/bootstrap) → 返回 '?'(不能当没人)。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    import webui.server as server
    try:
        assert server.another_instance_live("127.0.0.1", port) == "?"
    finally:
        srv.close()


def test_another_instance_live_reads_build_from_running_server(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "webui_version": "2026-10-10.01"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    import webui.server as server
    try:
        assert server.another_instance_live("127.0.0.1", port) == "2026-10-10.01"
    finally:
        srv.close()


def test_server_refuses_to_bind_when_instance_already_listening():
    """接线: 探测必须发生在 ThreadingHTTPServer 绑定**之前**, 且拒绝时 return 2(--force 可绕过)。"""
    src = (ROOT / "webui" / "server.py").read_text(encoding="utf-8")
    probe_at = src.index("other = another_instance_live(args.host, port)")
    bind_at = src.index("server = ThreadingHTTPServer((args.host, port), Handler)")
    assert probe_at < bind_at
    head = src[probe_at:bind_at]
    assert "return 2" in head and "已经有东西在监听" in head
    assert '--force' in src and 'action="store_true"' in src


# --------------------------------------------------------------------------- 接线

def test_start_bat_calls_guard_before_server():
    bat = (ROOT / "start_webui.bat").read_text(encoding="utf-8", errors="replace")
    guard_at = bat.index("webui_port_guard.py")
    server_at = bat.index("webui\\server.py")
    assert guard_at < server_at, "守卫必须在启动服务之前调用"
    assert "--yes" in bat and "errorlevel 1" in bat[guard_at:]
