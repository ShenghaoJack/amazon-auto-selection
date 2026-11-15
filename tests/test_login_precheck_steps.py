# 单元测试: 登录态预检查按步骤精确触发(不需要登录态的步骤不做 COOKIE/亚马逊/卖家精灵检查)
#
# 背景(2026-09-29 实例): 单步运行步骤8(纯本地生成关键词报告)时, 旧逻辑 `0 < num < 14`
# 会先做 COOKIE + 亚马逊 + 卖家精灵三重登录预检查 —— 白拉起浏览器, 还可能因与步骤8
# 完全无关的 COOKIE 失效/卖家精灵掉登录把这一步直接拦死。
import pytest

from core.config import Config
from core.orchestrator import (AMAZON_LOGIN_STEPS, COOKIE_STEPS,
                               SELLERSPRITE_LOGIN_STEPS, PipelineRunner)
from steps.step00_bestsellers import Step00Bestsellers


def _runner(ws_tmp, dry_run: bool = False) -> PipelineRunner:
    return PipelineRunner(Config({"CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": dry_run}))


@pytest.fixture
def rec(monkeypatch):
    """记录三类登录检查各被触发几次(不真的开浏览器/发 HTTP)。"""
    calls = {"cookie": 0, "amazon": 0, "ss": 0}

    def _cookie(self):
        calls["cookie"] += 1

    monkeypatch.setattr(PipelineRunner, "_precheck_cookie", _cookie)
    monkeypatch.setattr(Step00Bestsellers, "ensure_amazon_login",
                        lambda self, ctx: calls.__setitem__("amazon", calls["amazon"] + 1))
    monkeypatch.setattr(Step00Bestsellers, "ensure_sellersprite_login",
                        lambda self, ctx: calls.__setitem__("ss", calls["ss"] + 1))
    return calls


# ---- 依赖表本身 ----

def test_dependency_table_matches_browser_usage():
    """依赖表必须与各步实际打开的浏览器一致(用 `ctx.browser(` 全仓核对)。"""
    assert COOKIE_STEPS == {1, 10}                      # 纯 HTTP 直连
    assert AMAZON_LOGIN_STEPS == {0, 1, 9, 10}          # amazon 浏览器
    assert SELLERSPRITE_LOGIN_STEPS == {0, 11, 12, 13}  # sellersprite 浏览器
    # 本地计算步骤一个都不许出现在表里
    assert AMAZON_LOGIN_STEPS.isdisjoint(range(14, 20))
    assert SELLERSPRITE_LOGIN_STEPS.isdisjoint({2, 3, 4, 5, 6, 7, 8})


# ---- 纯本地 / 纯接口步骤: 一次登录检查都不做 ----

@pytest.mark.parametrize("num", [2, 3, 4, 5, 6, 7, 8, 14, 15, 16, 17, 18, 19])
def test_local_and_api_steps_skip_all_login_checks(ws_tmp, rec, num):
    _runner(ws_tmp)._precheck_login([num])
    assert rec == {"cookie": 0, "amazon": 0, "ss": 0}


def test_local_step_not_blocked_by_broken_cookie(ws_tmp, monkeypatch):
    """核心回归: COOKIE 失效/卖家精灵掉登录不得阻断纯本地步骤。"""
    def _boom(self):
        raise RuntimeError("COOKIE 检查失败(bot_blocked): 被拦截")

    monkeypatch.setattr(PipelineRunner, "_precheck_cookie", _boom)
    monkeypatch.setattr(Step00Bestsellers, "ensure_amazon_login",
                        lambda self, ctx: (_ for _ in ()).throw(RuntimeError("亚马逊需登录")))
    monkeypatch.setattr(Step00Bestsellers, "ensure_sellersprite_login",
                        lambda self, ctx: (_ for _ in ()).throw(RuntimeError("卖家精灵需登录")))
    for num in (2, 3, 4, 5, 6, 7, 8, 14, 15, 16, 17, 18, 19, 20):   # 19=VOC(无浏览器登录), 20=备份/体检
        _runner(ws_tmp)._precheck_login([num])  # 不应抛异常


def test_empty_and_out_of_range_no_check(ws_tmp, rec):
    r = _runner(ws_tmp)
    r._precheck_login([])
    r._precheck_login([99])
    assert rec == {"cookie": 0, "amazon": 0, "ss": 0}


# ---- 抓取步骤: 只做自己需要的那部分 ----

