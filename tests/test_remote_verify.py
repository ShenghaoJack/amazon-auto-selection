"""Unit tests for cross-process remote human verification bridge, WeCom alert, and WebUI wiring."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_remote_verify_wecom_click_type_and_resolve(tmp_path, monkeypatch):
    from core import notify, remote_verify as rv
    from core.config import Config

    monkeypatch.setenv("PIPELINE_REMOTE_VERIFY_DIR", str(tmp_path / "rv"))
    monkeypatch.setenv("WEBUI_PUBLIC_ORIGIN", "https://select.example.com")

    wecom_msgs: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "push_wecom", lambda url, msg: wecom_msgs.append((url, msg)) or True)

    events: list[tuple] = []
    resolved_flag = {"ok": False}

    class _FakeMouse:
        def click(self, x, y):
            events.append(("click", x, y))

    class _FakeKeyboard:
        def type(self, text, delay=0):
            events.append(("type", text))

        def press(self, key):
            events.append(("key", key))
            if key == "Enter":
                resolved_flag["ok"] = True

    class _FakePage:
        url = "https://www.amazon.com/errors/validateCaptcha"
        viewport_size = {"width": 1280, "height": 800}
        mouse = _FakeMouse()
        keyboard = _FakeKeyboard()

        def title(self):
            return "Robot Check"

        def screenshot(self, **kw):
            return b"\xff\xd8\xff\xe0FAKEJPEG"

    cfg = Config({"CAT": "Humidifiers-US-202611", "WECOM_WEBHOOK": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=demo", "LOGIN_TIMEOUT": 10})
    ctx = SimpleNamespace(config=cfg)
    browser = SimpleNamespace(touch_watchdog=lambda: None)
    page = _FakePage()

    result_holder: dict[str, bool] = {}

    def _runner():
        result_holder["ok"] = rv.wait_for_human_verification(
            ctx,
            browser,
            page,
            step_label="步骤00 榜单[bs] 第1页",
            url=page.url,
            is_resolved=lambda p: resolved_flag["ok"],
            timeout=8,
            poll_interval=0.05,
        )

    t = threading.Thread(target=_runner, daemon=True)
    t.start()

    # Wait for session to appear
    deadline = time.monotonic() + 3.0
    sessions = []
    while time.monotonic() < deadline:
        sessions = rv.list_active_sessions()
        if sessions:
            break
        time.sleep(0.05)

    assert len(sessions) == 1
    sid = sessions[0]["id"]
    assert sessions[0]["cat"] == "Humidifiers-US-202611"
    assert sessions[0]["wecom_sent"] is True
    assert rv.get_frame_bytes(sid) == b"\xff\xd8\xff\xe0FAKEJPEG"

    # Permission filtering: unrelated category cannot see this session
    assert rv.list_active_sessions(allowed_cats={"OtherCat"}) == []
    assert len(rv.list_active_sessions(allowed_cats={"Humidifiers-US-202611"})) == 1

    # WeCom notification includes public origin & category
    assert len(wecom_msgs) == 1
    assert "https://select.example.com" in wecom_msgs[0][1]
    assert "需要人工完成亚马逊人机验证" in wecom_msgs[0][1]

    # Simulate user clicking canvas, typing captcha text, and pressing Enter
    rv.enqueue_action(sid, {"type": "click", "x": 320.5, "y": 410.0})
    rv.enqueue_action(sid, {"type": "type", "text": "AB12CD"})
    rv.enqueue_action(sid, {"type": "key", "key": "Enter"})

    t.join(timeout=5.0)
    assert not t.is_alive()
    assert result_holder["ok"] is True
    assert ("click", 320.5, 410.0) in events
    assert ("type", "AB12CD") in events
    assert ("key", "Enter") in events
    assert rv.get_session(sid)["status"] == "verified"
    assert any("亚马逊人机验证已通过" in m for _, m in wecom_msgs)


def test_remote_verify_abort_stops_wait_immediately(tmp_path, monkeypatch):
    from core import notify, remote_verify as rv
    from core.config import Config

    monkeypatch.setenv("PIPELINE_REMOTE_VERIFY_DIR", str(tmp_path / "rv_abort"))
    monkeypatch.setattr(notify, "push_wecom", lambda url, msg: False)

    class _FakePage:
        url = "https://www.amazon.com/errors/validateCaptcha"
        viewport_size = {"width": 1280, "height": 800}

        def title(self):
            return "Robot Check"

        def screenshot(self, **kw):
            return b"\xff\xd8\xff\xe0FAKE"

    ctx = SimpleNamespace(config=Config({"CAT": "AbortCat-US-202611"}))
    browser = SimpleNamespace(touch_watchdog=lambda: None)
    holder: dict[str, bool] = {}

    t = threading.Thread(
        target=lambda: holder.__setitem__(
            "ok",
            rv.wait_for_human_verification(
                ctx, browser, _FakePage(),
                step_label="步骤09 搜索", url="https://www.amazon.com/s?k=test",
                is_resolved=lambda p: False, timeout=10, poll_interval=0.05,
            ),
        ),
        daemon=True,
    )
    t.start()

    sid = ""
    for _ in range(60):
        s = rv.list_active_sessions()
        if s:
            sid = s[0]["id"]
            break
        time.sleep(0.05)
    assert sid
    rv.enqueue_action(sid, {"type": "abort"})
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert holder["ok"] is False
    assert rv.get_session(sid)["status"] == "aborted"


def test_remote_verify_wecom_periodic_and_final_20s_reminders(tmp_path, monkeypatch):
    from core import notify, remote_verify as rv
    from core.config import Config

    monkeypatch.setenv("PIPELINE_REMOTE_VERIFY_DIR", str(tmp_path / "rv_rem"))
    msgs: list[str] = []
    monkeypatch.setattr(notify, "push_wecom", lambda url, msg: msgs.append(msg) or True)

    class _FakePage:
        url = "https://www.amazon.com/errors/validateCaptcha"
        viewport_size = {"width": 1280, "height": 800}

        def title(self):
            return "Robot Check"

        def screenshot(self, **kw):
            return b"\xff\xd8\xff\xe0FAKE"

    ctx = SimpleNamespace(config=Config({"CAT": "RemCat-US-202611", "WECOM_WEBHOOK": "https://example.invalid/hook"}))
    browser = SimpleNamespace(touch_watchdog=lambda: None)

    ok = rv.wait_for_human_verification(
        ctx,
        browser,
        _FakePage(),
        step_label="步骤00 榜单[bs] 第1页",
        url="https://www.amazon.com/errors/validateCaptcha",
        is_resolved=lambda p: False,
        timeout=1,
        poll_interval=0.05,
        remind_interval=0.35,
        final_remind_sec=0.25,
    )
    assert ok is False
    assert any("需要人工完成亚马逊人机验证" in m for m in msgs)
    assert any("再次提醒" in m for m in msgs)
    assert sum(1 for m in msgs if "仅剩 20 秒超时" in m) == 1


def test_remote_verify_drag_path_executes_mouse_down_move_up(tmp_path, monkeypatch):
    from core import notify, remote_verify as rv
    from core.config import Config

    monkeypatch.setenv("PIPELINE_REMOTE_VERIFY_DIR", str(tmp_path / "rv_drag"))
    monkeypatch.setattr(notify, "push_wecom", lambda url, msg: False)

    mouse_ops: list[tuple] = []
    done_flag = {"ok": False}

    class _FakeMouse:
        def move(self, x, y, steps=1):
            mouse_ops.append(("move", x, y))

        def down(self):
            mouse_ops.append(("down",))

        def up(self):
            mouse_ops.append(("up",))
            done_flag["ok"] = True

    class _FakePage:
        url = "https://www.amazon.com/errors/validateCaptcha"
        viewport_size = {"width": 1280, "height": 800}
        mouse = _FakeMouse()

        def title(self):
            return "Slider Check"

        def screenshot(self, **kw):
            return b"\xff\xd8\xff\xe0FAKE"

    ctx = SimpleNamespace(config=Config({"CAT": "DragCat-US-202611"}))
    browser = SimpleNamespace(touch_watchdog=lambda: None)
    holder: dict[str, bool] = {}

    t = threading.Thread(
        target=lambda: holder.__setitem__(
            "ok",
            rv.wait_for_human_verification(
                ctx, browser, _FakePage(),
                step_label="步骤00 滑块验证", url="https://www.amazon.com/errors/validateCaptcha",
                is_resolved=lambda p: done_flag["ok"], timeout=8, poll_interval=0.05,
            ),
        ),
        daemon=True,
    )
    t.start()

    sid = ""
    for _ in range(60):
        s = rv.list_active_sessions()
        if s:
            sid = s[0]["id"]
            break
        time.sleep(0.05)
    assert sid
    rv.enqueue_action(sid, {
        "type": "drag",
        "points": [{"x": 100, "y": 300}, {"x": 220, "y": 302}, {"x": 350, "y": 300}],
    })
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert holder["ok"] is True
    assert mouse_ops[0] == ("move", 100.0, 300.0)
    assert mouse_ops[1] == ("down",)
    assert ("move", 220.0, 302.0) in mouse_ops
    assert ("move", 350.0, 300.0) in mouse_ops
    assert mouse_ops[-1] == ("up",)


def test_webui_wires_remote_verify_endpoints_and_modal():
    server_src = Path("webui/server.py").read_text(encoding="utf-8")
    html_src = Path("webui/index.html").read_text(encoding="utf-8")
    assert "/api/remote-verify/sessions" in server_src
    assert "/api/remote-verify/frame" in server_src
    assert "/api/remote-verify/action" in server_src
    assert 'snap["remote_verify"]' in server_src
    assert "rvOverlay" in html_src
    assert "syncRemoteVerify" in html_src
    assert "/api/remote-verify/action" in html_src
