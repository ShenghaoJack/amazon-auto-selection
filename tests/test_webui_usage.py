# 单元测试: 看板的 LLM 用量/费用展示(Step 3, 2026-09-28)
# ---------------------------------------------------------------------------
# 需求: web 上要能看到"这类目/这一步烧了多少 token、花了多少钱", 并且
# **文本模型与图片模型必须分开**(两个 key、两个端点, 图片 token 由服务端按尺寸折算
# 进 prompt → 只看合计看不出图片花了多少)。
#
# 服务端新增 `cat_usage(mdir)` / `usage_lite(u)`, 并注入 `/api/status`(含逐步明细)与
# `/api/history`(只要合计)。**缓存是硬要求**: 实测解析开销几乎全在 JSON 上 ——
# 1.9MB/5k 次调用 ≈ 25ms, 19.3MB/50k ≈ 245ms; 而 /api/history 每 12s 轮 12 个类目,
# 不缓存最坏 ~2.9s/次。故按账本 (mtime,size) 指纹缓存, 下面专门钉住它。
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Demo Cat-US-202609"
RUN = "Demo Cat-US-202609-20260923-200415"
RUN_OLD = "Demo Cat-US-202609-20260920-101010"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_usage",
                                                  ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp):
    s = _load_server()
    s.PROJECT_ROOT = Path(ws_tmp)
    s._USAGE_CACHE.clear()
    return s


def _mk(ws_tmp, cat: str = CAT) -> Path:
    """建类目目录 + 配置。DATA_ROOT 必须是**绝对路径** —— 相对时 monitor_dir 会再拼
    一次 PROJECT_ROOT(实测会得到 `<root>/<root>/data/...`), 于是账本永远找不到。"""
    root = Path(ws_tmp)
    d = root / "data" / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{cat}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n', encoding="utf-8")
    return d


def _mdir(ws_tmp, cat: str = CAT) -> Path:
    from core.paths import STEP_DIR_NAMES
    from steps.ids import MONITOR_STEP
    return Path(ws_tmp) / "data" / cat / STEP_DIR_NAMES[MONITOR_STEP]


def _write(ws_tmp, recs, cat: str = CAT, name: str = "llm-usage.jsonl") -> Path:
    p = _mdir(ws_tmp, cat) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _meta(run: str = RUN, cat: str = CAT,
          src: str = "官网实时(2026-09-28 10:28, CNY)") -> dict:
    return {"type": "meta", "run_id": run, "cat": cat,
            "price_source": src, "currency": "CNY"}


def _step(run: str, step: int, calls: int, cost: float, *, cat: str = CAT,
          hit: int = 0, miss: int = 1000, out: int = 100, ch: dict | None = None,
          status: str = "DONE") -> dict:
    # 注意: summarize_run 的 total_tokens 是**由 in_hit+in_miss+out 派生**的,
    # 不读记录里写的 total_tokens(避免两个数打架) → 夹具要自己保证三者自洽。
    return {"type": "step-summary", "run_id": run, "cat": cat, "step": step,
            "status": status, "calls": calls, "failed_attempts": 0,
            "in_hit": hit, "in_miss": miss, "out": out,
            "total_tokens": hit + miss + out,
            "cost_cny": cost, "by_channel": ch or {}, "by_model": {}}


def _data_root(ws_tmp) -> str:
    return str(Path(ws_tmp) / "data")


# --------------------------------------------------------------- ① 取数

def test_totals_are_cumulative_across_runs_with_current_run_split(server, ws_tmp):
    """**累计口径**(2026-09-29 用户定稿): 合计 = 该类目历史 + 本次, 永不因重置清零;
    本轮(最后写入的 run_id)的量另给 run_calls/run_total_tokens/run_cost_cny。"""
    _mk(ws_tmp)
    _write(ws_tmp, [
        _meta(RUN_OLD), _step(RUN_OLD, 17, 999, 99.0),
        _meta(RUN), _step(RUN, 17, 3, 1.5),
    ])
    u = server.cat_usage(_mdir(ws_tmp))
    assert u["run_id"] == RUN
    assert u["calls"] == 1002                 # 999(历史轮) + 3(本轮)
    assert u["cost_cny"] == pytest.approx(100.5)
    assert u["run_calls"] == 3 and u["run_cost_cny"] == pytest.approx(1.5)
    assert u["runs"] == 2
    assert u["steps"]["17"]["calls"] == 1002
    assert u["steps"]["17"]["run_calls"] == 3


