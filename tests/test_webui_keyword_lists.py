# 单元测试: 看板「关键词清单」面板(核心大词 / 长尾词 三态, 2026-09-29)
# ---------------------------------------------------------------------------
# 面板用"人工清单文件 + 删除即回自动"那套(源自已下线的步骤10 注入词面板)管理核心大词/
# 长尾词, 并加# 一个"人工停止"开关。要点:
#   · 自动态与旧版完全一致; 人工态按最终词数判门槛; 停止态步骤9 直接不抓取;
#   · 面板的自动核心词复用 keyword_candidates() 的 auto_qualified —— 保证#     "面板 / 步骤9 搜索 / 步骤10 判定参考词"三处永远是同一批词(这是本面板存在的意义);
#     2026-10-06 起步骤10 直接取本面板「核心大词」列的生效词作注入词(长尾词不参与), 注入词面板已下线。
import importlib.util
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Kw Panel Cat-US-202609"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_kw",
                                                  ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp):
    s = _load_server()
    s.PROJECT_ROOT = Path(ws_tmp)
    return s


def _mk(ws_tmp, cat: str = CAT, gate: int = 2) -> Path:
    """类目目录 + 配置。DATA_ROOT 必须绝对路径(相对时会被再拼一次 PROJECT_ROOT)。"""
    root = Path(ws_tmp)
    d = root / "data" / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{cat}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n'
        f'SITE: "美国"\nMARKET: "amazon.com"\nGATE_MIN_QUALIFIED: {gate}\n',
        encoding="utf-8")
    (d / f"{cat}-keywords.txt").write_text(
        "[核心大词]\na\nb\nc\n[长尾词]\ntail one\ntail two\n", encoding="utf-8")
    (d / f"{cat}-screen-results.json").write_text(
        json.dumps([{"keyword": "a", "verdict": "达标"},
                    {"keyword": "b", "verdict": "达标"},
                    {"keyword": "c", "verdict": "不达标"}], ensure_ascii=False),
        encoding="utf-8")
    return d


def _droot(ws_tmp) -> str:
    return str(Path(ws_tmp) / "data")


# ---------------------------------------------------------------- 面板数据
def test_panel_defaults_to_auto(server, ws_tmp):
    d = _mk(ws_tmp)
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["ok"] is True
    assert p["mode"] == "auto"
    assert p["stopped"] is False
    assert p["gate"] == 2
    assert p["core"]["auto"] == ["a", "b"]           # = FR-07 达标词(与注入词面板同源)
    assert p["core"]["effective"] == ["a", "b"]
    assert p["core"]["gate_ok"] is True
    assert p["longtail"]["auto"] == ["tail one", "tail two"]
    assert p["files"]["core_manual_file"] == f"filter-{CAT}-core-words.txt"
    assert p["files"]["stop_file"] == f"filter-{CAT}-keyword-stop.txt"
    assert "核心" in p["search_plan"]["text"]


def test_panel_manual_mode_and_gate_by_word_count(server, ws_tmp):
    """情况一: 在达标词基础上删掉不想要的 + 加别的 —— 门槛按最终词数判。"""
    d = _mk(ws_tmp)
    (d / f"filter-{CAT}-core-words.txt").write_text("b\nmanual new\n", encoding="utf-8")
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["mode"] == "manual"
    assert p["core"]["manual"] == ["b", "manual new"]
    assert p["core"]["effective"] == ["b", "manual new"]
    assert p["core"]["qualified"] == 1          # b 达标
    assert p["core"]["no_verdict"] == 1         # manual new 无判定
    assert p["core"]["gate_ok"] is True         # 2 个 >= 门槛 2
    assert "人工清单生效" in p["core"]["gate_text"]


def test_panel_manual_below_gate_is_not_ok(server, ws_tmp):
    d = _mk(ws_tmp)
    (d / f"filter-{CAT}-core-words.txt").write_text("b\n", encoding="utf-8")
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["mode"] == "manual"
    assert p["core"]["gate_ok"] is False
    assert "❌停止" in p["core"]["gate_text"]


