# 单元测试: 卖家精灵浏览器**全局锁**(多类目并行, 2026-09-15)
# ---------------------------------------------------------------------------
# 背景: 步骤 0/11/12/13 都要驱动卖家精灵浏览器, CDP 直连模式下**所有类目共用一个
# Chrome**(同一个调试端口), 导出配额也是共享的 —— 并行时必须串行。
# 这里守住: ①同一时刻只有一个持有者; ②拿不到会等待并在日志里写明"在等谁";
# ③超时给出可操作的报错; ④进程死亡由 OS 释放锁(不需要陈旧锁清理);
# ⑤文件系统不支持锁/锁文件不可写时**降级不阻断**; ⑥`SsBrowserLease` 保证先取锁再进
# 浏览器、退出一定放锁(异常也不漏)。
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import ss_lock  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_lock(tmp_path, monkeypatch):
    """锁文件与"使用者记录"都落到临时目录(不污染项目 data/.locks)。"""
    monkeypatch.setenv("PIPELINE_SS_LOCK", str(tmp_path / "ss.lock"))
    monkeypatch.setenv("PIPELINE_SS_LOCK_DIR", str(tmp_path))
    yield


def test_second_acquire_times_out_and_reports_holder():
    """同一时刻只能有一个持有者; 后来者超时并说明"被谁占用"。"""
    logs: list[str] = []
    first = ss_lock.acquire(wait_seconds=0, label="CatA·卖家精灵")
    try:
        assert first.holder()["label"] == "CatA·卖家精灵"
        with pytest.raises(ss_lock.SsBrowserLockTimeout) as ei:
            ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵", log=logs.append)
        msg = str(ei.value)
        assert "CatA·卖家精灵" in msg, "报错必须说明是谁在占用"
        assert "SS_BROWSER_LOCK_WAIT" in msg, "报错要给出可操作的调整项"
    finally:
        first.release()
    # 释放后可以再拿
    second = ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵")
    second.release()


def test_waiting_logs_progress_and_then_succeeds():
    """等待者会打日志(不静默卡住), 持有者释放后能拿到锁。"""
    logs: list[str] = []
    holder = ss_lock.acquire(wait_seconds=0, label="CatA·卖家精灵")

    def _release_later():
        time.sleep(0.6)
        holder.release()

    threading.Thread(target=_release_later, daemon=True).start()
    t0 = time.monotonic()
    got = ss_lock.acquire(wait_seconds=10, label="CatB·卖家精灵", log=logs.append, poll=0.05)
    try:
        assert time.monotonic() - t0 >= 0.5, "应当真的等了一会儿"
        assert any("等待卖家精灵浏览器" in m for m in logs), f"等待要写进日志: {logs}"
        assert got.holder()["label"] == "CatB·卖家精灵", "拿到的锁应记新的持有者"
    finally:
        got.release()


def test_os_releases_lock_when_holder_process_dies():
    """持有者进程死亡 → 操作系统自动释放(这里用关闭句柄模拟, 等价于进程退出)。"""
    first = ss_lock.acquire(wait_seconds=0, label="CatA·卖家精灵")
    assert first._fh is not None
    first._fh.close()             # 进程死亡时 OS 会这么做
    first._fh = None
    got = ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵")
    got.release()


def test_lock_file_body_is_readable_and_overwritten():
    """锁文件正文(pid/入口/时间)可读, 反复取锁不会残留旧行。"""
    a = ss_lock.acquire(wait_seconds=0, label="CatA·卖家精灵")
    a.release()
    b = ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵")
    try:
        info = b.holder()
        assert info["pid"] == os.getpid()
        assert info["label"] == "CatB·卖家精灵"
        assert info["started"], "应写入开始时间"
    finally:
        b.release()


