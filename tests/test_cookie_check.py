# 单元测试: COOKIE 有效性检测(HTTP 直连前置检查)
import os
from pathlib import Path
from unittest import mock

import pytest

from core.config import Config
from core.cookie_check import analyze_page, check_cookie
from core.http_client import HttpResponse


def _cfg(cookie: str = "k=v") -> Config:
    c = Config({"CAT": "t", "DATA_ROOT": "data"})
    c.set("COOKIE", cookie)
    c.set("MARKET", "amazon.com")
    return c


def test_analyze_page_classification():
    assert analyze_page('<span id="productTitle">Salt grinder</span>') == "ok"
    assert analyze_page('<span id="productTitle">x</span> more') == "ok"
    assert analyze_page("captcha required") == "bot"
    assert analyze_page("Automated access from a computer network has been detected") == "bot"
    assert analyze_page("Sign in to continue /ap/signin") == "signin"
    assert analyze_page("something went wrong") == "error"
    assert analyze_page("<html>normal page</html>") == "unknown"


def test_check_cookie_no_cookie():
    r = check_cookie(_cfg(cookie=""))
    assert r["status"] == "no_cookie"
    assert r["valid"] is False


def test_check_cookie_ok():
    c = _cfg()
    fake = HttpResponse(200, '<span id="productTitle">Salt and Pepper Grinder Set</span>', url="u")
    fake.status_class = "ok"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        r = check_cookie(c)
    assert r["status"] == "ok"
    assert r["valid"] is True


def test_check_cookie_bot_blocked():
    c = _cfg()
    fake = HttpResponse(200, "captcha, robot check page", url="u")
    fake.status_class = "bot_blocked"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        r = check_cookie(c)
    assert r["status"] == "bot_blocked"
    assert r["valid"] is False


def test_check_cookie_uses_retries():
    """检测应使用配置的重试次数(默认 2, 2026-09-16 内化; 原默认 3), 而非单次请求, 避免偶发拦截误阻断。"""
    c = _cfg()
    fake = HttpResponse(200, '<span id="productTitle">Salt and Pepper Grinder Set</span>', url="u")
    fake.status_class = "ok"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        check_cookie(c)
        _, kwargs = F.return_value.get.call_args
        assert kwargs.get("retries", 1) >= 2


def test_check_cookie_respects_configured_retries():
    """配置里显式写了 REQUEST_RETRY 时以配置为准(内化不改变"可覆盖"这件事)。"""
    c = _cfg()
    c.data["REQUEST_RETRY"] = 5
    fake = HttpResponse(200, '<span id="productTitle">Salt and Pepper Grinder Set</span>', url="u")
    fake.status_class = "ok"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        check_cookie(c)
        _, kwargs = F.return_value.get.call_args
        assert kwargs.get("retries") == 5


def test_http_get_retries_on_bot_blocked():
    """HttpFetcher.get: bot_blocked 应重试, 直到 ok 或次数耗尽(风控概率性)。"""
    from core.http_client import HttpFetcher

    c = _cfg()
    fetcher = HttpFetcher(c, cookie="k=v")
    blocked = mock.Mock(status_code=200, text="captcha", url="u")
    ok = mock.Mock(status_code=200, text="real page", url="u")
    with mock.patch.object(fetcher, "_throttle", return_value=None), \
         mock.patch("core.http_client.cffi_requests.Session") as HC:
        HC.return_value.__enter__.return_value.get.side_effect = [blocked, ok]
        resp = fetcher.get("https://amazon.com/dp/X", retries=3)
    assert resp.status_class == "ok"
    assert resp.ok is True


def test_check_cookie_signin():
    c = _cfg()
    fake = HttpResponse(200, "Please sign in /ap/signin?continue", url="u")
    fake.status_class = "ok"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        r = check_cookie(c)
    assert r["status"] == "expired"
    assert r["valid"] is False


def test_check_cookie_network_error():
    c = _cfg()
    fake = HttpResponse(0, "SSL error", url="u")
    fake.status_class = "http_0"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        r = check_cookie(c)
    assert r["status"] == "network"
    assert r["valid"] is False


# ---- 编排器集成 ----
def _runner(ws_tmp):
    from core.orchestrator import PipelineRunner
    return PipelineRunner(Config({"CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": True}))