def test_killed_step_call_records_count_into_totals(server, ws_tmp):
    """被强杀那一步只有 call 记录(没有 step-summary) → 累计里也要算上, 不能凭空丢。"""
    def _call(cost: float, ts: str) -> dict:
        return {"type": "call", "run_id": RUN, "cat": CAT, "step": 10,
                "channel": "text", "model": "deepseek-flash", "in_hit": 0,
                "in_miss": 500, "out": 50, "cost_cny": cost, "priced": True, "ts": ts}

    _mk(ws_tmp)
    _write(ws_tmp, [
        _meta(RUN),
        _step(RUN, 10, 2, 0.2),                       # 上一次尝试: 有汇总(2 次调用)
        _call(0.1, "2026-09-29T06:10:00+08:00"),      # 上一次尝试的 2 条明细
        _call(0.1, "2026-09-29T06:10:01+08:00"),
        _call(0.25, "2026-09-29T16:30:00+08:00"),     # 本次被强杀, 只留下这一条
    ])
    u = server.cat_usage(_mdir(ws_tmp))
    assert u["calls"] == 3                            # 取更全的 call 侧, 不是只有 2
    assert u["cost_cny"] == pytest.approx(0.45)
    assert u["orphan_calls"] == 1
    assert u["steps"]["10"]["attempts"] == 2
    assert u["steps"]["10"]["last_ts"].startswith("2026-09-29T16:30")


def test_channel_split_reaches_both_totals_and_steps(server, ws_tmp):
    """文本/图片必须分开 —— 合计与逐步明细都要带 by_channel。"""
    _mk(ws_tmp)
    _write(ws_tmp, [
        _meta(RUN),
        _step(RUN, 17, 412, 8.91, hit=1000000, miss=530000, out=50000, ch={
            "text": {"calls": 200, "total_tokens": 600000, "cost_cny": 3.2},
            "vision": {"calls": 212, "total_tokens": 980000, "cost_cny": 5.71}}),
        _step(RUN, 7, 120, 0.42, ch={"text": {"calls": 120, "cost_cny": 0.42}}),
    ])
    u = server.cat_usage(_mdir(ws_tmp))
    assert set(u["by_channel"]) == {"text", "vision"}
    assert u["by_channel"]["vision"]["cost_cny"] == pytest.approx(5.71)
    assert set(u["steps"]) == {"17", "7"}
    assert u["steps"]["17"]["by_channel"]["vision"]["calls"] == 212
    assert u["steps"]["17"]["total_tokens"] == 1580000
    assert u["steps"]["7"]["cost_cny"] == pytest.approx(0.42)
    assert u["price_source"].startswith("官网实时")


def test_legacy_ledger_without_channel_goes_to_unknown(server, ws_tmp):
    """老账本(Step 1/2 早期写的, 无 by_channel) → 归到 unknown, 不崩、不伪造模型名。"""
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 5, 0.77, ch=None)])
    u = server.cat_usage(_mdir(ws_tmp))
    assert u["calls"] == 5
    assert set(u["by_channel"]) == {"unknown"}
    assert u["by_channel"]["unknown"]["calls"] == 5


def test_empty_without_ledger(server, ws_tmp):
    _mk(ws_tmp)
    assert server.cat_usage(_mdir(ws_tmp)) == {}
    assert server.cat_usage(None) == {}


def test_empty_when_ledger_has_no_step_summary(server, ws_tmp):
    """只有 meta(比如刚开跑还没到步骤收尾) → 返回空, 前端据此隐藏费用块。"""
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN)])
    assert server.cat_usage(_mdir(ws_tmp)) == {}


# --------------------------------------------------------------- ② 缓存(性能硬要求)

def test_cache_avoids_reparse(server, ws_tmp, monkeypatch):
    """账本指纹不变 → **不重复解析**。这是 /api/history 每 12s 轮 12 个类目不卡的前提。"""
    import core.usage as cu

    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 3, 0.5)])
    n = {"load": 0}
    real = cu.load_records

    def counting(path):
        n["load"] += 1
        return real(path)

    monkeypatch.setattr(cu, "load_records", counting)
    assert server.cat_usage(_mdir(ws_tmp))["calls"] == 3
    assert n["load"] == 1
    for _ in range(5):
        server.cat_usage(_mdir(ws_tmp))
    assert n["load"] == 1, "指纹未变却又解析了一遍"


