# 单元测试: webui 服务端"看板跟着哪个类目"的解析 + 残留 RUNNING 判定(2026-09-14)
# ---------------------------------------------------------------------------
# 用户报障: 在 🏠类目中心 选不同类目「打开运行台」后, ①运行控制(配置面板)显示所选类目,
#   ②运行状态 / ③实时日志 却显示**正在运行的另一个类目**。
# 根因: `_status_cfg` 运行中无视 file 一律返回运行类目, 只有请求带 `browse=1` 才按 file;
#   而「打开运行台」(`enterRunWith`) 不带 browse, 运行台历史「查看」才带 → 同一页面两个类目。
#   实测(2026-09-14, 真实服务 on 8790): /api/status?file=data/Dog Food Storage-.../config.yaml
#   返回 cat=Grilling Prep and Serve Trays-... 且日志是它的 [Step10Variants] 行。
#   同一函数还被 /api/files /api/download /api/keyword-lists **和写入路径
#   /api/keyword-lists/save** 共用(后者当时连 browse 参数都没有) → 运行中保存会写进别的类目。
# 修复: file 唯一决定类目; 只有解析不出 CAT(未传 file/文件不存在/配置缺 CAT)才回落运行类目。
import importlib.util
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

CAT_A = "Grilling Prep and Serve Trays-US-202608"   # "正在运行"的类目
CAT_B = "Dog Food Storage-US-202608"                # 用户在类目中心选择的类目


def _load_server():
    """按文件路径加载 webui/server.py(它不在包内, 与 test_webui_client_abort.py 同一套路)。"""
    spec = importlib.util.spec_from_file_location("webui_server_statuscfg", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp, monkeypatch):
    s = _load_server()
    root = Path(ws_tmp)
    monkeypatch.setattr(s, "PROJECT_ROOT", root)
    return s


class _FakeRunner:
    """替身 RunManager: 提供 _status_cfg/_api_status/_api_history 用到的查询接口。

    2026-09-15 多类目并行: RunManager 由"单进程"改为"按类目的多运行注册表",
    取运行信息要带类目(`snapshot(cat)`) —— 替身同步跟上, 并支持多类目同时在跑。
    """

    def __init__(self, snap: dict, others: list | None = None):
        self._snap = dict(snap)
        self._others = [dict(o) for o in (others or [])]

    def _all(self) -> list[dict]:
        out = []
        if self._snap:
            out.append(dict(self._snap))
        out.extend(dict(o) for o in self._others)
        return out

    def snapshot(self, cat: str | None = None) -> dict:
        if cat:
            for r in self._all():
                if str(r.get("cat") or "") == str(cat):
                    return dict(r)
            return {}
        live = [r for r in self._all() if r.get("active")]
        return dict(live[-1]) if live else (dict(self._snap) if self._snap else {})

    def snapshots(self) -> list[dict]:
        return self._all()

    def active_cats(self) -> list[str]:
        return sorted({str(r.get("cat") or "") for r in self._all() if r.get("active")} - {""})

    def active_for(self, cat: str) -> bool:
        return any(r.get("active") and str(r.get("cat") or "") == str(cat) for r in self._all())

    @property
    def active(self) -> bool:
        return any(r.get("active") for r in self._all())

    def ring_for(self, cat: str):
        return None


def _handler(server):
    """无 socket 的 Handler 实例(__init__ 会去摸请求流; 这里只用实例方法)。"""
    return server.Handler.__new__(server.Handler)


def _make_cat(root: Path, dirname: str, cat: str) -> Path:
    """建 data/<dirname>/config.yaml(CAT + DATA_ROOT 指向本临时目录)。"""
    d = root / "data" / dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / "config.yaml"
    p.write_text(f'CAT: "{cat}"\nDATA_ROOT: "{root.as_posix()}"\n', encoding="utf-8")
    return p


def _write_step_status(root: Path, cat: str, num: int, status: str, start: str) -> Path:
    """写 step-status.txt, 与真实文件同格式(管道分隔: 步骤|状态|开始|结束|原因|acc|fails|ints)。"""
    mdir = root / cat / "20_备份与监控"
    mdir.mkdir(parents=True, exist_ok=True)
    p = mdir / "step-status.txt"
    p.write_text(f"{num}|{status}|{start}|||0|0|0\n", encoding="utf-8")
    return p