def test_panel_stop_flag(server, ws_tmp):
    d = _mk(ws_tmp)
    (d / f"filter-{CAT}-keyword-stop.txt").write_text("2026-09-29 10:00:00\n", encoding="utf-8")
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["mode"] == "stop"
    assert p["stopped"] is True
    assert p["core"]["gate_ok"] is False
    assert "人工停止" in p["core"]["gate_text"]


def test_panel_gate_comes_from_config(server, ws_tmp):
    _mk(ws_tmp, gate=3)
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["gate"] == 3
    assert p["core"]["gate_ok"] is False         # 只有 2 个达标词 < 3


def test_panel_longtail_dimensions(server, ws_tmp):
    d = _mk(ws_tmp)
    (d / f"{CAT}-keywords-entities.txt").write_text(
        "[场景.实体]\n1\thome\n[场景.长尾词]\ndog dryer for home\n"
        "[人群.长尾词]\ndog dryer for pro\n", encoding="utf-8")
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["longtail"]["dimensions"] == {"场景": ["dog dryer for home"],
                                           "人群": ["dog dryer for pro"]}


def test_panel_displays_keyword_metrics_and_both_longtail_sources(server, ws_tmp):
    d = _mk(ws_tmp)
    (d / f"{CAT}-keywords-candidates.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (d / f"{CAT}-amazon-keyword-data.json").write_text(json.dumps([
        {"keyword": "a", "monthly_search_volume": 15000, "cpc": 1.25},
        {"keyword": "b", "monthly_search_volume": None, "cpc": None},
    ]), encoding="utf-8")
    (d / f"{CAT}-kwgen-candidates.csv").write_text(
        "keyword,dimension,is_longtail,relation\nverified tail,人群,True,same_product\n"
        "unrelated tail,人群,True,adjacent\n", encoding="utf-8")
    (d / f"{CAT}-keywords-entities.txt").write_text(
        "[场景.实体]\n1\toutdoor\t商品数=2\n2\ttravel\t商品数=1\n", encoding="utf-8")
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    metrics = {r["word"]: r for r in p["candidates"]}
    assert (metrics["a"]["volume"], metrics["a"]["cpc"]) == (15000, 1.25)
    assert metrics["b"]["volume"] is None and metrics["b"]["cpc"] is None
    assert p["longtail"]["verified_pool"] == {"人群": ["verified tail"]}
    assert p["longtail"]["generated_pool"]["场景"] == ["a for outdoor", "a for travel"]


def test_panel_empty_cat_is_rejected(server, ws_tmp):
    assert server.keyword_lists_panel("", _droot(ws_tmp))["ok"] is False


def test_panel_used_head_after_first_run(server, ws_tmp):
    from core.keyword_lists import resolve, write_used
    d = _mk(ws_tmp)
    plan = resolve(d, CAT, auto_core=["a", "b"], auto_longtail=["t"])
    write_used(d, CAT, plan, site="美国")
    p = server.keyword_lists_panel(CAT, _droot(ws_tmp))
    assert p["used_head"] and "本轮实际生效关键词" in p["used_head"][0]


# ---------------------------------------------------------------- 保存接口
def test_save_lists_roundtrip(server, ws_tmp):
    d = _mk(ws_tmp)
    out = server.save_keyword_lists(CAT, _droot(ws_tmp), "core", "manual", ["B", "a", "a"])
    assert out["ok"] and out["saved"] == ["b", "a"]
    # 2026-10-06 起核心清单文件带 `[核心大词]` 分区头(以便与 `[仅注入]` 共存)
    assert (d / f"filter-{CAT}-core-words.txt").read_text(encoding="utf-8") == "[核心大词]\nb\na\n"
    out2 = server.save_keyword_lists(CAT, _droot(ws_tmp), "core", "auto")
    assert out2["ok"] and out2["mode"] == "auto"
    assert not (d / f"filter-{CAT}-core-words.txt").exists()