def test_mark_current_interrupted(ws_tmp):
    """Ctrl+C 中断: 进行中(RUNNING)步骤被标记 INTERRUPTED, 结束=中断时间。"""
    runner = _runner(ws_tmp)
    runner.state.start_step(3)
    runner._mark_current_interrupted()
    assert runner.state.status_of(3) == "INTERRUPTED"
    assert runner.state.steps[3]["end"]  # 中断时间已记
    # 无进行中步骤时不应报错
    runner2 = _runner(ws_tmp)
    runner2._mark_current_interrupted()


def test_report_interruptions_logs_stale_running(ws_tmp):
    """启动报告: 残留 RUNNING(进程被强杀)被输出为中断(时间≈检测时刻), 不抛错。"""
    runner = _runner(ws_tmp)
    runner.state.start_step(7)  # 残留 RUNNING 无 end(模拟进程被杀)
    runner._report_interruptions()  # 不应抛异常, 日志输出中断报告
    its = runner.state.interruptions()
    assert any(it["number"] == 7 and it["kind"] == "stale_running" for it in its)


def test_check_cookie_ok_without_exact_double_quoted_id():
    """回归 2026-09-13: 判定条件必须与 analyze_page 同口径。

    analyze_page 认宽口径(出现 producttitle 即 ok)。原判定又要求 `id="producttitle"`(窄),
    两者分叉时返回 {"valid": False, "status": "ok"} —— 调用方只看 status, 一次**失败**的
    预检被打印成"COOKIE 检查通过/有效"。
    """
    c = _cfg()
    fake = HttpResponse(200, "<span id='productTitle'>Salt and Pepper Grinder Set</span>", url="u")
    fake.status_class = "ok"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        r = check_cookie(c)
    assert r["status"] == "ok"
    assert r["valid"] is True


def test_check_cookie_unrecognized_page_not_reported_ok():
    """无法识别的探测页不得返回 status="ok"(否则编排器误报通过); 记为 unrecognized。"""
    c = _cfg()
    fake = HttpResponse(200, "<html><body>骨架页</body></html>", url="u")
    fake.status_class = "ok"
    with mock.patch("core.cookie_check.HttpFetcher") as F:
        F.return_value.get.return_value = fake
        r = check_cookie(c)
    assert r["status"] == "unrecognized"
    assert r["valid"] is False


def test_precheck_cookie_unrecognized_not_raise(ws_tmp):
    """unrecognized 与 network 同口径: 警告但不阻断整轮。"""
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc:
        cc.return_value = {"status": "unrecognized", "message": "探测页无法识别"}
        runner._precheck_cookie()  # 不阻断


def test_precheck_cookie_ok(ws_tmp):
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc:
        cc.return_value = {"status": "ok", "message": "COOKIE 有效"}
        runner._precheck_cookie()  # 不应抛异常


def test_precheck_cookie_blocked_is_allowed_through(ws_tmp):
    """**固定放行**(2026-10-08 用户定调: "COOKIE 检查需要放行, 让系统自己兜底处理"):

    检查失败只报警不阻断 —— 把"抓得到抓不到"交给 直连 → 无头 → 有头 的兜底链; **没有开关**。
    """
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc, \
         mock.patch("core.cookie_check.diagnose_cookie") as dc:
        cc.return_value = {"status": "bot_blocked", "message": "被拦截"}
        dc.return_value = {"verdict": "ip_or_client_marked", "message": "换 COOKIE 无用", "probes": []}
        runner._precheck_cookie()  # 不应抛异常
    log = Path(runner.paths.monitor_file("pipeline-progress.log")).read_text(encoding="utf-8")
    assert "COOKIE 检查失败" in log
    assert "放行" in log and "兜底" in log
    alerts = Path(runner.paths.monitor_file("monitor-alerts.txt")).read_text(encoding="utf-8")
    assert "COOKIE 检查失败" in alerts, "放行也要留 CRIT 告警(问题是真的, 该看见)"


def test_precheck_cookie_network_not_raise(ws_tmp):
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc:
        cc.return_value = {"status": "network", "message": "网络异常"}
        runner._precheck_cookie()  # 不阻断