# --------------------------------------------------------------- 类目解析(file 优先)
def test_status_cfg_uses_requested_file_while_other_cat_running(server):
    """核心回归: 运行中的是 A, 请求 B 的配置 → 必须返回 B(原实现返回 A)。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _make_cat(root, "b", CAT_B)
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})

    got = server.Handler._status_cfg(None, "data/b/config.yaml")
    assert got["CAT"] == CAT_B, "看板/日志/产物/注入词必须按所选配置(用户报障点)"


def test_status_cfg_uses_requested_file_for_running_cat_too(server):
    """请求的正是运行类目 → 照常返回它(不能因为"运行中特殊"而走别的分支)。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})

    assert server.Handler._status_cfg(None, "data/a/config.yaml")["CAT"] == CAT_A


def test_status_cfg_falls_back_to_run_cat_when_file_unresolvable(server):
    """解析不出 CAT(未传 file / 文件不存在) → 回落运行类目, 兼容"不带 file 看运行任务"。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})

    assert server.Handler._status_cfg(None, None)["CAT"] == CAT_A
    assert server.Handler._status_cfg(None, "data/nope/config.yaml")["CAT"] == CAT_A


def test_status_cfg_without_file_follows_run_cat_even_with_root_template(server):
    """诊断口径: 不带 file 调 /api/status(如 curl) 要跟随运行类目, 不能返回根模板的占位 CAT。

    页面永远显式带 file, 所以这条只影响诊断/脚本调用; 而"显式带 file"仍以 file 为准
    (根模板/占位模板选了就显示它自己 —— 与"六模块同一个类目"的修复语义一致)。
    """
    root = server.PROJECT_ROOT
    (root / "config.yaml").write_text('CAT: "your-category-US-YYYYMM"\nDATA_ROOT: "data"\n',
                                      encoding="utf-8")
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})

    assert server.Handler._status_cfg(None, None)["CAT"] == CAT_A
    # 显式传根模板 -> 返回模板自己的(占位)CAT, 不再串到运行类目
    assert server.Handler._status_cfg(None, "config.yaml")["CAT"] == "your-category-US-YYYYMM"


def test_status_cfg_no_run_returns_requested_cat(server):
    """没任务在跑时本来就是 file 说了算(行为不变)。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "b", CAT_B)
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})

    assert server.Handler._status_cfg(None, "data/b/config.yaml")["CAT"] == CAT_B


def test_browse_param_is_gone(server):
    """browse 参数已废弃: 带不带都必须按 file(防止旧前端/旧书签又把类目切走)。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _make_cat(root, "b", CAT_B)
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})

    q_with = {"file": ["data/b/config.yaml"], "browse": ["1"]}
    assert server.Handler._status_cfg(None, q_with.get("file")[0])["CAT"] == CAT_B
    assert not hasattr(server.Handler, "_is_browse")


# --------------------------------------------------------------- ②运行状态: 残留 RUNNING 判定
def test_api_status_keeps_running_step_for_its_own_cat(server, monkeypatch):
    """看的是正在跑的类目 → 它的 RUNNING 步骤保持 RUNNING(cat_running=True)。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _write_step_status(root, CAT_A, 7, "RUNNING", "2026-09-14 10:53:20")
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root),
                                 "started_at": "2026-09-14 10:53:14", "pid": 4242})

    snap = _handler(server)._api_status({"file": ["data/a/config.yaml"]}, "/api/status")
    assert snap["cat"] == CAT_A
    assert snap["cat_running"] is True
    step7 = [s for s in snap["steps"] if s["number"] == 7][0]
    assert step7["status"] == "RUNNING"
    assert snap["run"]["active"] is True          # 全局运行信息仍返回(横幅/停止按钮用)


def test_api_status_marks_stale_running_when_viewing_other_cat(server, monkeypatch):
    """看的是**别的**类目(A 在跑) → B 自己的残留 RUNNING 按中断显示, 且不显示为"本类目在跑"。

    原实现把全局运行态直接塞给 B 的快照 → B 的历史残留 RUNNING 被当成"本轮在跑",
    还会把 A 的启动时间当作 B 的判定基准。
    """
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _make_cat(root, "b", CAT_B)
    p = _write_step_status(root, CAT_B, 16, "RUNNING", "2026-09-13 18:20:00")
    import os
    old = 1757000000.0                              # 2025-09 左右: 确保不被"近 5 分钟活动"兜底命中
    os.utime(p, (old, old))
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root),
                                 "started_at": "2026-09-14 10:53:14", "pid": 4242})

    snap = _handler(server)._api_status({"file": ["data/b/config.yaml"]}, "/api/status")
    assert snap["cat"] == CAT_B
    assert snap["cat_running"] is False, "B 类目自己没进程在跑"
    step16 = [s for s in snap["steps"] if s["number"] == 16][0]
    assert step16["status"] == "INTERRUPTED"
    # 多类目并行(2026-09-15): 运行横幅/控制台按**所看类目**取 —— 看 B 时 `run` 是空的
    # (B 没有本服务的子进程), A 的运行信息改由 `other_runs` 带出(前端提示"并行中: A")。
    assert snap["run"] == {}, "看 B 时不应把 A 的运行记录当成 B 的横幅"
    assert [r["cat"] for r in snap["other_runs"]] == [CAT_A], "别的类目的运行要在 other_runs 里可见"
    assert snap["other_runs"][0]["pid"] == 4242


