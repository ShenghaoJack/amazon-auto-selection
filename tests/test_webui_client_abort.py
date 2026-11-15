# 单元测试: webui 客户端提前断开(刷新/关页/取消下载)不再打 traceback(2026-09-13)
#
# 背景: 浏览器在服务端写完响应前关闭连接 → wfile.write 抛 ConnectionAbortedError(WinError 10053)
#   / BrokenPipeError / ConnectionResetError。原实现让异常冒到 socketserver → 一次刷新打印两大段
#   traceback(第一段=写响应体中断; 第二段=兜底 500 分支又写一次), 淹没真正的报错。
# 现行为: 三类异常统一吞掉 + 记一行(同位置 5 秒内合并计数); 兜底分支区分"客户端断开"与业务错误;
#   附件下载把 ConnectionAbortedError 也纳入捕获(原来只捕 BrokenPipe/ConnectionReset)。
import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(__file__).resolve().parent / "_wb_abort_tmp"


def _load_server():
    """按文件路径加载 webui/server.py(它不在包内, 与 tests/test_event_counts.py 同一套路)。"""
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("webui_server_abort", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load_server()


class _Wfile:
    def __init__(self, exc):
        self.exc = exc
        self.written = []

    def write(self, data):
        if self.exc:
            raise self.exc
        self.written.append(data)


class _Dummy:
    """最小 Handler 替身: 只提供 _send/_send_file 用到的方法与属性。"""

    command = "GET"

    def __init__(self, exc=None):
        self.wfile = _Wfile(exc)
        self.path = "/api/bootstrap"
        self.headers_sent = []

    def send_response(self, code):
        self.headers_sent.append(("status", code))

    def send_header(self, k, v):
        self.headers_sent.append((k, v))

    def end_headers(self):
        self.headers_sent.append(("end", None))


@pytest.mark.parametrize("exc", [
    ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接。"),
    BrokenPipeError(32, "Broken pipe"),
    ConnectionResetError(10054, "连接被重置"),
])
def test_send_swallows_client_gone(exc):
    dummy = _Dummy(exc)
    server.Handler._send(dummy, 200, b"{}")          # 不抛
    assert dummy.headers_sent and dummy.wfile.written == []


def test_send_still_raises_other_errors():
    dummy = _Dummy(ValueError("boom"))
    try:
        server.Handler._send(dummy, 200, b"{}")
        raise AssertionError("非客户端断开异常应继续抛出")
    except ValueError:
        pass


def test_send_file_swallows_aborted_download():
    # 不用 pytest tmp_path(沙箱拒绝系统临时目录), 用仓库内自建目录
    shutil.rmtree(_TMP, ignore_errors=True)
    _TMP.mkdir(parents=True, exist_ok=True)
    f = _TMP / "a.bin"
    f.write_bytes(b"x" * 10)
    dummy = _Dummy(ConnectionAbortedError(10053, "aborted"))
    server.Handler._send_file(dummy, f, "a.bin")     # 不抛(原来会抛并打 traceback)
    assert dummy.headers_sent


def test_is_client_gone_helper():
    assert server.is_client_gone(ConnectionAbortedError(10053, "x"))
    assert server.is_client_gone(BrokenPipeError(32, "x"))
    assert not server.is_client_gone(ValueError("x"))


def test_log_client_abort_merges_repeats(capsys):
    server._ABORT_STATE.update({"key": "", "ts": 0.0, "n": 0})
    exc = ConnectionAbortedError(10053, "x")
    for _ in range(3):
        server.log_client_abort("GET /api/bootstrap", exc)
    out = capsys.readouterr().out
    assert out.count("[连接中断]") == 1              # 同一位置 5 秒内只打一行
    assert "GET /api/bootstrap" in out