def test_deprecated_switch_keys_are_ignored(ws_tmp):
    """两个历史开关键已废弃(行为写死在代码里): 配置里写了也**照样检查 + 照样放行**, 并打一行废弃提示。

    为什么留这条用例: 旧配置/旧备份里可能还带着 `COOKIE_CHECK: false`(曾经用来"关掉预检查"),
    必须保证它**不会**再改变行为(既不能关掉检查, 也不能让失败变成阻断)。
    """
    runner = _runner(ws_tmp)
    runner.config.set("COOKIE_CHECK", False)
    runner.config.set("COOKIE_CHECK_BLOCK", True)
    with mock.patch("core.cookie_check.check_cookie") as cc, \
         mock.patch("core.cookie_check.diagnose_cookie") as dc:
        cc.return_value = {"status": "bot_blocked", "message": "被拦截"}
        dc.return_value = {"verdict": "ip_or_client_marked", "message": "换 COOKIE 无用", "probes": []}
        runner._precheck_cookie()          # COOKIE_CHECK_BLOCK=true 也不再阻断
    cc.assert_called_once()                # COOKIE_CHECK=false 也不再跳过检查
    log = Path(runner.paths.monitor_file("pipeline-progress.log")).read_text(encoding="utf-8")
    assert "已废弃" in log and "被忽略" in log
    assert "放行" in log


# ---------------------------------------------------------------- 失败自动定性(2026-09-29)
#
# 背景: 步骤10 被 `bot_blocked` 预检查挡下时, 日志只有一句"COOKIE 失效或 IP 被标记",
# 用户为了分辨这两者得自己做"带/不带 COOKIE + 多站点对照", 来回折腾半小时。
# 现在失败时自动做这组对照, 并把结论写进日志/告警/异常文案。

_BOT = "captcha required, automated access from a computer network has been detected"
_OK = '<span id="productTitle">Salt and Pepper Grinder Set</span>'
_SIGNIN = "Sign in to continue /ap/signin"


def _fake_pages(monkeypatch, pages: dict, *, raise_on: tuple | None = None) -> list:
    """把 core.cookie_check.HttpFetcher 换成按 (站点, 是否带 COOKIE) 返回预设页的假实现。"""
    calls: list = []

    class _Fetcher:
        def __init__(self, config, cookie=None):
            self.market = config.market
            self.cookie = cookie or ""
            calls.append((self.market, bool(self.cookie)))

        def get(self, url, retries=1):
            key = (self.market, bool(self.cookie))
            if raise_on is not None and key == raise_on:
                raise RuntimeError("connection reset")
            if key not in pages:      # 注意: 不能用 dict.get(key, pages[...]) —— 默认参数会被求值 → KeyError
                key = ("*", bool(self.cookie))
            text, cls = pages[key]
            resp = HttpResponse(200 if cls == "ok" else 503, text, url)
            resp.status_class = cls
            return resp

    monkeypatch.setattr("core.cookie_check.HttpFetcher", _Fetcher)
    return calls


def test_diagnose_concludes_ip_marked_when_anonymous_gets_same_page(monkeypatch):
    """带 COOKIE 与不带 COOKIE 拿到**同一张拦截页**, 而其它站点正常 → 本机 IP/客户端形态被标记。

    这是 2026-09-29 `Dog Dryer` 的实测形状(3781 字节的验证码页), 结论是"换 COOKIE 无用"。
    """
    from core.cookie_check import diagnose_cookie

    calls = _fake_pages(monkeypatch, {
        ("amazon.com", True): (_BOT, "bot_blocked"),
        ("amazon.com", False): (_BOT, "bot_blocked"),
        ("amazon.co.uk", False): (_OK, "ok"),
        ("amazon.de", False): (_OK, "ok"),
    })
    diag = diagnose_cookie(_cfg())
    assert diag["verdict"] == "ip_or_client_marked"
    assert "换 COOKIE 无用" in diag["message"]
    assert "浏览器兜底" in diag["message"] or "换网络" in diag["message"]
    assert [p["site"] for p in diag["probes"]] == ["amazon.com", "amazon.com", "amazon.co.uk", "amazon.de"]
    assert diag["probes"][0]["with_cookie"] is True and diag["probes"][1]["with_cookie"] is False
    assert len(calls) == 4, f"对照探测应只打 4 个请求: {calls}"


def test_diagnose_points_at_cookie_when_anonymous_is_fine(monkeypatch):
    """带 COOKIE 被拦但**匿名正常** → 指向该站 COOKIE 本身(而不是 IP)。"""
    from core.cookie_check import diagnose_cookie

    _fake_pages(monkeypatch, {
        ("amazon.com", True): (_BOT, "bot_blocked"),
        ("amazon.com", False): (_OK, "ok"),
        ("amazon.co.uk", False): (_OK, "ok"),
        ("amazon.de", False): (_OK, "ok"),
    })
    diag = diagnose_cookie(_cfg())
    assert diag["verdict"] == "cookie_suspect"
    assert "COOKIE 本身" in diag["message"]