def test_api_status_does_not_borrow_other_cat_run_state(server, monkeypatch):
    """尖锐用例: B 的残留 RUNNING **开始时间晚于** A 的启动时间。

    旧实现把 A 的 started_at 当作 B 的基准(run_started=A 的启动) → `t_start < t_run` 不成立
    → B 的残留被当成"正在跑"; 新实现按"本类目有无进程"判定, B 无进程 → 中断。
    """
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _make_cat(root, "b", CAT_B)
    p = _write_step_status(root, CAT_B, 12, "RUNNING", "2026-09-14 11:30:00")  # 晚于 A 的 10:53
    import os
    old = 1757000000.0
    os.utime(p, (old, old))
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root),
                                 "started_at": "2026-09-14 10:53:14", "pid": 4242})

    snap = _handler(server)._api_status({"file": ["data/b/config.yaml"]}, "/api/status")
    step12 = [s for s in snap["steps"] if s["number"] == 12][0]
    assert step12["status"] == "INTERRUPTED", "别类目的运行态不能借给本类目"


def test_api_status_cli_run_visible_via_lock(server, monkeypatch):
    """CLI/外部进程跑(无 RUNNER 记录)时, 该类目仍按"在跑"显示(锁兜底), 供③日志走文件尾部。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _write_step_status(root, CAT_A, 9, "RUNNING", "2026-09-14 10:53:20")
    monkeypatch.setattr(server, "RING", server.RingLog())
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (True, "2026-09-14 10:53:14", False))
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})

    snap = _handler(server)._api_status({"file": ["data/a/config.yaml"]}, "/api/status")
    assert snap["cat_running"] is True
    # 多类目并行(2026-09-15): CLI 跑的类目本服务没有运行记录 → `run` 为空, 但
    # `cat_running` 由 .run.lock 兜底为 True(③日志改走文件尾部)。
    assert snap["run"] == {}                       # 不是本服务的子进程 → ③日志用文件尾部
    step9 = [s for s in snap["steps"] if s["number"] == 9][0]
    assert step9["status"] == "RUNNING"


# --------------------------------------------------------------- 前端不再依赖 browse 标记
def test_index_html_has_no_browse_flag(server):
    """页面所有数据请求只带 file: ②③⑤⑥ 与①运行控制用同一个 selFile(原实现靠 browse 才一致)。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "browse=1" not in html
    assert "__viewOther" not in html
    for ep in ("/api/status?file=", "/api/download?file=", "/api/step-files?file=",
               "/api/keyword-lists?file=", "/api/log?file="):
        assert ep in html, f"缺数据请求: {ep}"
    assert "cat_running" in html                    # ③日志模式按"本类目是否有进程"判定


