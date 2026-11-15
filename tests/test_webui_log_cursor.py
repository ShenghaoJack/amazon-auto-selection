"""回归测试: 多类目并行下"日志面板游标"必须按缓冲(cat)重置(2026-09-15 用户报障)。

现象: 从类目中心点进 `Chopping Boards-FR-202608` 的运行台, ③ 日志面板**还停在 UK 的步骤10**。
根因: 每个在跑的类目有**自己的**环形缓冲(`RingLog`), id 序列各自从 1 开始; 而前端只有一个
全局游标 `lastLogId`。切到 FR 后仍用 UK 的大游标去请求(`after=UK的watermark`), 对新缓冲
`since(大数)` **恒为空** → 面板不再更新、停在旧类目内容。
实测(用户当时两台跑着的实例): UK watermark=113, FR=54; `after=113` 请求 FR → **0 行**,
`after=-1` → **54 行**(FR 自己的日志)。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT_A = "Cat A-US-202609"
CAT_B = "Cat B-US-202609"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_streamkey", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp, monkeypatch):
    s = _load_server()
    root = Path(ws_tmp)
    monkeypatch.setattr(s, "PROJECT_ROOT", root)
    for cat in (CAT_A, CAT_B):
        d = root / "data" / cat
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(
            f'CAT: "{cat}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n', encoding="utf-8")
    monkeypatch.setattr(s, "_lock_live_started", lambda cat, droot: (False, None, False))
    monkeypatch.setattr(s, "RING", s.RingLog())
    return s


class _R:
    """替身 runner: 只有 A 有运行记录(自己的缓冲)。"""

    def __init__(self, rings):
        self.rings = rings

    def snapshot(self, cat=None):
        if cat == CAT_A:
            return {"active": True, "cat": CAT_A, "pid": 1, "started_at": "2026-09-15 23:24:24"}
        return {}

    def snapshots(self):
        return [{"active": True, "cat": CAT_A, "pid": 1}]

    def active_for(self, cat):
        return cat == CAT_A

    def active_cats(self):
        return [CAT_A]

    def ring_for(self, cat):
        return self.rings.get(cat)


def _status(server, cat: str) -> dict:
    h = server.Handler.__new__(server.Handler)
    return h._api_status({"file": [f"data/{cat}/config.yaml"], "after": ["-1"]}, "/api/status")


def test_status_reports_stream_key_per_ring(server, monkeypatch):
    """`stream_key` = 本次日志取自哪个缓冲: 有运行记录 → 该类目; 没有 → `__server__`。

    前端据此判断"游标是否属于同一个缓冲"; 没有这个信号时换类目会拿旧游标去请求新缓冲 → 面板冻结。
    """
    ring_a = server.RingLog()
    ring_a.push("A 自己的日志")
    monkeypatch.setattr(server, "RUNNER", _R({CAT_A: ring_a}))
    sa = _status(server, CAT_A)
    assert sa["stream_key"] == CAT_A, "有运行记录 → 键就是类目名"
    assert sa["stream_watermark"] == ring_a.watermark()
    assert [e["text"] for e in sa["stream"]] == ["A 自己的日志"]
    sbb = _status(server, CAT_B)                      # B 没在跑 → 回落全局缓冲
    assert sbb["stream_key"] == "__server__", "无运行记录 → 全局缓冲的键"
    assert sbb["cat"] == CAT_B, "但展示的类目仍是 B(前端用 cat 兜底判断是否换类目)"


def test_ring_ids_are_independent_proving_cursor_must_reset(server):
    """两个缓冲的 id 各自从 1 开始 → 用一个缓冲的大游标请求另一个**必然为空**(bug 本质)。"""
    a, b = server.RingLog(), server.RingLog()
    for i in range(5):
        a.push(f"A{i}")
    for i in range(3):
        b.push(f"B{i}")
    assert a.watermark() == 5 and b.watermark() == 3
    assert b.since(a.watermark()) == [], "旧游标(5)请求只有 3 条的缓冲 → 空(面板会冻结)"
    assert len(b.since(-1)) == 3, "重置游标后能拿到 B 的全部日志"


def test_index_html_resets_log_cursor_when_buffer_changes(server):
    """前端: 缓冲变了必须清游标/清面板并立刻重取; 键优先 stream_key, 旧服务退回 cat。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "let lastLogKey = \"\";" in html, "缺少游标归属记录"
    assert "const key = d.stream_key || d.cat || \"\";" in html, "键要优先 stream_key、退回 cat"
    assert "if (key !== lastLogKey) {" in html, "换缓冲时必须重置"
    assert "lastLogId = -1;" in html and "clearLogPanel();" in html
    assert "setTimeout(pollNow, 0);" in html, "重置后要立刻重取, 否则面板会等下一轮才更新"
    # 轮询循环也要马上按当前类目取一次(切类目后不等 1~2.2 秒)
    assert "if (!polling) pollNow();" in html


def test_status_cursor_does_not_skip_concurrent_log_append(server, monkeypatch):
    ring = server.RingLog()
    ring.push("first")
    original = ring.since
    def append_after_snapshot(after, limit=400):
        result = original(after, limit)
        ring.push("second")
        return result
    monkeypatch.setattr(ring, "since", append_after_snapshot)
    monkeypatch.setattr(server, "RUNNER", _R({CAT_A: ring}))
    result = _status(server, CAT_A)
    assert [e["text"] for e in result["stream"]] == ["first"]
    assert result["stream_watermark"] == 1
    assert [e["text"] for e in original(result["stream_watermark"])] == ["second"]


def test_event_cache_throttling_is_per_category_and_exact_by_default(server, ws_tmp, monkeypatch):
    a, b = Path(ws_tmp) / "a", Path(ws_tmp) / "b"
    for path in (a, b):
        path.mkdir()
        (path / "pipeline-progress.log").write_text("", encoding="utf-8")
        (path / "monitor-alerts.txt").write_text("", encoding="utf-8")
    assert server.log_event_counts(a, max_age=3) == (0, 0)
    (a / "pipeline-progress.log").write_text("步骤 01 测试失败: 错误\n", encoding="utf-8")
    assert server.log_event_counts(a, max_age=3) == (0, 0)
    assert server.log_event_counts(b, max_age=3) == (0, 0)
    assert server.log_event_counts(a) == (1, 0)