def test_diagnose_reports_all_sites_blocked(monkeypatch):
    """对照站点**也全被拦** → 本机 IP 被大范围标记(比单站被标记更严重)。"""
    from core.cookie_check import diagnose_cookie

    _fake_pages(monkeypatch, {
        ("amazon.com", True): (_BOT, "bot_blocked"),
        ("amazon.com", False): (_BOT, "bot_blocked"),
        ("amazon.co.uk", False): (_BOT, "bot_blocked"),
        ("amazon.de", False): (_BOT, "bot_blocked"),
    })
    diag = diagnose_cookie(_cfg())
    assert diag["verdict"] == "all_sites_blocked"
    assert "大范围" in diag["message"]


def test_diagnose_reports_expired_on_signin(monkeypatch):
    from core.cookie_check import diagnose_cookie

    _fake_pages(monkeypatch, {
        ("amazon.com", True): (_SIGNIN, "ok"),
        ("amazon.com", False): (_SIGNIN, "ok"),
        ("amazon.co.uk", False): (_OK, "ok"),
        ("amazon.de", False): (_OK, "ok"),
    })
    assert diagnose_cookie(_cfg())["verdict"] == "cookie_expired"


def test_diagnose_reports_network_when_probe_raises(monkeypatch):
    from core.cookie_check import diagnose_cookie

    _fake_pages(monkeypatch, {("*", True): (_OK, "ok"), ("*", False): (_OK, "ok")},
                raise_on=("amazon.com", True))
    assert diagnose_cookie(_cfg())["verdict"] == "network"


def test_precheck_failure_includes_diagnosis_in_message_and_log(ws_tmp):
    """预检查失败时: 结论必须写进**日志**(看板 ③ 与 ④ 都能看到)且带逐条探测明细。"""
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc, \
         mock.patch("core.cookie_check.diagnose_cookie") as dc:
        cc.return_value = {"status": "bot_blocked", "message": "被拦截"}
        dc.return_value = {"verdict": "ip_or_client_marked",
                           "message": "匿名同样被拦 + 其它站点正常 → 换 COOKIE 无用",
                           "probes": [{"site": "amazon.com", "with_cookie": True,
                                       "status": "bot_blocked", "kind": "bot", "len": 3781},
                                      {"site": "amazon.com", "with_cookie": False,
                                       "status": "bot_blocked", "kind": "bot", "len": 3781}]}
        runner._precheck_cookie()          # 默认放行: 记录后继续
    log_path = runner.paths.monitor_file("pipeline-progress.log")
    log = Path(log_path).read_text(encoding="utf-8") if Path(log_path).exists() else ""
    assert "定性探测: amazon.com" in log and "ip_or_client_marked" in log
    assert "换 COOKIE 无用" in log
    alerts = Path(runner.paths.monitor_file("monitor-alerts.txt")).read_text(encoding="utf-8")
    assert "定性结论" in alerts, "告警文案里也要带结论(企业微信同源)"


def test_precheck_success_never_runs_diagnosis(ws_tmp):
    """检查通过时**一个多余请求都不打**(定性只在失败时跑)。"""
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc, \
         mock.patch("core.cookie_check.diagnose_cookie") as dc:
        cc.return_value = {"status": "ok", "message": "COOKIE 有效"}
        runner._precheck_cookie()
    dc.assert_not_called()


def test_precheck_diagnosis_failure_does_not_break_the_run(ws_tmp):
    """定性自身抛异常时: 仍要如实记录原来的 CRIT 结论, 且**继续放行**(不能因为定性坏了就阻断)。"""
    runner = _runner(ws_tmp)
    with mock.patch("core.cookie_check.check_cookie") as cc, \
         mock.patch("core.cookie_check.diagnose_cookie", side_effect=RuntimeError("diag boom")):
        cc.return_value = {"status": "bot_blocked", "message": "被拦截"}
        runner._precheck_cookie()      # 不应抛异常
    log = Path(runner.paths.monitor_file("pipeline-progress.log")).read_text(encoding="utf-8")
    assert "COOKIE 检查失败" in log and "定性不可用" in log and "放行" in log
    alerts = Path(runner.paths.monitor_file("monitor-alerts.txt")).read_text(encoding="utf-8")
    assert "COOKIE 检查失败" in alerts