# ------------------------------------------------ 类目中心「累计运行时长」列(2026-09-15)
def _write_step_status_full(root: Path, cat: str, rows: list) -> Path:
    """写多行 step-status.txt(num|status|start|end|reason|acc|fails|ints), 与真实格式同。

    ⚠️ 落点必须是 `monitor_dir(cat, DATA_ROOT)` = **<DATA_ROOT>/<CAT>/20_备份与监控**
    (不是配置所在的 `data/<目录名>/…`) —— 类目数据目录由配置里的 DATA_ROOT + CAT 决定,
    目录名可与 CAT 不同(错位配置会被防呆拦, 但路径语义仍以 CAT 为准)。
    """
    mdir = root / cat / "20_备份与监控"
    mdir.mkdir(parents=True, exist_ok=True)
    p = mdir / "step-status.txt"
    p.write_text("\n".join("|".join(str(c) for c in r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_api_history_has_dur_sec_same_as_dashboard(server, monkeypatch):
    """类目中心列表新增 dur_sec(累计执行时长), 与 ②看板 durations.total_sec **同一个数**。

    用户需求(2026-09-15): "在类目中心的表格中加入一列「累计运行时长」, 与 web 页面中的一样"。
    真实数据对账已确认二者差值为 0(Grilling: 46234s); 这里用构造数据把口径差异钉死:
    - 含 acc 的跨段步骤 10 (7200 acc + 1682 段) 与步骤 00 的 73s → 合计 8955s;
    - 未开始的步骤(如 16 号 PENDING)不计;
    - 同一份状态喂给 _api_status, 两处必须相等。
    """
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    _write_step_status_full(root, CAT_A, [
        [0, "DONE", "2026-09-14 10:53:14", "2026-09-14 10:54:27", "", 0, 0, 0],
        [10, "DONE", "2026-09-14 19:45:08", "2026-09-14 20:13:10", "", 7200, 0, 1],
        [16, "PENDING", "", "", "", 0, 0, 0],
        [17, "PENDING", "", "", "", 0, 0, 0],
    ])
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})

    hist = _handler(server)._api_history()
    item = [it for it in hist["items"] if it["cat"] == CAT_A][0]
    assert item["dur_sec"] == 73 + 7200 + 1682 == 8955

    snap = _handler(server)._api_status({"file": ["data/a/config.yaml"]}, "/api/status")
    assert item["dur_sec"] == snap["durations"]["total_sec"], "类目中心列必须与运行台 ⏱ 同口径"


# ------------------- 运行中两端时长不统一修复(2026-09-16 用户报障)
def _ts_ago(minutes: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _mk_pipeline_log(stp: Path, lines: list) -> None:
    (stp.parent / "pipeline-progress.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_running_step_counts_to_now_in_both_views(server, monkeypatch):
    """用户报障: "类目运行的时候, 类目中心的累计运行时长 与 运行台的累计执行时长 不统一,
    执行完了以后就正常了"。

    根因: `cumulative_seconds(end_hint_for=…)` 会把**任何** RUNNING 步骤截断到"日志最后活动时刻",
    而该 hint 只为"残留 RUNNING(进程已死、日志停了)"设计 → **真在跑**的步骤也被截断, 于是总量
    只在该步写一条日志时才跳一次(步骤10 约 4 秒、步骤16 约 135 秒 → 一次冻结两分多钟), 两个页面
    在不同瞬间取数 → 数字不统一; 步骤一跑完就有 `end` → 走 `end-start` → 两边立刻一致(即"跑完就正常")。
    修后: 属于**当前存活进程段**(start >= run_started / live_started)的 RUNNING 步骤按 now 每秒计。
    """
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    stp = _write_step_status_full(root, CAT_A, [
        [0, "DONE", _ts_ago(120), _ts_ago(110), "", 0, 0, 0],       # 固定 600 秒
        [10, "RUNNING", _ts_ago(60), "", "", 0, 0, 0],              # 60 分钟前开始(真在跑)
    ])
    # 日志**30 分钟前就停了**(模拟"这一步很久才写一行日志")
    _mk_pipeline_log(stp, [f"{_ts_ago(30)} [INFO] [Step10Variants] 已抓取 B000C91625 ok=True"])
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})
    run_started = _ts_ago(90)        # 本进程 90 分钟前启动 → 步骤10(60 分钟前)属于本段

    snap = server.status_snapshot({"CAT": CAT_A, "DATA_ROOT": str(root)}, None,
                                  run_active=True, run_started=run_started, ring=server.RING)
    total = snap["durations"]["total_sec"]
    assert 600 + 3590 <= total <= 600 + 3610, \
        f"真在跑的步骤应按 now 计(≈4200s), 而不是被日志冻结在 30 分钟(1800s); 实际 {total}"

    # 类目中心同口径: 存活进程时刻由 .run.lock 探测得来(这里直接给替身)
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (True, run_started, False))
    item = [it for it in _handler(server)._api_history()["items"] if it["cat"] == CAT_A][0]
    assert abs(item["dur_sec"] - total) <= 2, \
        f"两端必须同口径(类目中心 {item['dur_sec']} vs 运行台 {total})"