def test_save_inject_only_keeps_core_and_vice_versa(server, ws_tmp):
    """`[仅注入]` 与 `[核心大词]` 同文件分区保存: 互不覆盖; 都空才删文件。"""
    d = _mk(ws_tmp)
    p = d / f"filter-{CAT}-core-words.txt"
    a = server.save_keyword_lists(CAT, _droot(ws_tmp), "core", "manual", ["c1"])
    assert a["ok"] and a["saved"] == ["c1"]
    b = server.save_keyword_lists(CAT, _droot(ws_tmp), "inject", "manual", ["x1", "x2"])
    assert b["ok"] and b["saved"] == ["x1", "x2"]
    assert p.read_text(encoding="utf-8") == "[核心大词]\nc1\n[仅注入]\nx1\nx2\n"
    # 切回自动只清核心, 仅注入词保留
    server.save_keyword_lists(CAT, _droot(ws_tmp), "core", "auto")
    assert p.read_text(encoding="utf-8") == "[核心大词]\n[仅注入]\nx1\nx2\n"
    # 再清仅注入 -> 两分区都空 -> 删文件
    server.save_keyword_lists(CAT, _droot(ws_tmp), "inject", "auto")
    assert not p.exists()


def test_save_lists_rejects_over_limit(server, ws_tmp):
    _mk(ws_tmp)
    out = server.save_keyword_lists(CAT, _droot(ws_tmp), "longtail", "manual",
                                    [f"w{i}" for i in range(51)])
    assert out["ok"] is False and "最多 50 个" in out["error"]


def test_save_stop_toggle(server, ws_tmp):
    d = _mk(ws_tmp)
    assert server.save_keyword_stop(CAT, _droot(ws_tmp), True)["stopped"] is True
    assert (d / f"filter-{CAT}-keyword-stop.txt").exists()
    assert server.save_keyword_stop(CAT, _droot(ws_tmp), False)["stopped"] is False
    assert not (d / f"filter-{CAT}-keyword-stop.txt").exists()