@pytest.mark.parametrize("num", [9])
def test_amazon_browser_step_checks_amazon_only(ws_tmp, rec, num):
    """步骤 9(亚马逊搜索收集): 要亚马逊登录态, 但不用 COOKIE(浏览器不吃 COOKIE 字段), 不用卖家精灵。"""
    _runner(ws_tmp)._precheck_login([num])
    assert rec == {"cookie": 0, "amazon": 1, "ss": 0}


@pytest.mark.parametrize("num", [11, 12, 13])
def test_sellersprite_step_checks_sellersprite_only(ws_tmp, rec, num):
    """步骤 11-13(卖家精灵浏览器): 不再被无关的亚马逊 COOKIE 误阻断。"""
    _runner(ws_tmp)._precheck_login([num])
    assert rec == {"cookie": 0, "amazon": 0, "ss": 1}


@pytest.mark.parametrize("num", [1, 10])
def test_http_direct_steps_check_cookie_and_amazon_not_ss(ws_tmp, rec, num):
    _runner(ws_tmp)._precheck_login([num])
    assert rec == {"cookie": 1, "amazon": 1, "ss": 0}


def test_step0_checks_both_browsers_not_cookie(ws_tmp, rec):
    """步骤 0 内部既调 ensure_amazon_login 也调 ensure_sellersprite_login, 但不读 COOKIE。"""
    _runner(ws_tmp)._precheck_login([0])
    assert rec == {"cookie": 0, "amazon": 1, "ss": 1}


# ---- 多步(全流程/续跑)口径 ----

def test_resume_from_step5_checks_all_three(ws_tmp, rec):
    """从步骤 5 续跑: 后面还有 9/10(亚马逊+COOKIE)与 11-13(卖家精灵), 三样都查。"""
    _runner(ws_tmp)._precheck_login(range(5, 20))
    assert rec == {"cookie": 1, "amazon": 1, "ss": 1}


def test_resume_from_step11_skips_cookie_and_amazon(ws_tmp, rec):
    """从步骤 11 续跑: 只剩卖家精灵步骤, 失效的亚马逊 COOKIE 不该再拦路。"""
    _runner(ws_tmp)._precheck_login(range(11, 20))
    assert rec == {"cookie": 0, "amazon": 0, "ss": 1}


def test_mixed_set_checks_union_only(ws_tmp, rec):
    _runner(ws_tmp)._precheck_login([8, 10])
    assert rec == {"cookie": 1, "amazon": 1, "ss": 0}


def test_dry_run_never_checks(ws_tmp, rec):
    _runner(ws_tmp, dry_run=True)._precheck_login([0, 1, 11])
    assert rec == {"cookie": 0, "amazon": 0, "ss": 0}


# ---- 调用点: run_single 真的不再对本地步骤做登录预检查 ----

def _stub_run_single(monkeypatch, runner):
    monkeypatch.setattr(runner, "snapshot_dir", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_log_price_source", lambda: None)
    monkeypatch.setattr(runner, "_report_interruptions", lambda: None)
    monkeypatch.setattr(runner, "_backfill_synced_steps", lambda num: None)
    monkeypatch.setattr(runner, "_write_usage_summary", lambda: None)
    monkeypatch.setattr(runner, "_run_one", lambda step: 0)


def test_run_single_step8_does_not_touch_login(ws_tmp, monkeypatch, rec):
    runner = _runner(ws_tmp)
    _stub_run_single(monkeypatch, runner)
    monkeypatch.setattr(PipelineRunner, "_precheck_cookie",
                        lambda self: (_ for _ in ()).throw(RuntimeError("COOKIE 失效")))
    assert runner.run_single(8) == 0
    assert rec == {"cookie": 0, "amazon": 0, "ss": 0}


def test_run_single_step11_checks_sellersprite(ws_tmp, monkeypatch, rec):
    runner = _runner(ws_tmp)
    _stub_run_single(monkeypatch, runner)
    assert runner.run_single(11) == 0
    assert rec == {"cookie": 0, "amazon": 0, "ss": 1}


def test_run_single_step10_checks_cookie_and_amazon(ws_tmp, monkeypatch, rec):
    runner = _runner(ws_tmp)
    _stub_run_single(monkeypatch, runner)
    assert runner.run_single(10) == 0
    assert rec == {"cookie": 1, "amazon": 1, "ss": 0}