def test_residual_running_step_still_truncated_to_log(server, monkeypatch):
    """残留 RUNNING(开始早于本进程段)仍按"日志最后活动时刻"截断 —— 保护不变, 且两端一致。

    这正是 hint 当初的用途: 进程被强杀后"日志停了、进程还没退"的闲置段不计入累计。
    """
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    stp = _write_step_status_full(root, CAT_A, [
        [0, "DONE", _ts_ago(300), _ts_ago(290), "", 0, 0, 0],       # 固定 600 秒
        [16, "RUNNING", _ts_ago(180), "", "", 0, 0, 0],             # 3 小时前开始 → 残留
    ])
    _mk_pipeline_log(stp, [f"{_ts_ago(150)} [INFO] [Step16AttributeAnnotation] 标注进度 40/329"])
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": True, "cat": CAT_A, "data_root": str(root)})
    run_started = _ts_ago(60)        # 本进程 1 小时前启动 → 步骤16 开始于 3 小时前 → 残留

    snap = server.status_snapshot({"CAT": CAT_A, "DATA_ROOT": str(root)}, None,
                                  run_active=True, run_started=run_started, ring=server.RING)
    total = snap["durations"]["total_sec"]
    assert 600 + 1790 <= total <= 600 + 1810, \
        f"残留步骤应截断到日志最后活动(≈2400s), 而不是算到 now(≈9000s); 实际 {total}"
    st16 = [s for s in snap["steps"] if s["number"] == 16][0]
    assert st16["status"] == "INTERRUPTED", "残留 RUNNING 仍应标 INTERRUPTED"

    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (True, run_started, False))
    item = [it for it in _handler(server)._api_history()["items"] if it["cat"] == CAT_A][0]
    assert abs(item["dur_sec"] - total) <= 2, \
        f"残留场景两端也要一致(类目中心 {item['dur_sec']} vs 运行台 {total})"


def test_no_live_info_keeps_old_truncation(server, monkeypatch):
    """判不出存活进程时(`live_started` 缺省=空) → **保持旧行为**(全部 RUNNING 按日志活动截断)。

    保守方向: 宁可少算"日志停了到进程退出"的闲置段, 也不把闲置时间算进执行时长。
    """
    root = server.PROJECT_ROOT
    _make_cat(root, "a", CAT_A)
    stp = _write_step_status_full(root, CAT_A, [
        [10, "RUNNING", _ts_ago(60), "", "", 0, 0, 0],
    ])
    _mk_pipeline_log(stp, [f"{_ts_ago(30)} [INFO] [Step10Variants] 已抓取 B000C91625 ok=True"])
    mdir = stp.parent
    assert server._cat_cumulative_sec(mdir, CAT_A) == 1800
    # 给了存活进程时刻(且该步属于本段) → 改按 now 计
    got = server._cat_cumulative_sec(mdir, CAT_A, live_started=_ts_ago(90))
    assert 3590 <= got <= 3610, got


def test_api_history_dur_sec_zero_when_no_status(server, monkeypatch):
    """没有 step-status.txt 的类目 dur_sec=0(不算错表, 前端显示「—」)。"""
    root = server.PROJECT_ROOT
    _make_cat(root, "b", CAT_B)
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})
    hist = _handler(server)._api_history()
    item = [it for it in hist["items"] if it["cat"] == CAT_B][0]
    assert item["dur_sec"] == 0


def test_index_html_has_duration_column(server):
    """类目中心表格确实渲染了「累计运行时长」列(表头/取值/刷新都在)。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "累计运行时长" in html                     # 表头(表格视图)
    assert "it.dur_sec" in html                      # 取值来自 /api/history
    assert "pollHubDur" in html                      # 停留页面时静默刷新该列
    assert 'colspan="6"' in html                     # 新增列后空行/错误行的跨列数已同步

# ------------------------------- 配置下拉"记住的值失效"时不得静默跳到别类目(2026-09-15 用户报障)
def test_index_html_never_silently_switches_category_on_stale_config(server):
    """用户报障: "不运行的时候运行控制里的配置文件会变成别的名称"。

    实测复现: localStorage 里记的配置**不在** /api/configs 列表时(根模板 config.yaml、
    已删除/改名类目、或被清理的历史快照路径 —— 快照不进该列表), 原实现直接
    `selFile = d.configs[0].name`, 而 configs[0] 是**字母序第一个类目** → 运行控制显示成
    `Air Purifiers for Home Pets-US-202608`(实测), 且**不写回 localStorage**, 于是每次刷新都再跳一次
    —— 用户可能据此误启动别的类目。
    现在要求: ①按类目找回(记住的 wui_cat, 或从 data/<CAT>/config.yaml、run-<ts>-<CAT>-config-*.yaml 解析);
    ②找不回才退回第一个, 且**必须弹提示**; ③选中类目时记住 wui_cat。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "function resolveFallbackConfig" in html, "缺少按类目找回的实现"
    assert "function snapshotCatOf" in html and "function catOfValue" in html, "缺少从快照名解析类目"
    assert "function rememberCat" in html, "缺少记住当前类目"
    assert "rememberCat(catOfValue(selFile))" in html, "下拉切换时要记住类目"
    # 旧的无条件静默回退必须消失
    assert "selFile = d.configs[0] ? d.configs[0].name : \"config.yaml\";" not in html, \
        "旧的静默回退还在"
    assert "已不在可用列表" in html, "失效时必须给出提示"
    assert "请确认这是你要跑的类目" in html, "无类目线索时的回退也要提示确认"
    # 启动时的"回到运行台"判断也要优先用记住的类目
    assert "const wantCat = localStorage.getItem(\"wui_cat\")" in html


