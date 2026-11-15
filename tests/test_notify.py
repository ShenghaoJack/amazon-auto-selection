"""notify.push_alerts 重置边界单测: 步骤重置([RESET] 行)后只推重置后的新预警。"""
from __future__ import annotations

import os

import core.notify as notify


def _write(ws_tmp, lines):
    f = os.path.join(ws_tmp, "alerts.txt")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return f


def test_push_alerts_no_reset_marker_pushes_tail(ws_tmp, monkeypatch):
    """无 [RESET] 边界: 维持旧行为, 推送文件末尾 limit 条。"""
    lines = [
        "2026-09-01 10:00:00 [CRIT] 旧告警A",
        "2026-09-02 11:00:00 [WARN] 旧告警B",
        "2026-09-03 12:00:00 [CRIT] 最新告警C",
    ]
    captured = {}
    monkeypatch.setattr(
        notify, "push_wecom", lambda wh, content: captured.update(content=content) or True
    )
    assert notify.push_alerts("http://webhook", _write(ws_tmp, lines))
    body = captured["content"]
    assert "旧告警A" in body and "最新告警C" in body


def test_push_alerts_skips_prereset_lines(ws_tmp, monkeypatch):
    """含 [RESET] 边界: 只推送最近一次重置之后的预警, 历史保留但不重推。"""
    lines = [
        "2026-09-01 10:00:00 [CRIT] 重置前失败: 步骤9 类目不达标",
        "2026-09-03 13:24:00 [RESET] 步骤 4-17 已重置, 重置前的历史预警保留不推送",
        "2026-09-03 14:00:00 [WARN] 重置后的新告警1",
        "2026-09-03 14:05:00 [CRIT] 重置后的新告警2",
    ]
    captured = {}
    monkeypatch.setattr(
        notify, "push_wecom", lambda wh, content: captured.update(content=content) or True
    )
    assert notify.push_alerts("http://webhook", _write(ws_tmp, lines))
    body = captured["content"]
    assert "重置前的历史预警" not in body  # 旧文案不重推
    assert "重置前失败" not in body
    assert "新告警1" in body and "新告警2" in body


def test_push_alerts_reset_marker_no_new_lines(ws_tmp, monkeypatch):
    """重置后还没有新预警: 不推送(历史不重推)。"""
    lines = [
        "2026-09-01 10:00:00 [CRIT] 重置前失败",
        "2026-09-03 13:24:00 [RESET] 步骤 4-17 已重置, 重置前的历史预警保留不推送",
    ]
    called = []
    monkeypatch.setattr(notify, "push_wecom", lambda wh, content: called.append(content) or True)
    assert not notify.push_alerts("http://webhook", _write(ws_tmp, lines))
    assert called == []


def test_push_alerts_multiple_resets_uses_last(ws_tmp, monkeypatch):
    """多次重置: 以最近一次 [RESET] 为界。"""
    lines = [
        "2026-09-01 10:00:00 [CRIT] 第一次重置前",
        "2026-09-02 08:00:00 [RESET] 第一次重置",
        "2026-09-02 09:00:00 [CRIT] 第一次重置后(第二次重置前)",
        "2026-09-03 13:24:00 [RESET] 第二次重置",
        "2026-09-03 14:00:00 [WARN] 第二次重置后新告警",
    ]
    captured = {}
    monkeypatch.setattr(
        notify, "push_wecom", lambda wh, content: captured.update(content=content) or True
    )
    assert notify.push_alerts("http://webhook", _write(ws_tmp, lines))
    body = captured["content"]
    assert "第一次重置前" not in body
    assert "第一次重置后" not in body
    assert "第二次重置后新告警" in body
