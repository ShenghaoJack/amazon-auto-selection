# 单元测试: 步骤1 浏览器兜底 + Cookie 相关误用防护(2026-09-15)
# ---------------------------------------------------------------------------
# 背景(实测): amazon.co.uk 对 HTTP 直连(curl_cffi)返回 200 + captcha 壳页(3805 字节),
# 且**匿名请求同样被拦**、把请求间隔从 2-4s 放到 12-18s 也无改善(拦截率恒为 4/5)——
# 说明该 IP 已被标记, 光靠"放慢+Cookie"解决不了。同机真实浏览器却能正常打开(步骤0 成功)。
# 因此步骤1 增加: 直连失败的 ASIN 自动转**真实浏览器**补抓(与步骤10 同思路)。
import json
import os

from core.config import Config
from core.io_utils import write_json
from core.paths import Paths
from steps.base import StepContext
from steps.step01_details import Step01Details


class _Resp:
    def __init__(self, status, ok):
        self.request_asin = "X"
        self.ok = ok
        self.status = status
        self.fetched_at = "2026-09-15 13:00:00"
        self.title = ""
        self.bullets = []


def _ctx(tmp_dir: str, **extra) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": tmp_dir, "DRY_RUN": True, "MARKET": "amazon.co.uk"}
    data.update(extra)
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _write_union(ctx: StepContext, asins: list[str]) -> None:
    write_json(ctx.resolve_artifact(f"{ctx.config.cat}-bestseller-newrelease.json"),
               {"bs": asins, "nr": [], "union": asins, "processed_urls": []})


def _read_records(ctx: StepContext) -> list[dict]:
    p = ctx.resolve_artifact(f"{ctx.config.cat}-dual-details.jsonl")
    return [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()]


# ---------------------------------------------------------------- 模式判定
def test_fallback_mode_parsing(ws_tmp):
    """STEP01_BROWSER_FALLBACK: auto(默认) / always / off 三种写法都要能认。"""
    s = Step01Details()
    assert s._browser_fallback_mode(_ctx(ws_tmp)) == "auto"                     # 未配置
    assert s._browser_fallback_mode(_ctx(ws_tmp, STEP01_BROWSER_FALLBACK="")) == "auto"
    for v in ("always", "all", "force", "true", "1", "yes"):
        assert s._browser_fallback_mode(_ctx(ws_tmp, STEP01_BROWSER_FALLBACK=v)) == "always"
    for v in ("off", "false", "0", "no", "none"):
        assert s._browser_fallback_mode(_ctx(ws_tmp, STEP01_BROWSER_FALLBACK=v)) == "off"


def test_fallback_mode_uses_global_switch(ws_tmp):
    """2026-09-16: 步骤01 也听全局兜底开关 `FALLBACK`(用户"太多参数了, 只需要个兜底的")。

    优先级: `STEP01_BROWSER_FALLBACK`(历史专用键, 还能表达 always) > `FALLBACK` > auto。
    """
    s = Step01Details()
    assert s._browser_fallback_mode(_ctx(ws_tmp, FALLBACK="auto")) == "auto"
    assert s._browser_fallback_mode(_ctx(ws_tmp, FALLBACK="off")) == "off"
    assert s._browser_fallback_mode(_ctx(ws_tmp, FALLBACK="always")) == "always"
    # 两个键都写时以步骤专用键为准(向后兼容, 不被全局开关悄悄改掉)
    assert s._browser_fallback_mode(
        _ctx(ws_tmp, FALLBACK="off", STEP01_BROWSER_FALLBACK="auto")) == "auto"
    assert s._browser_fallback_mode(
        _ctx(ws_tmp, FALLBACK="auto", STEP01_BROWSER_FALLBACK="off")) == "off"


def test_needs_browser_skips_terminal_404():
    """404 类不转浏览器(转过去也是 404); 风控/连接/解析类才转。"""
    s = Step01Details()
    for st in ("bot_blocked", "blocked_page", "http_0", "http_429", "http_503", "parse_failed"):
        assert s._needs_browser(st) is True, st
    for st in ("not_found", "skipped_page_not_found", "ok", ""):
        assert s._needs_browser(st) is False, st