# ------------------------- 配置下拉标签必须按内容判断(2026-09-15 用户截图报障)
def test_index_html_config_option_label_is_not_the_path(server):
    """用户截图: 从类目中心进运行台, 配置显示成
    `data/Chopping Boards-FR-202608/config.yaml · (历史快照)` —— 它其实是**类目配置**,
    只是补选项的代码无条件加了"(历史快照)"后缀、还把原始路径当文字(viewHistoryRun / enterRunWith
    两处; scopeCfgDropdown 也用过原始路径), 于是"类目名"被路径取代。
    现在要求: 真快照(`.webui/runs/run-…`)才带"· (历史快照)"; 类目配置一律显示**类目名 + REAL/DRY**。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "function isSnapshotPath" in html and "function cfgLabelFor" in html, "缺少标签判断实现"
    assert 'o.textContent = file + " · (历史快照)";' not in html, "补选项仍在无条件用路径+历史快照"
    assert html.count("o.textContent = cfgLabelFor(file);") == 2, \
        "viewHistoryRun / enterRunWith 都要用统一标签"
    assert "ensureCfgOption(selFile, cfgLabelFor(selFile))" in html, "收窄下拉时也要用统一标签"
    assert 'selFile.startsWith("run-") ? selFile' not in html, "旧的路径标签写法还在"
    assert "__cfgList = d.configs || [];" in html, "标签要能判断 REAL/DRY, 需缓存配置清单"
    assert 'if (isSnapshotPath(v)) return v + " · (历史快照)";' in html, "真快照的后缀逻辑要保留"


# ------------------- 配置中心只留一个兜底开关(2026-09-16 用户"太多参数了, 我只需要个兜底的就行")
def test_index_html_registers_single_fallback_switch(server):
    """配置中心必须把 `FALLBACK` 登记成**一等公民**, 并把已内化的 7 个重试键彻底隐藏。

    背景: 配置中心按 `GROUPS` + "未登记键"兜底分组渲染。若 `FALLBACK` 没登记, 它会掉进
    "其他参数(未登记说明的键)"那一坨(没有专属说明); 而 `REQUEST_RETRY`/`BROWSER_*` 等
    内化键若只从 yaml 删掉、界面仍登记着, 一旦从旧备份恢复配置就会重新冒出来。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    # ① FALLBACK 有标签 / 有专属分组 / 有说明 / 是下拉(auto|off)
    assert 'FALLBACK:"浏览器兜底开关' in html, "缺少 FALLBACK 的中文标签"
    assert '{id:"兜底(浏览器补抓)", keys:["FALLBACK"]}' in html, "缺少 FALLBACK 的专属分组"
    assert 'key === "FALLBACK" ? ["auto", "off"]' in html, "FALLBACK 应渲染为 auto/off 下拉"
    assert 'auto · 直连失败自动用浏览器补抓' in html, "下拉选项要给中文解释"
    m = re.search(r"FALLBACK:\"步骤01 详情 \+ 步骤10 变体共用的唯一抓取开关", html)
    assert m, "缺少 FALLBACK 的说明(hint)"
    # ② 已内化的 7 个键: 全部进 HIDDEN_KEYS, 且**不再出现在任何分组**
    hidden = html.split("const HIDDEN_KEYS", 1)[1][:1500]
    for k in ("FETCH_ROUND_PAUSE", "FETCH_STALL_ROUNDS", "FETCH_ASIN_TRIES", "REQUEST_RETRY",
              "BROWSER_FALLBACK_ROUNDS", "BROWSER_ROUND_PAUSE", "STEP01_BROWSER_FALLBACK"):
        assert k in hidden, f"{k} 应加入 HIDDEN_KEYS(否则旧备份恢复后会冒进表单)"
    groups = html.split("const GROUPS = [", 1)[1].split("];", 1)[0]
    for k in ("FETCH_ROUND_PAUSE", "FETCH_STALL_ROUNDS", "FETCH_ASIN_TRIES",
              "BROWSER_FALLBACK_ROUNDS", "BROWSER_ROUND_PAUSE", "STEP01_BROWSER_FALLBACK"):
        assert f'"{k}"' not in groups, f"{k} 不该再登记在配置中心的任何分组里"
    # ③ 防风控维度必须留着(用户要求"并发与间隔保留现值")
    for k in ("FETCH_WORKERS", "REQUEST_MIN_INTERVAL", "REQUEST_MAX_INTERVAL", "FETCH_LONG_EVERY"):
        assert f'"{k}"' in groups, f"{k} 应保留在界面(防风控维度, 用户明确要保留)"