# ---------------------------------------------------------------- 真起 HTTP
def test_http_keyword_lists_endpoints(server, ws_tmp):
    """端到端: GET 面板 + POST 保存清单 + POST 停止标记(证明路由与 JSON 序列化)。"""
    import threading
    from http.server import ThreadingHTTPServer

    d = _mk(ws_tmp)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    cfg = urllib.parse.quote(f"data/{CAT}/config.yaml")
    try:
        with urllib.request.urlopen(base + "/api/keyword-lists?file=" + cfg, timeout=10) as f:
            got = json.loads(f.read().decode("utf-8"))
        assert got["ok"] is True and got["mode"] == "auto"
        assert got["core"]["auto"] == ["a", "b"]

        def _post(path: str, body: dict) -> dict:
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))

        saved = _post("/api/keyword-lists/save",
                      {"config_file": f"data/{CAT}/config.yaml", "list": "core",
                       "mode": "manual", "words": ["a", "zzz"]})
        assert saved["ok"] is True and saved["saved"] == ["a", "zzz"]
        assert (d / f"filter-{CAT}-core-words.txt").exists()

        stopped = _post("/api/keyword-lists/stop",
                        {"config_file": f"data/{CAT}/config.yaml", "stopped": True})
        assert stopped["ok"] is True

        with urllib.request.urlopen(base + "/api/keyword-lists?file=" + cfg, timeout=10) as f:
            got2 = json.loads(f.read().decode("utf-8"))
        assert got2["mode"] == "stop" and got2["stopped"] is True
        assert got2["core"]["manual"] == ["a", "zzz"]      # 停止态仍能看到清单内容
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 前端源码钉住
def test_keyword_confirmation_http_captures_baseline_and_starts(server, ws_tmp, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from core.state import RunState
    from core.paths import STEP_DIR_NAMES
    from steps.ids import MONITOR_STEP

    root = _mk(ws_tmp)
    monitor = root / STEP_DIR_NAMES[MONITOR_STEP]
    monitor.mkdir(parents=True, exist_ok=True)
    state = RunState(str(monitor / "step-status.txt"), str(monitor / "step-duration.csv"))
    state.start_step(8)
    state.mark_done(8)
    calls = []
    monkeypatch.setattr(server.Handler, "_api_run", lambda self, body: calls.append(body.copy()) or {"ok": True})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    cfg = f"data/{CAT}/config.yaml"
    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    try:
        saved = post("/api/keyword-lists/save", {"config_file": cfg, "list": "core",
                       "mode": "manual", "words": ["a", "b", "slow feeder"]})
        assert saved["ok"]
        with urllib.request.urlopen(base + "/api/keyword-lists?file=" + urllib.parse.quote(cfg), timeout=10) as f:
            panel = json.loads(f.read())
        assert panel["confirmation"]["status"] == "pending"
        assert panel["confirmation"]["baseline"]["core"] == ["a", "b"]
        reply = post("/api/keyword-lists/confirm", {"config_file": cfg,
                     "revision": panel["confirmation_revision"]})
        assert reply["ok"] and calls[0]["mode"] == "confirm_keywords"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_index_html_has_keyword_panel():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert 'id="secKwLists"' in html
    assert 'id="btnSaveKwMode"' in html and 'id="btnReloadKw"' in html
    assert 'id="kwGate"' in html and 'id="btnSaveKwGate"' in html
    assert 'id="kwCoreChips"' in html and 'id="kwTailChips"' in html
    assert 'id="kwCandChips"' in html and 'id="kwUsedHead"' in html
    assert 'id="kwSearchPlan"' in html
    for v in ('value="auto"', 'value="manual"', 'value="stop"'):
        assert f'name="kwMode" {v}' in html or f'name="kwMode"{v}' in html or v in html


def test_index_html_keyword_panel_is_wired():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert '$("btnSaveKwMode").onclick = saveKwMode' in html
    assert '$("btnSaveKwGate").onclick = saveKwGate' in html
    # 2026-10-12 **删除**「以自动词为基础」按钮（自动态已预填 → 空操作；人工态下取名不副实）。
    #   但"切人工清单时铺底"的**内部逻辑保留**（`seedKwFromEffective`）。
    assert "btnKwCoreFill" not in html and "btnKwTailFill" not in html, "旧按钮又回来了"
    assert "fillKwFromAuto" not in html, "旧函数名又回来了"
    assert "seedKwFromEffective(" in html, "铺底逻辑被误删（人工清单是整体替换，必须铺底）"
    assert "loadKeywordPanel(force)" in html and "loadKeywordPanel(true)" in html, "面板必须在类目切换/初始化时加载"
    assert "/api/keyword-lists?file=" in html
    assert "/api/keyword-lists/save" in html
    assert "/api/keyword-lists/stop" in html
    assert "GATE_MIN_QUALIFIED" in html, "门槛值必须能在面板里改"


def test_keyword_panel_is_registered_in_run_view_lists():
    """回归(2026-09-29 实测"面板在 HTML 里但看不到"): 运行台用**白名单**控制卡片显隐,
    新卡片必须同时登记进 RUN_CARDS(否则不随视图切换、会漏进类目中心)、reorderCards
    (卡片顺序)与 renumberCards(编号) —— 只往 HTML 里塞一个 section 是不够的。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    run_cards = html.split("const RUN_CARDS = [", 1)[1].split("];", 1)[0]
    assert "secKwLists" in run_cards, "secKwLists 必须登记进 RUN_CARDS(运行台卡片白名单)"
    assert 'kw = $("secKwLists")' in html, "reorderCards 要认这块卡片"
    # 顺序: 状态 → **关键词清单** → 属性集 → 标准化
    # (2026-10-06: 原「步骤10 注入词」卡片已删除, 步骤10 判定参考词与关键词清单同步)
    assert 'st.insertAdjacentElement("afterend", kw)' in html, "关键词清单要排到状态之后"
    assert 'a1.insertAdjacentElement("afterend", attr)' in html, "属性集要排到关键词清单之后"
    assert "secInject" not in html, "注入词卡片已下线(2026-10-06), 不应再出现在页面里"
    assert "/api/inject-words" not in html, "注入词接口已下线, 页面不应再请求"
    assert 'secKwLists: "03 · Keyword Lists"' in html, "renumberCards 要有编号"
    assert '<div class="overline">03 · Keyword Lists</div>' in html, "静态编号要与 renumber 结果一致"


def test_longtail_words_are_clickable():
    """回归(2026-09-29 用户报"长尾词怎么不可以选择"): 长尾词原先只能手打,
    必须有一个**可点的词池**(五维分组), 且点词走 kwPickWord —— 非人工态下先以当前
    生效词为基础, 否则"从空清单点一个词"会把其余自动词全丢掉。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert 'id="kwTailPool"' in html, "长尾词池容器"
    assert "function renderKwTailPool()" in html
    assert 'class="chip kw-pool"' in html, "池子里的词要可点"
    assert "kwPickWord(x.dataset.kind, x.dataset.w)" in html, "点池子里的词"
    assert 'kwPickWord("core", x.dataset.w)' in html, "核心候选词走同一条点词路径"
    assert "function kwPickWord(" in html
    assert "kwEdit.core = _coreManual.length" in html and "kwEdit.longtail = _tailManual.length" in html, "加载时保留预填的核心与长尾词"
    assert "renderKwTailPool();" in html, "renderKeywordPanel 必须渲染长尾词池"


def test_candidate_pool_aligned_with_longtail_pool():
    """回归(2026-09-30 用户要求"候选词池的布局和UI与长尾词池对齐"):
    候选词池原先是一个扁平 chip 列表、容器只有 max-height 没有边框, 且位于**下方整行**;
    长尾词池则是「圆角边框滚动框 + 分组标题」并且**在所属列的词条下方**。
    现要求两者同构 —— 同款容器样式、同款分组渲染、同一条点词路径、各自待在本列内。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    box = ('style="max-height:320px;overflow:auto;border:1px solid var(--line);'
           'border-radius:10px;padding:6px"')
    assert f'id="kwTailPool" {box}' in html, "长尾词池容器样式"
    assert f'id="kwCandChips" {box}' in html, "候选词池容器样式必须与长尾词池一致"
    assert 'id="kwCandCount"' in html, "候选词池要有与核心/长尾同款的计数位"
    assert "function renderKwCandPool()" in html, "候选词池要有独立渲染函数"
    assert "renderKwCandPool();" in html, "renderKeywordPanel 必须渲染候选词池"
    assert 'box.querySelectorAll(".kw-cand-select")' in html, "候选词池勾选绑定"
    assert 'class="kw-cand-select"' in html and 'kwPickWord("core", x.dataset.w)' in html
    assert "月搜索量</th><th>CPC" in html
    # 两列结构对称: 候选词池在左列(核心大词列)内, 长尾词池在右列(长尾词列)内,
    # 即两个池容器都必须排在各自列的闭合 </div> 之前、且在 flex 行结束之前。
    flex = html.split('id="kwCoreChips"', 1)[1].split('id="kwUsedHead"', 1)[0]
    i_cand = html.index('id="kwCandChips"')
    i_tail = html.index('id="kwTailPool"')
    i_row_end = html.index('id="kwTailPool"')
    row = html[html.index('<div style="display:flex;gap:18px;flex-wrap:wrap">'):]
    row = row[:row.index("<!-- ============ ⑥ 步骤16")]
    assert 'id="kwCandChips"' in row and 'id="kwTailPool"' in row, "两个池都要在同一个 flex 行内"
    assert row.index('id="kwCandChips"') < row.index('id="kwTailPool"'), "候选词池在左列、长尾词池在右列"
    # 不留旧的全宽候选词块(否则 id 重复)
    assert html.count('id="kwCandChips"') == 1, "kwCandChips 只能出现一次"
    assert html.count('id="kwCandCount"') == 1, "kwCandCount 只能出现一次"
    assert html.count('id="kwTailPool"') == 1


def test_inject_only_pool_is_selectable_from_core_words():
    """回归(2026-10-06 用户要求"仅注入改成自选，可以从核心词中选择"):
    仅注入词不能只靠手打, 必须有**可点的池子**, 且池子取「核心词候选里尚未注入的那些」
    (候选池数据 减去 生效核心词), 点词即加入/移出仅注入清单。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    # ① 容器与计数位
    assert 'id="kwInjectPool"' in html, "仅注入词池容器"
    assert 'id="kwInjectPoolCount"' in html, "仅注入词池计数位"
    assert html.count('id="kwInjectPool"') == 1, "kwInjectPool 只能出现一次"
    assert "function renderKwInjectPool()" in html
    assert "renderKwInjectPool();" in html, "renderKeywordPanel 必须渲染仅注入词池"
    # ② 池子来源 = 核心词候选 - 已注入的
    seg = html.split("function renderKwInjectPool()", 1)[1].split("\nfunction ", 1)[0]
    assert "(kwData.candidates || [])" in seg, "池子取自核心词候选池"
    assert "(kwData.core || {}).effective" in seg, "要排除已生效的核心词"
    assert "(kwEdit.core || [])" in seg, "也要排除正在编辑的人工核心清单"
    assert "filter(c => !injected.has(c.word))" in seg, "过滤逻辑: 只留下尚未注入的"
    # ③ 点词 = 加入/移出仅注入清单
    assert 'box.querySelectorAll(".kw-inj-pool")' in seg
    assert 'toggleKwWord("inject", x.dataset.w)' in seg, "点池子里的词 -> 切换仅注入"
    assert 'class="chip kw-inj-pool"' in seg
    # ④ 手填仅作为"候选之外的词"的补充入口
    assert 'id="kwInjectAdd"' in html
    assert "候选之外的词" in html, "手填框要说明它是补充入口(不是主路径)"
    # ⑤ 仅注入词控件**不受三态显隐影响**(自动态下也能加)
    assert '["kwInjectAdd"' not in html, "仅注入词控件不应被 setElDisplay 按 isManual 隐藏"
    assert '["kwTailAdd", "btnKwTailAdd", "btnKwTailClear"]' in html


def test_inject_only_manual_entry_is_wired_end_to_end():
    """手填路径必须完整可用(2026-10-06 用户明确"也需要手填"):
    input -> 按钮 -> addKwWords("inject") -> kwEdit.inject -> 保存 -> `/api/keyword-lists/save{list:"inject"}`。
    """
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    # ① 输入框 + 按钮 + 绑定
    assert 'id="kwInjectAdd"' in html and 'id="btnKwInjectAdd"' in html
    assert '$("btnKwInjectAdd").onclick = () => addKwWords("inject");' in html
    # ② addKwWords 认得 inject(元素 id 映射)
    assert '{ core: "kwCoreAdd", longtail: "kwTailAdd", inject: "kwInjectAdd" }' in html
    # ③ 保存: 两个分支都要落盘(mode 由是否有词决定)
    assert 'list: "inject"' in html
    assert 'mode: kwEdit.inject.length ? "manual" : "auto"' in html
    # ④ 手填进去的词要以 chip 形式可见可删(✕ 走 toggleKwWord)
    seg = html.split("function renderKwChips(kind)", 1)[1].split("\nfunction ", 1)[0]
    assert '"kwInjectChips"' in seg, "仅注入词要有 chips 容器"
    assert 'data-kind="${kind}"' in seg and 'toggleKwWord(x.dataset.kind, x.dataset.w)' in seg
    # 2026-10-12 更新：注入块改成「**合并后的实际注入词** + 来源标记(核心/手加)」，
    #   渲染搬到了 `renderInjectChips`，空态指引也随之下移 —— **指引本身没删，只是换了地方**。
    iseg = html.split("function renderInjectChips(box)", 1)[1].split("\nfunction ", 1)[0]
    assert "用下方「从核心词中选择」池点选, 或在补充框手填" in iseg, "空态提示要指到点选池与手填两条路"