# ---------------------------------------------------------------- auto: 只在失败时转
def test_auto_falls_back_to_browser_on_bot_blocked(ws_tmp, monkeypatch):
    """直连被风控(bot_blocked) → 该 ASIN 自动转浏览器补抓并把结果写进产物。"""
    ctx = _ctx(ws_tmp)
    asins = ["B062592129", "B0866BC2EE"]
    _write_union(ctx, asins)

    class _Http:
        def fetch_product_page(self, asin, market=None):
            return _Resp("bot_blocked", False)

    monkeypatch.setattr(ctx, "http", lambda cookie=None: _Http())
    called: list[str] = []

    def _fake_browser(ctx2, asin):
        called.append(asin)
        return {"request_asin": asin, "ok": True, "status": "ok",
                "fetched_at": "2026-09-15 13:00:00", "title": f"title-{asin}",
                "bullets": ["b1"], "source": "browser"}

    s = Step01Details()
    monkeypatch.setattr(s, "_browser_fetch_one", _fake_browser)
    s.validate_upstream(ctx)
    s.run(ctx)

    assert called == asins, "两个被拦的 ASIN 都应转浏览器"
    recs = {r["request_asin"]: r for r in _read_records(ctx)}
    assert recs["B062592129"]["ok"] is True
    assert recs["B062592129"]["source"] == "browser"
    assert recs["B062592129"]["title"] == "title-B062592129"


def test_auto_does_not_fall_back_when_direct_succeeds(ws_tmp, monkeypatch):
    """直连成功的 ASIN 不得再走浏览器(否则白白变慢)。"""
    ctx = _ctx(ws_tmp)
    _write_union(ctx, ["B0A00878D7"])

    class _Http:
        def fetch_product_page(self, asin, market=None):
            r = _Resp("ok", True)
            r.title = "direct title"
            return r

    monkeypatch.setattr(ctx, "http", lambda cookie=None: _Http())
    called: list[str] = []
    s = Step01Details()
    monkeypatch.setattr(s, "_browser_fetch_one",
                        lambda c, a: called.append(a) or {"request_asin": a, "ok": True,
                                                          "status": "ok"})
    s.validate_upstream(ctx)
    s.run(ctx)
    assert called == [], "直连成功时不应触发浏览器"
    assert _read_records(ctx)[0]["title"] == "direct title"


def test_off_mode_never_uses_browser(ws_tmp, monkeypatch):
    """off 模式: 即使被拦也只记失败(保留老行为, 便于对比)。"""
    ctx = _ctx(ws_tmp, STEP01_BROWSER_FALLBACK="off")
    _write_union(ctx, ["B0AB57FC96"])

    class _Http:
        def fetch_product_page(self, asin, market=None):
            return _Resp("bot_blocked", False)

    monkeypatch.setattr(ctx, "http", lambda cookie=None: _Http())
    called: list[str] = []
    s = Step01Details()
    monkeypatch.setattr(s, "_browser_fetch_one", lambda c, a: called.append(a))
    s.validate_upstream(ctx)
    s.run(ctx)
    assert called == []
    rec = _read_records(ctx)[0]
    assert rec["ok"] is False and rec["status"] == "bot_blocked"


def test_always_mode_skips_direct_http(ws_tmp, monkeypatch):
    """always 模式: 全部直接走浏览器(直连一次都不发) —— 适合已知 HTTP 被全量拦截时。"""
    ctx = _ctx(ws_tmp, STEP01_BROWSER_FALLBACK="always")
    _write_union(ctx, ["B09ADC0C86", "B0F87BD338"])
    http_calls: list[str] = []

    class _Http:
        def fetch_product_page(self, asin, market=None):
            http_calls.append(asin)
            return _Resp("ok", True)

    monkeypatch.setattr(ctx, "http", lambda cookie=None: _Http())
    s = Step01Details()
    monkeypatch.setattr(s, "_browser_fetch_one",
                        lambda c, a: {"request_asin": a, "ok": True, "status": "ok",
                                      "fetched_at": "x", "title": "t", "bullets": [],
                                      "source": "browser"})
    s.validate_upstream(ctx)
    s.run(ctx)
    assert http_calls == [], "always 模式不应发 HTTP 直连请求"
    assert all(r["source"] == "browser" for r in _read_records(ctx))


# ---------------------------------------------------------------- 真实浏览器(解析器)路径
def test_dry_run_browser_fetch_parses_product_page(ws_tmp):
    """dry-run 下 MockPage 也要能返回**商品页**(否则浏览器兜底这条新路径没有冒烟覆盖)。

    此前 MockPage.content() 一律返回榜单页 HTML → 浏览器兜底必然 parse_failed。
    """
    ctx = _ctx(ws_tmp)
    s = Step01Details()
    rec = s._browser_fetch_one(ctx, "B017CBD1C5")
    assert rec is not None, "浏览器会话应可用(dry-run 替身)"
    assert rec["ok"] is True, f"应解析出商品页, 实际 status={rec['status']}"
    assert rec["source"] == "browser"
    assert rec["title"], "标题不应为空"
    assert rec["bullets"], "五点不应为空"