def test_degrades_without_blocking_when_lock_unusable(tmp_path, monkeypatch):
    """锁文件建不出来(如父路径是个文件)时降级为"无锁但可用", 且明确告警。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_SS_LOCK", str(blocker / "nested" / "ss.lock"))
    logs: list[str] = []
    lock = ss_lock.acquire(wait_seconds=0, label="CatA", log=logs.append)
    try:
        assert lock.degraded is True
        assert any("锁不可用" in m for m in logs), f"降级必须告警: {logs}"
    finally:
        lock.release()


class _FakeBrowser:
    """记录 enter/exit 顺序的假浏览器。"""

    def __init__(self, events: list):
        self.events = events

    def __enter__(self):
        self.events.append("browser:enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append("browser:exit")


def test_lease_warns_when_browser_resource_changed_account(monkeypatch, tmp_path):
    """**账号自检**: 同一个浏览器资源上次是别类目/别的账号在用 → 明确告警。

    卖家精灵的登录检查只判"登没登录", 不判"登录的是哪个账号"; 一个 Chrome 只保持一份登录态 ——
    若端口里的登录态没换过来, 本类目会静默读到/用掉**上一个账号**的配额与数据
    (用户"给不同类目配不同账号"后必须能发现这种情况)。
    """
    monkeypatch.delenv("PIPELINE_SS_LOCK", raising=False)
    res = "cdp:127.0.0.1:9222"
    warnings: list[str] = []
    # 第一次: CatA / acctA 使用该资源(记录落到 owner 文件)
    with ss_lock.SsBrowserLease(_FakeBrowser([]), wait_seconds=0, label="CatA·卖家精灵",
                                resource=res, account="acctA@x.com", warn=warnings.append):
        pass
    assert ss_lock.read_owner(res)["account"] == "accta@x.com"
    assert warnings == []

    # 第二次: CatB 配的是另一个账号, 但用了同一个端口 → 必须告警且点名两个账号
    with ss_lock.SsBrowserLease(_FakeBrowser([]), wait_seconds=0, label="CatB·卖家精灵",
                                resource=res, account="acctB@x.com", warn=warnings.append):
        pass
    assert warnings and "accta@x.com" in warnings[0] and "acctb@x.com" in warnings[0], warnings
    assert "换一个不同的 SS_CDP_URL 端口" in warnings[0]
    # 同账号再来一次 → 不再告警(正常复用)
    warnings.clear()
    with ss_lock.SsBrowserLease(_FakeBrowser([]), wait_seconds=0, label="CatB·卖家精灵",
                                resource=res, account="acctB@x.com", warn=warnings.append):
        pass
    assert warnings == []


def test_lease_owner_note_skipped_without_account_or_resource(tmp_path):
    """没配账号/没资源标识时不写记录也不告警(工具脚本场景)。"""
    warnings: list[str] = []
    with ss_lock.SsBrowserLease(_FakeBrowser([]), wait_seconds=0, label="X",
                                resource="", account="a@x.com", warn=warnings.append):
        pass
    with ss_lock.SsBrowserLease(_FakeBrowser([]), wait_seconds=0, label="X",
                                resource="cdp:127.0.0.1:9299", account="",
                                warn=warnings.append):
        pass
    assert warnings == []


def test_lease_holds_lock_around_browser_and_releases_on_error():
    """`SsBrowserLease`: 先拿锁再进浏览器; 退出(含异常)一定放锁, 让别人能接手。"""
    events: list = []
    lease = ss_lock.SsBrowserLease(_FakeBrowser(events), wait_seconds=0, label="CatA")
    with lease:
        assert events == ["browser:enter"]
        with pytest.raises(ss_lock.SsBrowserLockTimeout):
            ss_lock.acquire(wait_seconds=0, label="CatB")
    assert events[-1] == "browser:exit"
    ss_lock.acquire(wait_seconds=0, label="CatB").release()      # 锁已释放

    # 浏览器进入就抛异常时, 锁也不能漏(否则整轮都拿不到)
    class _BoomBrowser:
        def __enter__(self):
            raise RuntimeError("CDP 连不上")

        def __exit__(self, *a):
            return None

    with pytest.raises(RuntimeError):
        with ss_lock.SsBrowserLease(_BoomBrowser(), wait_seconds=0, label="CatA"):
            pass
    ss_lock.acquire(wait_seconds=0, label="CatB").release()


def test_lock_path_is_per_resource_not_global(monkeypatch, tmp_path):
    """锁按**浏览器资源**分文件(2026-09-15 用户"给不同类目配不同账号"后调整):

    不同 CDP 端口/profile = 不同的锁 = 真并行; 同端口 = 同一把锁(串行)。
    """
    monkeypatch.delenv("PIPELINE_SS_LOCK", raising=False)
    p_9222 = ss_lock.lock_path(ss_lock.resource_key("http://127.0.0.1:9222"))
    p_9223 = ss_lock.lock_path(ss_lock.resource_key("http://127.0.0.1:9223"))
    p_prof = ss_lock.lock_path(ss_lock.resource_key("", str(tmp_path / "ss-profile-CatA")))
    assert p_9222 != p_9223 != p_prof, "不同资源必须落在不同锁文件上(否则又变成全局串行)"
    assert p_9222 == ss_lock.lock_path(ss_lock.resource_key("127.0.0.1:9222")), \
        "同一端口的不同写法(localhost/scheme/无端口)必须归一成同一个资源"
    assert p_9222.parent == tmp_path, "锁文件落在可覆盖的锁目录里(默认是项目根 data/.locks)"
    assert "9222" in p_9222.name, f"锁文件名要能看出资源, 实际 {p_9222.name}"
    # 默认目录(不设覆盖时)必须是项目根 data/.locks —— 与 CAT/DATA_ROOT 无关, 各类目共用一处
    monkeypatch.delenv("PIPELINE_SS_LOCK_DIR", raising=False)
    assert ss_lock.lock_path("cdp:127.0.0.1:9222").parent.name == ".locks"


def test_different_ports_do_not_block_each_other(monkeypatch):
    """两个不同端口(各自账号)的两个类目: 各拿各的锁, **互不等待**(真并行)。"""
    monkeypatch.delenv("PIPELINE_SS_LOCK", raising=False)   # 让锁按资源分文件(而非固定单文件)
    a = ss_lock.acquire(wait_seconds=0, label="CatA·卖家精灵", resource="cdp:127.0.0.1:9222")
    try:
        b = ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵", resource="cdp:127.0.0.1:9223")
        b.release()          # 不应抛超时
    finally:
        a.release()


def test_same_resource_still_blocks(monkeypatch):
    """同一个资源(同端口)两个类目: 仍然互斥(一份登录态/配额不能同时用)。"""
    monkeypatch.delenv("PIPELINE_SS_LOCK", raising=False)
    a = ss_lock.acquire(wait_seconds=0, label="CatA·卖家精灵", resource="cdp:127.0.0.1:9222")
    try:
        with pytest.raises(ss_lock.SsBrowserLockTimeout):
            ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵", resource="cdp:127.0.0.1:9222")
    finally:
        a.release()


def test_browser_conflict_rules():
    """冲突判定: 不同端口不冲突; 同端口同主账号只是排队; 同端口**主账号不同**是硬冲突。"""
    acc_a, acc_b = {"a@x.com"}, {"b@x.com"}
    assert ss_lock.browser_conflict("http://127.0.0.1:9222", "a@x.com", acc_a,
                                    "http://127.0.0.1:9223", "b@x.com", acc_b) == ""
    assert ss_lock.browser_conflict("http://127.0.0.1:9222", "a@x.com", acc_a,
                                    "127.0.0.1:9222", "a@x.com", acc_a) == "", \
        "同端口同主账号 = 排队(不是冲突)"
    why = ss_lock.browser_conflict("http://127.0.0.1:9222", "a@x.com", acc_a,
                                   "http://127.0.0.1:9222", "b@x.com", acc_b)
    assert why and "不同账号" in why and "9223" in why, f"硬冲突必须给出可操作的换端口建议: {why}"
    # 实测踩到的坑: 两类目共用同一份**备用账号**列表, 只看集合交集会把"主账号不同"误判成
    # "账号相同"而放过 → 必须按**主账号**判。
    shared_backup = {"a@x.com", "backup@x.com"}
    other_backup = {"b@x.com", "backup@x.com"}
    why2 = ss_lock.browser_conflict("http://127.0.0.1:9222", "a@x.com", shared_backup,
                                    "http://127.0.0.1:9222", "b@x.com", other_backup)
    assert why2, "共用备用账号但主账号不同 → 仍是硬冲突(否则 B 会用 A 的登录态导出)"
    # 非 CDP 模式: 按 profile 判资源
    assert ss_lock.browser_conflict("", "", {"a"}, "", "", {"a"}) == "", "同一 profile 同一账号 → 排队"
    assert ss_lock.browser_conflict("", "a@x.com", {"a@x.com"}, "", "b@x.com", {"b@x.com"}), \
        "同一 profile + 不同账号 → 冲突"


def test_primary_account_falls_back_to_first_listed():
    """主账号缺省取 SS_ACCOUNTS 第一项(冲突判定用它)。"""
    assert ss_lock.primary_account({"SS_ACCOUNT": "Main@X.com"}) == "main@x.com"
    assert ss_lock.primary_account({"SS_ACCOUNTS": [{"account": "First@X.com"}]}) == "first@x.com"
    assert ss_lock.primary_account({}) == ""


# ------------------------------------------- 接线检查: 步骤真用的浏览器工厂走了锁
def test_ctx_browser_sellersprite_goes_through_global_lock(tmp_path, monkeypatch):
    """`StepContext.browser("sellersprite")` 必须**真的**先抢全局锁(接线不能只在模块里躺着)。

    这是"多类目并行"最关键的一根线: 步骤 0/11/12/13 都通过这个工厂拿浏览器, 只要它
    返回的是 `SsBrowserLease`, 那些步骤就自动串行了(步骤代码一行不用改)。
    """
    import steps.base as base
    from core.config import Config
    from core.paths import Paths

    events: list = []

    class _FakeSSBrowser:
        def __init__(self, *a, **kw):
            events.append("init")

        def __enter__(self):
            events.append("browser:enter")
            return self

        def __exit__(self, *a):
            events.append("browser:exit")
            return None

    monkeypatch.setattr(base, "BrowserAutomation", _FakeSSBrowser)
    # SS_BROWSER_LOCK_WAIT=0 → 拿不到锁立刻报错(默认 1800s 会**耐心等待**, 那是生产行为;
    # 单测要的是"抢不到就不进浏览器"这条断言, 所以把等待预算设为 0)
    cfg = Config({"CAT": "CatA", "DATA_ROOT": str(tmp_path), "DRY_RUN": False,
                  "SS_PROFILE": str(tmp_path / "profiles" / "ss-profile"),
                  "SS_CDP_URL": "http://127.0.0.1:9222", "SITE": "美国",
                  "SS_BROWSER_LOCK_WAIT": 0})
    ctx = base.StepContext(cfg, Paths(cfg))

    # 别的类目正持有全局锁 → 这个类目的浏览器工厂必须拿不到(而不是直接连 CDP)
    holder = ss_lock.acquire(wait_seconds=0, label="CatB·卖家精灵")
    try:
        with pytest.raises(ss_lock.SsBrowserLockTimeout):
            with ctx.browser("sellersprite"):
                events.append("should-not-enter")
        assert "browser:enter" not in events, "抢不到锁就不该启动浏览器"
    finally:
        holder.release()

    # 锁释放后: 先取锁 → 再进浏览器 → 退出后锁也放掉
    with ctx.browser("sellersprite") as b:
        assert events[-1] == "browser:enter"
        assert isinstance(b, _FakeSSBrowser)
        with pytest.raises(ss_lock.SsBrowserLockTimeout):
            ss_lock.acquire(wait_seconds=0, label="CatB")
    assert events[-1] == "browser:exit"
    ss_lock.acquire(wait_seconds=0, label="CatB").release()      # 已释放


def test_launch_ss_chrome_uses_per_category_profile(tmp_path, monkeypatch):
    """自动拉起 Chrome 必须用**按类目隔离**的 profile(2026-09-15, 用户"不同类目不同账号")。

    若仍用共享的 `SS_PROFILE`, 第二个类目(另一个账号)启动自己的 Chrome 时会因为
    user-data-dir 已被占用而失败, 且一个 profile 只能保存一份登录态 → 两个账号互相顶掉。
    """
    import core.browser as browser_mod
    from core.config import Config

    captured: list[list[str]] = []
    monkeypatch.setattr(browser_mod.subprocess, "Popen", lambda cmd: captured.append(cmd))
    monkeypatch.setattr(browser_mod, "find_chrome_path", lambda: "chrome.exe")
    cfg = Config({"CAT": "CatA", "DATA_ROOT": str(tmp_path),
                  "PROFILE_DIR": str(tmp_path / "profiles"),
                  "SS_PROFILE": str(tmp_path / "profiles" / "ss-profile"),
                  "SS_CDP_URL": "http://127.0.0.1:9223"})
    assert browser_mod.launch_ss_chrome(cfg, 9223) is True
    cmd = captured[0]
    assert "--remote-debugging-port=9223" in cmd
    user_dir = [c for c in cmd if c.startswith("--user-data-dir=")][0]
    assert user_dir.endswith("ss-profile-CatA"), \
        f"应使用按类目隔离的 profile, 实际 {user_dir}"
