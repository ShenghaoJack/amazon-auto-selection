# 单元测试: 多类目并行(2026-09-15, 用户需求"看板里同时跑多个类目")
# ---------------------------------------------------------------------------
# 三块要守住的行为:
#   ① `RunManager` 由"单进程"改为"按类目的多运行注册表": 不同类目可同时跑,
#      同一类目重复启动必须被拒; 每个类目一条独立日志缓冲(控制台只看本类目);
#   ② 启动守门 `run_start_guard`: 同类目(本服务在跑 / CLI 持锁)拦住, 不同类目放行;
#   ③ `.run.lock` 仍是"每类目一把"的最终防线(不同类目互不影响)。
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webui.server as server  # noqa: E402


# --------------------------------------------------------------- 替身子进程
class _FakeProc:
    """可控退出的假子进程(poll/wait/stdout 与 subprocess.Popen 同形)。

    `wait()` 必须**阻塞到 exit_now()**(真实的 wait 就是这样) —— 否则
    RunManager 的 _waiter 线程会立刻把进程标成"已结束", 测不出并行。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self._code = None
        self._done = threading.Event()
        self.stdout = iter(())
        self.waited = 0

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        self.waited += 1
        self._done.wait(timeout)
        return self._code

    def exit_now(self, code: int = 0):
        self._code = code
        self._done.set()


def _wait_inactive(m, cat: str, timeout: float = 3.0) -> bool:
    """等 RunManager 的 _waiter 线程把该类目标成"已结束"。"""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not m.active_for(cat):
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def manager(monkeypatch):
    """把 Popen/RING 换成可控替身, 返回 (RunManager 实例, 已启动进程列表)。"""
    m = server.RunManager()
    procs: list[_FakeProc] = []
    seq = {"pid": 1000}

    def _popen(cmd, **kw):
        seq["pid"] += 1
        p = _FakeProc(seq["pid"])
        procs.append(p)
        return p

    monkeypatch.setattr(server.subprocess, "Popen", _popen)
    monkeypatch.setattr(server, "RING", server.RingLog())
    # 结束清理历史快照会去 glob 项目目录, 与本测试无关 → 短路掉
    monkeypatch.setattr(server, "RUN_DIR", Path(server.__file__).parent / "_no_such_dir")
    return m, procs


def _start(m, cat: str, root: str = "data"):
    return m.start([sys.executable, "-c", "pass"], "全流程运行", "config.yaml",
                   Path("run.yaml"), cat, root, "resume", 0, {})


# --------------------------------------------------------------- ① 多运行注册表
def test_two_categories_run_in_parallel(manager):
    """A、B 两个类目能同时在跑(旧版第二个会被 `已有流水线在运行中` 拒掉)。"""
    m, procs = manager
    _start(m, "CatA")
    _start(m, "CatB")
    assert m.active is True
    assert m.active_for("CatA") and m.active_for("CatB")
    assert m.active_cats() == ["CatA", "CatB"]
    assert len(procs) == 2

    # 各自独立的运行信息与日志缓冲
    assert m.snapshot("CatA")["pid"] == procs[0].pid
    assert m.snapshot("CatB")["pid"] == procs[1].pid
    assert m.ring_for("CatA") is not m.ring_for("CatB")


def test_same_category_twice_is_rejected(manager):
    """同一个类目重复启动必须被拒(双跑会互写 step-status/产物)。"""
    m, _ = manager
    _start(m, "CatA")
    with pytest.raises(RuntimeError) as ei:
        _start(m, "CatA")
    assert "已有流水线在运行中" in str(ei.value)
    assert m.active_cats() == ["CatA"], "被拒的启动不能留下第二条记录"


def test_category_can_restart_after_finish(manager):
    """类目跑完后可以再启动(不是"一次性"记录)。"""
    m, procs = manager
    _start(m, "CatA")
    procs[0].exit_now(0)
    assert _wait_inactive(m, "CatA"), "进程退出后 _waiter 应把该类目标成已结束"
    _start(m, "CatA")
    assert m.active_for("CatA") is True
    assert m.snapshot("CatA")["pid"] == procs[1].pid


def test_stop_only_stops_given_category(manager, monkeypatch):
    """停止只作用于指定类目(并行时不能把别类目一起杀掉)。"""
    m, procs = manager
    _start(m, "CatA")
    _start(m, "CatB")
    calls: list[str] = []

    def _fake_stop_one(key):
        calls.append(key)
        pid = m.snapshot(key)["pid"]
        for p in procs:
            if p.pid == pid:
                p.exit_now(-9)          # 模拟"杀掉了它"
        return {"ok": True, "pid": pid, "key": key}

    monkeypatch.setattr(m, "_stop_one", _fake_stop_one)
    out = m.stop("CatA")
    assert out["ok"] is True and calls == ["CatA"]
    assert _wait_inactive(m, "CatA")
    assert m.active_for("CatB") is True, "B 不能被误停"
    # 不给 cat 的旧调用: 只有一个在跑就停它; 多个在跑则全停
    out2 = m.stop()
    assert out2["ok"] is True and calls[-1] == "CatB"
    assert _wait_inactive(m, "CatB")
    assert m.active is False
    assert m.stop("CatA")["note"] == "该类目没有运行中的进程"


def test_run_banner_only_reports_own_category():
    """`snapshot(cat)` 只回该类目; `snapshots()` 给全部(前端据此显示"并行中")。"""
    m = server.RunManager()
    rec = {"proc": _FakeProc(1), "info": {"cat": "CatA", "active": True, "pid": 1},
           "ring": server.RingLog()}
    rec["proc"].exit_now(0)                     # 已结束
    m._runs["CatA"] = rec
    assert m.snapshot("CatA")["cat"] == "CatA"
    assert m.snapshot("CatA")["active"] is False
    assert m.snapshot("CatB") == {}, "没有记录的类目返回空(前端横幅不借用别类目)"
    assert m.active_cats() == []


# --------------------------------------------------------------- ② 启动守门
def test_guard_allows_other_category_and_reports_them(monkeypatch):
    """守门: 类目 A 在跑时启动 B **放行**, 并把 A 作为"其它在跑类目"返回。"""
    class _R:
        def active_for(self, cat):
            return cat == "CatA"

        def snapshots(self):
            return [{"cat": "CatA", "active": True}]

    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    others = server.run_start_guard("CatB", "data", runner=_R())
    assert [o["cat"] for o in others] == ["CatA"]


def test_guard_rejects_same_category_running_here(monkeypatch):
    class _R:
        def active_for(self, cat):
            return True

        def snapshots(self):
            return []

    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    with pytest.raises(RuntimeError) as ei:
        server.run_start_guard("CatA", "data", runner=_R())
    assert "已有流水线在本服务运行中" in str(ei.value)


def test_guard_rejects_cli_locked_category(monkeypatch):
    """命令行窗口里在跑(只有 .run.lock)时, 同类目也必须拦(否则会双跑)。"""
    class _R:
        def active_for(self, cat):
            return False

        def snapshots(self):
            return []

    monkeypatch.setattr(server, "_lock_live_started",
                        lambda cat, droot: (True, "2026-09-15 10:00:00", False))
    with pytest.raises(RuntimeError) as ei:
        server.run_start_guard("CatA", "data", runner=_R())
    assert ".run.lock" in str(ei.value) and "2026-09-15 10:00:00" in str(ei.value)


# --------------------------------------------------- ④ 临时配置快照不能互相覆盖(实测复现)
def test_run_snapshot_name_unique_per_category_in_same_second(tmp_path, monkeypatch):
    """**实测复现的数据污染隐患**: 真实类目配置都叫 `config.yaml`, 快照目录却是全局共享的。

    旧命名 `run-<时间戳秒>-config.yaml` 在同一秒内启动两个类目时会算出**同一个路径**, 后者覆盖
    前者; 若先启动的子进程还没读到配置, 它会加载**另一个类目**的配置 → 把产物写进对方的数据目录
    (实测: A/B 两个类目同一秒启动, 最终文件内容是 B 的 CAT)。现要求: 每个类目/每次启动都唯一。
    """
    monkeypatch.setattr(server, "RUN_DIR", tmp_path / "runs")
    server.RUN_DIR.mkdir(parents=True, exist_ok=True)
    h = server.Handler.__new__(server.Handler)
    made = []
    for cat in ("CatA-202608", "CatB-202608"):
        d = tmp_path / "data" / cat
        d.mkdir(parents=True, exist_ok=True)
        p = d / "config.yaml"                       # ← 真实布局: 都叫 config.yaml
        p.write_text(f'CAT: "{cat}"\nDATA_ROOT: "{tmp_path.as_posix()}"\nDRY_RUN: true\n',
                     encoding="utf-8")
        made.append(h._build_run_config(p, {}))
    assert made[0] != made[1], "同一秒内两个类目的临时配置不能是同一个文件(会互相覆盖)"
    for conf, cat in zip(made, ("CatA-202608", "CatB-202608")):
        text = conf.read_text(encoding="utf-8")
        assert f'CAT: "{cat}"' in text, f"{conf.name} 内容必须是自己的 CAT"
        assert cat in conf.name, "文件名里带上 CAT, 便于人工辨认"


def test_prune_keeps_snapshots_of_running_categories(tmp_path, monkeypatch):
    """清理旧快照时必须**跳过在跑运行的快照**(否则 A 跑完可能删掉 B 正在用的配置)。"""
    monkeypatch.setattr(server, "RUN_DIR", tmp_path / "runs")
    server.RUN_DIR.mkdir(parents=True, exist_ok=True)
    names = [f"run-20260915-1600{i:02d}-cat{i}-config-abc{i}.yaml" for i in range(15)]
    paths = []
    for n in names:
        p = server.RUN_DIR / n
        p.write_text("CAT: x\n", encoding="utf-8")
        paths.append(p)
    active = paths[0]                                # 最旧的一份, 但正被某个类目使用
    removed = server.prune_run_snapshots(keep=5, protect={active})
    assert active.exists(), "在跑运行的快照不能被清理"
    assert len(removed) == 15 - 5 - 1, f"应删掉除保护项外的多余快照, 实删 {len(removed)}"

def _write_cat_cfg(root: Path, cat: str, cdp: str, account: str) -> None:
    d = root / "data" / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{cat}"\nDATA_ROOT: "data"\nSS_CDP_URL: "{cdp}"\n'
        f'SS_ACCOUNT: "{account}"\nSS_PASSWORD: "x"\n', encoding="utf-8")


class _RActive:
    """替身 runner: CatB 正在跑(用于守门的冲突判定)。"""

    def __init__(self, cats: list[str]):
        self.cats = cats

    def active_for(self, cat):
        return cat in self.cats

    def snapshots(self):
        return [{"cat": c, "active": True, "data_root": "data"} for c in self.cats]


def test_guard_blocks_same_port_with_different_accounts(tmp_path, monkeypatch):
    """**一个 Chrome 只能保持一份卖家精灵登录态**: 同端口 + 不同账号必须拦下并给出换端口建议。

    用户已明确"会给不同类目配不同账号" —— 若两个类目配了不同账号却共用一个端口, 后跑的
    会读到/用到前一个账号的导出配额与数据(静默出错数据), 比"排队等待"危险得多。
    """
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    _write_cat_cfg(tmp_path, "CatA", "http://127.0.0.1:9222", "a@x.com")
    _write_cat_cfg(tmp_path, "CatB", "http://127.0.0.1:9222", "b@x.com")
    with pytest.raises(RuntimeError) as ei:
        server.run_start_guard("CatA", "data", runner=_RActive(["CatB"]))
    msg = str(ei.value)
    assert "不同账号" in msg and "9223" in msg, f"要说明原因并给出换端口做法: {msg}"


def test_guard_allows_same_port_same_account_with_queue_note(tmp_path, monkeypatch):
    """同端口 + **相同账号** 只是排队(锁串行), 不拦; 但要明确提示"步骤 0/11/12/13 会排队"。"""
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    _write_cat_cfg(tmp_path, "CatA", "http://127.0.0.1:9222", "a@x.com")
    _write_cat_cfg(tmp_path, "CatB", "http://127.0.0.1:9222", "a@x.com")
    others = server.run_start_guard("CatA", "data", runner=_RActive(["CatB"]))
    assert [o["cat"] for o in others] == ["CatB"]
    notes = server.ss_parallel_notes("CatA", "data", others)
    assert notes and "排队" in notes[0] and "9222" in notes[0], f"应提示排队与端口: {notes}"


def test_guard_allows_different_ports_no_wait(tmp_path, monkeypatch):
    """每个类目各自端口/账号 → 放行且**没有任何排队提示**(这才是用户想要的并行)。"""
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    _write_cat_cfg(tmp_path, "CatA", "http://127.0.0.1:9222", "a@x.com")
    _write_cat_cfg(tmp_path, "CatB", "http://127.0.0.1:9223", "b@x.com")
    others = server.run_start_guard("CatA", "data", runner=_RActive(["CatB"]))
    assert [o["cat"] for o in others] == ["CatB"]
    assert server.ss_parallel_notes("CatA", "data", others) == [], "不同端口不该有任何等待提示"


# --------------------------------------------------------------- ③ 独立日志缓冲
def test_console_ring_is_per_category():
    """子进程输出只进本类目缓冲; 服务器级事件(A类目)不串到 B 类目控制台。"""
    a, b = server.RingLog(), server.RingLog()
    server.push_console_line("2026-09-15 10:00:00 [INFO] [Step00] A 类目的输出", ring=a)
    server.push_console_line("2026-09-15 10:00:01 [INFO] [Step00] B 类目的输出", ring=b)
    assert any("A 类目" in e["text"] for e in a.tail())
    assert not any("B 类目" in e["text"] for e in a.tail()), "不能把 B 的日志串进 A"
    assert not any("A 类目" in e["text"] for e in b.tail()), "不能把 A 的日志串进 B"


def test_server_events_mirror_to_active_runs():
    """服务器级事件(保存配置/告警)同时进在跑类目的控制台(旧观感不变)。"""
    run_ring = server.RingLog()
    hub = server.RingLog()
    hub.mirrors.append(run_ring)
    hub.push("已保存配置 data/a/config.yaml", level="INFO")
    assert any("已保存配置" in e["text"] for e in run_ring.tail())
    # 子进程输出(ring=)不该被 mirror 回去造成重复
    server.push_console_line("2026-09-15 10:00:00 [INFO] 子进程行", ring=run_ring)
    assert not any("子进程行" in e["text"] for e in hub.tail())


def test_guard_message_is_actionable_with_free_port_and_command(tmp_path, monkeypatch):
    """冲突报错必须**直接可操作**(2026-09-15): 点名本次要启动的类目 + 给出一个真的空着的端口 + 拉起命令。

    用户实测就卡在这儿: 只知道"冲突了", 不知道改成几号、改完怎么起 Chrome。
    """
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    _write_cat_cfg(tmp_path, "CatA", "http://127.0.0.1:9222", "a@x.com")
    _write_cat_cfg(tmp_path, "CatB", "http://127.0.0.1:9222", "b@x.com")
    with pytest.raises(RuntimeError) as ei:
        server.run_start_guard("CatA", "data", runner=_RActive(["CatB"]))
    msg = str(ei.value)
    assert "CatA" in msg and "CatB" in msg, "两个类目名都要出现(谁在跑 / 谁要启动)"
    assert "SS_CDP_URL" in msg and "9223" in msg, "要给出可直接照抄的端口修改"
    assert "start_ss_chrome.py" in msg and "--port 9223" in msg, "要给出拉起该端口 Chrome 的命令"


# ------------------------------------------------- ⑤ 停"外部/孤儿运行"(2026-09-16 用户确认)
# 背景: `RUNNER._runs` 是内存注册表 —— 看板**非优雅退出**(关窗口/任务管理器/taskkill /F 不带 /T)
# 后上一实例启动的流水线子进程仍在跑(孤儿运行), 新实例注册表为空 → 旧实现下页面「中断」
# 回"该类目没有运行中的进程"(空操作), 只能人工 taskkill。现回退按 `.run.lock` 里的 pid 强杀。
def _mk_orphan_lock(root: Path, cat: str, pid, started: str = "2026-09-16 16:41:26") -> Path:
    """造"孤儿运行"现场: `data/<cat>/.run.lock` 记着存活进程的 pid, 且注册表里没有它。"""
    d = root / "data" / cat
    d.mkdir(parents=True, exist_ok=True)
    p = d / ".run.lock"
    p.write_text(f"{pid}\nrun {cat}\n{started}\n", encoding="utf-8")
    return p


def test_stop_orphan_run_kills_lock_pid(monkeypatch, tmp_path):
    """孤儿运行: 「中断」必须按锁内 pid 强杀进程树, 并把"页面手动中断"写进监控告警。

    ⚠️ 本组用例**故意不用 `manager` fixture**: 它会把 `subprocess.Popen` 换成替身, 而
    `subprocess.Popen` 一旦不是类, `asyncio` 就完全无法导入(playwright 依赖 asyncio),
    进而 `import core.run_lock` 会失败并污染 `sys.modules`。这里直接建 RunManager。
    """
    m = server.RunManager()
    cat = "OrphanCat"
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "RING", server.RingLog())
    _mk_orphan_lock(tmp_path, cat, 4242)

    kills: list[int] = []
    monkeypatch.setattr(server, "_kill_pid_tree", lambda pid: kills.append(pid))
    # 存活探测顺序: ①确认"有孤儿" ②强杀后复查"已死" —— 与真实实现一致
    seq = {"n": 0}

    def fake_probe(cat_root, data_root=None):
        seq["n"] += 1
        return (seq["n"] == 1), "2026-09-16 16:41:26", False

    monkeypatch.setattr("core.run_lock.probe_lock", fake_probe)

    out = m.stop(cat)
    assert kills == [4242], f"应按 .run.lock 里的 pid 强杀; 实际 {kills}"
    assert out["ok"] is True and out["orphan"] is True and out["pid"] == 4242
    assert "已强杀" in out["note"] and "孤儿" in out["note"]
    alerts = (tmp_path / "data" / cat / "17_备份与监控" / "monitor-alerts.txt").read_text(encoding="utf-8")
    assert "流水线被页面手动中断" in alerts and "4242" in alerts, alerts
    assert any("孤儿" in e["text"] and "4242" in e["text"] for e in server.RING.tail(20))


def test_stop_orphan_without_readable_pid_asks_manual(monkeypatch, tmp_path):
    """锁里有存活进程但读不出 pid → 明确让用户手动结束(不能静默当"没在跑")。"""
    m = server.RunManager()
    cat = "OrphanCat2"
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "RING", server.RingLog())
    _mk_orphan_lock(tmp_path, cat, "not-a-pid")
    monkeypatch.setattr("core.run_lock.probe_lock", lambda cat_root, data_root=None: (True, "2026-09-16 16:41:26", False))
    called: list[int] = []
    monkeypatch.setattr(server, "_kill_pid_tree", lambda pid: called.append(pid))

    out = m.stop(cat)
    assert out["ok"] is False and "读不到 pid" in out["note"]
    assert called == [], "读不到 pid 时不能乱杀"


def test_stop_orphan_kill_failure_reports_manual_command(monkeypatch, tmp_path):
    """两次强杀后进程仍在 → 返回失败并给出可照抄的手动命令(不假装成功)。"""
    m = server.RunManager()
    cat = "OrphanCat3"
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "RING", server.RingLog())
    _mk_orphan_lock(tmp_path, cat, 4243)
    monkeypatch.setattr("core.run_lock.probe_lock", lambda cat_root, data_root=None: (True, "2026-09-16 16:41:26", False))
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)      # 跳过 30×0.5s 复查等待
    tried: list[int] = []
    monkeypatch.setattr(server, "_kill_pid_tree", lambda pid: tried.append(pid))

    out = m.stop(cat)
    assert tried == [4243, 4243], "应二次强杀"
    assert out["ok"] is False and "taskkill /PID 4243 /T /F" in out["note"]


def test_stop_idle_category_needs_no_lock(manager, monkeypatch):
    """没有锁文件的类目: 「中断」照旧回"没有运行中的进程"(且不导入 core.*, 见上面注释)。"""
    m, _procs = manager
    out = m.stop("IdleCatWithoutLock")
    assert out == {"ok": True, "note": "该类目没有运行中的进程"}


def test_frontend_enables_stop_for_external_running_category():
    """前端: 外部/孤儿运行(cat_running 但无本服务子进程)也必须点亮「中断」并显示真实 pid。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "running = active || here;" in html, "按钮/守卫的运行态必须含外部进程"
    assert '$("btnStop").disabled = !running;' in html
    assert "cat_external" in html and "cat_live_started" in html
    assert "RUNNING · 外部进程" in html, "横幅要明确标出这是外部/孤儿进程, 别让人误判"
    assert ".run.lock 里记录的 pid 直接强杀它" in html, "中断确认框要预先告知将强杀锁内 pid"


def test_suggest_free_cdp_port_skips_used_ports(tmp_path, monkeypatch):
    """建议端口要避开所有类目已占用的端口(否则用户照抄后还是冲突)。"""
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    _write_cat_cfg(tmp_path, "CatA", "http://127.0.0.1:9222", "a@x.com")
    _write_cat_cfg(tmp_path, "CatB", "http://127.0.0.1:9223", "b@x.com")
    assert server.suggest_free_cdp_port() == 9224
    # 让某个类目"不算占用"(就是要给它换端口的那一个) → 可以把它的端口让出来
    assert server.suggest_free_cdp_port(ignore_cat="CatB") == 9223