def test_cache_invalidates_when_ledger_grows(server, ws_tmp):
    """账本一变(运行中持续追加) → 指纹失效并重算。"""
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 7, 3, 0.5)])
    assert server.cat_usage(_mdir(ws_tmp))["calls"] == 3
    with open(_mdir(ws_tmp) / "llm-usage.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_step(RUN, 7, 4, 0.6), ensure_ascii=False) + "\n")
    u = server.cat_usage(_mdir(ws_tmp))
    assert u["calls"] == 7, "追加后必须重算"
    assert u["cost_cny"] == pytest.approx(1.1)


def test_cache_dropped_when_ledger_removed(server, ws_tmp):
    """账本被删(重置类目) → 返回空, 不能回放旧缓存。"""
    _mk(ws_tmp)
    p = _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 3, 0.5)])
    assert server.cat_usage(_mdir(ws_tmp))["calls"] == 3
    p.unlink()
    assert server.cat_usage(_mdir(ws_tmp)) == {}


def test_rotated_volume_is_read_too(server, ws_tmp):
    """轮转分卷里的记录也要算(同轮被轮转到分卷时不能少报)。"""
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 2, 0.2)],
           name="llm-usage-20260101-000000-1.jsonl")
    u = server.cat_usage(_mdir(ws_tmp))
    assert u["calls"] == 2


# --------------------------------------------------------------- ③ 接口注入

def test_usage_lite_is_summary_only(server, ws_tmp):
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 3, 0.5, ch={"text": {"calls": 3}})])
    lite = server.usage_lite(server.cat_usage(_mdir(ws_tmp)))
    assert "steps" not in lite, "类目中心只要合计(逐步明细留给运行台)"
    assert set(lite) == {"run_id", "calls", "total_tokens", "cost_cny",
                         "run_calls", "run_total_tokens", "run_cost_cny",
                         "by_channel", "currency", "price_source"}
    assert server.usage_lite({}) == {}
    assert server.usage_lite(None) == {}


def test_status_snapshot_carries_usage_with_steps(server, ws_tmp):
    """/api/status(运行台)要带**逐步明细** —— ② 步骤卡片的费用行靠它。"""
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 3, 0.5), _step(RUN, 7, 1, 0.1)])
    snap = server.status_snapshot({"CAT": CAT, "DATA_ROOT": _data_root(ws_tmp)},
                                  0, run_active=False)
    assert snap["usage"]["calls"] == 4
    assert set(snap["usage"]["steps"]) == {"17", "7"}
    assert snap["usage"]["steps"]["17"]["cost_cny"] == pytest.approx(0.5)


def test_status_snapshot_without_ledger_is_empty(server, ws_tmp):
    """没账本时 usage 为空 dict(前端据此隐藏整块), 且不得影响其它字段。"""
    _mk(ws_tmp)
    snap = server.status_snapshot({"CAT": CAT, "DATA_ROOT": _data_root(ws_tmp)},
                                  0, run_active=False)
    assert snap["usage"] == {}
    assert snap["cat"] == CAT and "steps" in snap


def test_api_history_carries_usage_lite(server, ws_tmp):
    """类目中心列表每项带 usage(只要合计) —— 「累计运行时长 / 费用」第二行靠它。"""
    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 3, 1.25,
                                    ch={"text": {"calls": 2, "cost_cny": 1.0},
                                        "vision": {"calls": 1, "cost_cny": 0.25}})])
    h = server.Handler.__new__(server.Handler)
    out = h._api_history()
    item = [i for i in out["items"] if i["cat"] == CAT][0]
    assert item["usage"]["calls"] == 3
    assert item["usage"]["cost_cny"] == pytest.approx(1.25)
    assert set(item["usage"]["by_channel"]) == {"text", "vision"}
    assert "steps" not in item["usage"]


def test_api_history_without_ledger_has_empty_usage(server, ws_tmp):
    _mk(ws_tmp)
    h = server.Handler.__new__(server.Handler)
    item = [i for i in h._api_history()["items"] if i["cat"] == CAT][0]
    assert item["usage"] == {}


# --------------------------------------------------------------- ④ 前端