# --------------- 类目级消息不得串进别的类目控制台(2026-09-16 用户报障"UK 混到 FR 的日志里")
def test_push_cat_routes_by_category_and_never_mirrors(server, monkeypatch):
    """`push_cat`: 有该类目缓冲 → 只写它; 没有 → 只写服务器级缓冲; **都不广播**给别的在跑类目。"""
    fr_ring, uk_ring = server.RingLog(), server.RingLog()
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RING.mirrors = [fr_ring, uk_ring]        # 模拟两个在跑类目都在接收"服务器级广播"

    class _R:
        def ring_for(self, cat):
            return uk_ring if str(cat).startswith("Chopping Boards-UK") else None

    monkeypatch.setattr(server, "RUNNER", _R())
    server.push_cat("Chopping Boards-UK-202608", "UK 的消息")
    server.push_cat("没有缓冲的类目", "无缓冲类目的消息")

    assert [e["text"] for e in uk_ring.tail(5)] == ["UK 的消息"]
    assert [e["text"] for e in fr_ring.tail(5)] == [], "UK 的消息不该被广播进 FR 的缓冲"
    assert [e["text"] for e in server.RING.tail(5)] == ["无缓冲类目的消息"]


def test_maintenance_output_stays_in_target_category_console(server, monkeypatch):
    """用户报障「UK 的怎么又混到了 FR 的实时日志中」的回归。

    实测(2026-09-16): 用户在 FR 的运行台看到 **18 行 UK 的「重置步骤10-17」输出**(含被删的
    15 个 `data/Chopping Boards-UK-202608/...` 路径)。根因: 维护命令 `_run_main_short` 把输出
    写进全局 `RING`, 而 `RingLog.push` 默认 `mirror=True` → 广播进**所有在跑类目**的缓冲
    (`RING.mirrors`, 由 RunManager 起停时 append/remove)。流水线子进程输出一直是"只进本类目
    缓冲", 唯独维护/配置这条路径漏了。修法: 维护输出按 CAT 路由(`push_cat`)+ 子进程行带 ring。
    """
    root = server.PROJECT_ROOT
    cat_uk = "Chopping Boards-UK-202608"
    _make_cat(root, "uk", cat_uk)
    fr_ring, uk_ring = server.RingLog(), server.RingLog()

    class _Runner:
        active = False

        def active_for(self, cat):
            return False

        def ring_for(self, cat):
            return uk_ring if str(cat) == cat_uk else None

    monkeypatch.setattr(server, "RUNNER", _Runner())
    monkeypatch.setattr(server, "RING", server.RingLog())
    server.RING.mirrors = [fr_ring]             # FR 正在跑 → 旧实现会把 UK 的输出也塞进去

    class _Res:
        returncode = 0
        stdout = ("已重置步骤 10-17, 删除 15 个产物\n"
                  f"  删除: D:/x/data/{cat_uk}/{cat_uk}-results.jsonl\n")

    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _Res())

    _handler(server)._run_main_short({"config_file": "data/uk/config.yaml"},
                                     "reset-step", "重置步骤10-17", step=10)

    uk_texts = [e["text"] for e in uk_ring.tail(50)]
    fr_texts = [e["text"] for e in fr_ring.tail(50)]
    assert any("重置步骤10-17" in t for t in uk_texts), "维护命令的开始行应进 UK 自己的缓冲"
    assert any("results.jsonl" in t for t in uk_texts), "子进程输出应进 UK 自己的缓冲"
    assert any("结束 code=0" in t for t in uk_texts), "结束行也应进 UK 自己的缓冲"
    assert fr_texts == [], f"UK 的维护输出不该出现在 FR 的运行台日志里: {fr_texts}"
    assert not any(cat_uk in e["text"] for e in server.RING.tail(50)), \
        "也不该广播进服务器级缓冲(那会被'无缓冲类目'的视图看到)"