def test_index_html_renders_usage_everywhere(server):
    """前端三处: ② 步骤卡片费用行 / 顶部合计 / 类目中心两行单元格。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    for fn in ("function fmtTok", "function fmtYuan", "function usageLine",
               "function usageTip", "function hubCostHtml", "function hubCostText"):
        assert fn in html, f"缺格式化函数: {fn}"
    assert 'id="costVal"' in html and 'id="costLine"' in html, "顶部合计未接"
    assert 'class="usgrow"' in html or "top usgrow" in html, "步骤卡片费用行未接"
    assert 'class="usg"' in html, "步骤卡片费用行未接"
    assert 'class="durv"' in html and 'class="costv"' in html, "类目中心两行单元格未接"
    assert "data.usage.steps" in html, "步骤卡片没读 /api/status 的 usage"
    assert "it.usage" in html, "类目中心没读 usage"


def test_pollhub_does_not_wipe_cost_cell(server):
    """回归: `pollHubDur` 每 12s 刷那一列, **必须改写内层 span**。

    原实现是 `cell.textContent = …` 整格覆盖 —— 若照旧, 费用那行会在页面停留
    十几秒后被静默清掉(表现为"费用显示一会儿就没了")。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert 'cell.querySelector(".durv")' in html, "应改写内层 .durv"
    assert 'cell.querySelector(".costv")' in html, "应同时刷新费用行"
    assert 'cell.textContent = (typeof v === "number" && v > 0) ? fmtClock(v) : "—";' not in html, \
        "pollHubDur 不能整格覆盖(会把费用行抹掉)"


def test_index_html_step_card_hides_usage_when_no_calls(server):
    """没调过 LLM 的步骤(如步骤19)不显示费用行 —— 不刷噪音。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "if (!usTxt)" in html and 'usRow.style.display = "none"' in html
    assert "usageLine(us)" in html


def test_index_html_says_cumulative_and_shows_current_run(server):
    """文案必须写清"累计"(2026-09-29 用户定稿): 否则又会把累计数当成"这一轮刚烧的"。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "累计" in html
    assert 'head || "LLM 用量(累计)"' in html, "usageTip 默认标题应写累计"
    assert "u.run_calls" in html, "tooltip 应给「本轮」一行(与本轮不同时)"
    assert "含历史轮次/重跑/中断" in html
    assert "重置步骤/重置类目都不清零" in html
    assert "最近一轮" not in html, "不得再写「最近一轮」(那是按 run_id 的旧口径)"
    assert 'us.attempts' in html and "最近一次" in html, "步骤卡片 tooltip 应给尝试次数/最近时间"


# --------------------------------------------------------------- ⑤ 真起 HTTP 打一次

def test_http_endpoints_expose_usage(server, ws_tmp, monkeypatch):
    """端到端: 两个接口的 JSON 里都取得到 usage(顺带证明嵌套结构可被 JSON 序列化)。"""
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    from webui import auth

    monkeypatch.setattr(auth, "DB_PATH", Path(ws_tmp) / "auth.sqlite3")
    monkeypatch.delenv("WEBUI_PUBLIC_ORIGIN", raising=False)
    auth.create_user("reader", "readonly-test-password", "viewer", actor="test", first=True)
    token = auth.login("reader", "readonly-test-password")

    _mk(ws_tmp)
    _write(ws_tmp, [_meta(RUN), _step(RUN, 17, 3, 1.25, ch={
        "text": {"calls": 2, "cost_cny": 1.0},
        "vision": {"calls": 1, "cost_cny": 0.25}})])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/api/history", headers={"Cookie": "wui_session=" + token}), timeout=10) as f:
            hist = json.loads(f.read().decode("utf-8"))
        item = [i for i in hist["items"] if i["cat"] == CAT][0]
        assert item["usage"]["calls"] == 3
        assert set(item["usage"]["by_channel"]) == {"text", "vision"}
        assert "steps" not in item["usage"], "类目中心只要合计"

        url = (base + "/api/status?after=0&file="
               + urllib.parse.quote(f"data/{CAT}/config.yaml"))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Cookie": "wui_session=" + token}), timeout=10) as f:
            st = json.loads(f.read().decode("utf-8"))
        assert st["usage"]["steps"]["17"]["total_tokens"] == 1100
        assert st["usage"]["steps"]["17"]["by_channel"]["vision"]["cost_cny"] == 0.25
    finally:
        httpd.shutdown()
        httpd.server_close()