# ------------------------------------------- 类目中心与运行台"在跑"口径统一(2026-09-16 报障)
def _mk_history_cat(root: Path, cat: str, rows: list, log_lines: list) -> Path:
    _make_cat(root, "uk", cat)
    stp = _write_step_status_full(root, cat, rows)
    _mk_pipeline_log(stp, log_lines)
    return stp


def _hist_item(server, cat: str) -> dict:
    return [it for it in _handler(server)._api_history()["items"] if it["cat"] == cat][0]


def test_api_history_orphan_alive_run_stays_running(server, monkeypatch):
    """**本轮报障复现**: 看板重启后它的内存注册表为空, 但 `.run.lock` 里的进程仍存活
    (上一实例启动的"孤儿运行") → 类目中心必须照常显示"运行中", 不能改判 INTERRUPTED。

    实测(2026-09-16 17:2x, 真实服务): UK 步骤16 真在跑(pid 10116、日志 17:24:05 写 380/984),
    运行台显示 RUNNING, 而类目中心显示"卡在步骤16(INTERRUPTED)" —— 两处打架, 用户据此以为"断了"。
    """
    root = server.PROJECT_ROOT
    run_started = _ts_ago(45)        # 存活进程(上一实例启动的)45 分钟前起
    _mk_history_cat(root, CAT_A, [
        [0, "DONE", _ts_ago(300), _ts_ago(290), "", 0, 0, 0],
        [16, "RUNNING", _ts_ago(40), "", "", 949, 0, 1],   # 步骤开始**晚于**进程启动 → 属于本段
    ], [f"{_ts_ago(1)} [INFO] [Step16AttributeAnnotation] 标注进度 380/984 (39%)"])
    # 注册表**空**(重启后): 既没有本服务的子进程, 也没有 active_cats
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})
    monkeypatch.setattr(server, "RING", server.RingLog())
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (True, run_started, False))

    item = _hist_item(server, CAT_A)
    assert item["running"] == ["16"], f"孤儿但在跑的步骤应显示运行中; 实际 {item}"
    assert item["failed"] == []
    assert item["current"] == "步骤16 运行中"
    assert item["can_resume"] is True


def test_api_history_step_before_live_process_is_residual(server, monkeypatch):
    """有存活进程, 但步骤**开始得比它更早** → 仍是残留(上一段进程的遗留), 与运行台一致。

    (运行台 status_snapshot 的 residual_nums 就是这条规则; 这里锁的是"两处同口径"。)
    """
    root = server.PROJECT_ROOT
    run_started = _ts_ago(30)
    _mk_history_cat(root, CAT_A, [
        [16, "RUNNING", _ts_ago(180), "", "", 949, 0, 1],   # 3 小时前起 → 早于存活进程(30 分钟前)
    ], [f"{_ts_ago(1)} [INFO] [Step16AttributeAnnotation] 标注进度 380/984 (39%)"])
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})
    monkeypatch.setattr(server, "RING", server.RingLog())
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (True, run_started, False))

    item = _hist_item(server, CAT_A)
    assert item["running"] == []
    assert item["failed"] == ["16"]
    assert item["current"] == "卡在步骤16(INTERRUPTED)", f"残留仍须标中断; 实际 {item}"


def test_api_history_no_process_no_registry_is_interrupted(server, monkeypatch):
    """无存活进程、注册表也没有 → 残留 RUNNING 照旧改判 INTERRUPTED(保护原有意图, 不回归)。"""
    root = server.PROJECT_ROOT
    _mk_history_cat(root, CAT_A, [
        [10, "RUNNING", _ts_ago(600), "", "", 0, 0, 0],
    ], [f"{_ts_ago(590)} [INFO] [Step10Variants] 已抓取 B000C91625 ok=True"])
    server.RUNNER = _FakeRunner({"active": False, "cat": "", "data_root": "data"})
    monkeypatch.setattr(server, "RING", server.RingLog())
    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))

    item = _hist_item(server, CAT_A)
    assert item["running"] == []
    assert item["failed"] == ["10"]
    assert item["current"] == "卡在步骤10(INTERRUPTED)"
