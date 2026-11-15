# 单元测试: 步骤产物弹窗(2026-09-15, 用户需求)
# ---------------------------------------------------------------------------
# 需求: 去掉前端"产物下载"列表, 改为**点击②步骤卡片弹窗**, 弹窗里列出该步骤产生的文件。
# 服务端为此新增 `/api/step-files?file=…&step=N`, 数据来源:
#   ① 步骤类声明的 artifacts + extra_archive_files(与流水线归档同步同一份声明, 最权威);
#   ② 该步骤实时目录 `data/<CAT>/<STEP_DIR_NAMES[step]>/` 下的实际文件(下载件/日志/截图);
#   ③ `unclaimed`: 类目根带 <CAT> 前缀却没被任何步骤声明的文件(正常为空, 防空漏)。
# 顺带放宽下载白名单: 原实现要求 name 精确命中 ARTIFACT_TABLE, 于是弹窗里能列出却下不了
# 非关键交付件 —— 现改为"必须落在类目目录内"(真正的安全边界), 并保持防目录穿越。
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Demo Cat-US-202609"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_stepfiles", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp):
    s = _load_server()
    s.PROJECT_ROOT = Path(ws_tmp)
    return s


def _mk(ws_tmp, cat: str = CAT) -> Path:
    """建类目数据目录 + 配置(webui 按 DATA_ROOT/<CAT>/config.yaml 定位类目)。

    ⚠️ DATA_ROOT 写 `<root>/data`: `cat_root_dir(cat, DATA_ROOT)` = `<DATA_ROOT>/<CAT>`,
    与测试里直接调的 `step_files(CAT, "data", …)`(PROJECT_ROOT 已指向 ws_tmp)保持一致。
    """
    root = Path(ws_tmp)
    d = root / "data" / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{cat}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n', encoding="utf-8")
    return d


def _touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------- ① 声明产物
def test_step_files_lists_declared_artifacts(server, ws_tmp):
    """步骤声明的产物要能列出来(如步骤8 关键词报告), 并标 declared=True。"""
    d = _mk(ws_tmp)
    _touch(d / f"{CAT}-keyword-report.xlsx")
    out = server.step_files(CAT, "data", 8)
    names = [f["name"] for f in out["files"]]
    assert f"{CAT}-keyword-report.xlsx" in names
    row = [f for f in out["files"] if f["name"].endswith("keyword-report.xlsx")][0]
    assert row["declared"] is True and row["size"] > 0 and row["mtime"]
    assert out["title"], "应带步骤标题(弹窗标题用)"
    assert out["declared_count"] == 1 and out["extra_count"] == 0


def test_step_files_includes_realtime_dir_files(server, ws_tmp):
    """实时目录里的"过程文件"(下载件/日志/截图)也要列出, 且标 declared=False。"""
    d = _mk(ws_tmp)
    _touch(d / "17_备份与监控" / "health-report.json")            # 声明产物
    _touch(d / "17_备份与监控" / "pipeline-progress.log")         # 过程文件
    _touch(d / "17_备份与监控" / "screenshots" / "shot.png", "png")  # 递归子目录
    out = server.step_files(CAT, "data", 20)
    by = {f["name"]: f for f in out["files"]}
    assert "17_备份与监控/pipeline-progress.log" in by
    assert "17_备份与监控/screenshots/shot.png" in by, "子目录里的截图也要列(递归)"
    assert by["17_备份与监控/pipeline-progress.log"]["declared"] is False
    assert out["dir"] == "17_备份与监控"
    assert out["extra_count"] >= 2


def test_step_files_empty_for_unrun_step(server, ws_tmp):
    """没跑过的步骤: 返回空列表(弹窗显示"暂无产物"), 不报错。"""
    _mk(ws_tmp)
    out = server.step_files(CAT, "data", 12)
    assert out["files"] == [] and out["unclaimed"] == []


def test_step_files_reports_unclaimed_category_files(server, ws_tmp):
    """类目根带 <CAT> 前缀却没有任何步骤声明的文件 → 进 unclaimed(防空漏展示)。

    2026-09-15: 未归属项也要带**真实大小/时间** —— 原先只回文件名, 前端只能显示 "—",
    用户看到两行 "—" 来问"这是怎么回事"(实际是步骤09 的 search-verify 附档, 现已正常声明)。
    """
    d = _mk(ws_tmp)
    _touch(d / f"{CAT}-keyword-report.xlsx")      # 步骤8 声明
    mystery = _touch(d / f"{CAT}-mystery-output.csv", "abc")   # 谁都没声明
    _touch(d / "unrelated.txt")                   # 不带 CAT 前缀, 不算
    out = server.step_files(CAT, "data", 8)
    assert [u["name"] for u in out["unclaimed"]] == [f"{CAT}-mystery-output.csv"]
    row = out["unclaimed"][0]
    assert row["size"] == mystery.stat().st_size and row["size"] > 0, "未归属也要有大小"
    assert row["mtime"] and row["mtime"] != "—", "未归属也要有更新时间"
    assert row["declared"] is False


def test_step09_declares_its_verify_attachments(server, ws_tmp):
    """步骤09 的两份附档要**声明**(归到 STEP 09, 不再掉进"未归属产物")。

    实测(用户问起): `Chopping Boards-UK-…-search-verify.csv/.json` 是步骤09 产出的
    "断点续爬持久化 + 人工审计表"(见 step09 模块 docstring), 但没进任何声明 →
    看板只能把它们塞进"未归属产物"且显示不出大小/时间。
    """
    d = _mk(ws_tmp)
    _touch(d / f"{CAT}-search-verify.json", "{}")
    _touch(d / f"{CAT}-search-verify.csv", "a,b\n")
    out = server.step_files(CAT, "data", 9)
    names = {f["name"]: f for f in out["files"]}
    for n in (f"{CAT}-search-verify.json", f"{CAT}-search-verify.csv"):
        assert n in names, f"{n} 应归到 STEP 09"
        assert names[n]["declared"] is True and names[n]["size"] > 0
    assert out["unclaimed"] == [], "声明后不该再出现在未归属里"
    other = server.step_files(CAT, "data", 0)     # 其它步骤的视图里同样看不到
    assert other["unclaimed"] == []


def test_step_files_no_cat_returns_empty(server, ws_tmp):
    out = server.step_files("", "data", 0)
    assert out["files"] == [] and out["unclaimed"] == []


# --------------------------------------------------------------- ② HTTP 接口
def test_api_step_files_endpoint(server, ws_tmp):
    """接口可用 + 参数解析正确。

    2026-09-16 起**缺省 source=archive**(卡片读归档快照) → 这个只有实时文件、没有归档的夹具,
    缺省调用会得到空清单; 因此这里显式用 `source=live` 校验旧的"声明产物"口径,
    另外再断言"缺省就是归档模式"(默认值本身也要钉住)。
    """
    d = _mk(ws_tmp)
    _touch(d / f"{CAT}-keyword-report.xlsx")
    h = server.Handler.__new__(server.Handler)
    out = h._api_step_files({"file": [f"data/{CAT}/config.yaml"], "step": ["8"],
                             "source": ["live"]})
    assert out["ok"] is True and out["cat"] == CAT and out["step"] == 8
    assert out["source"] == "live"
    assert [f["name"] for f in out["files"]] == [f"{CAT}-keyword-report.xlsx"]

    default = h._api_step_files({"file": [f"data/{CAT}/config.yaml"], "step": ["8"]})
    assert default["source"] == "archive", "缺省必须读归档(用户 2026-09-16 定调)"
    assert default["files"] == [] and default["snapshot"] == "", "该夹具没有归档快照"


def test_api_step_files_rejects_bad_step(server, ws_tmp):
    _mk(ws_tmp)
    h = server.Handler.__new__(server.Handler)
    # ⚠ 越界样例必须**跟着 STEP_COUNT 走**：`STEP_COUNT` 于 2026-10-09 因
    #   「新增步骤20=VOC 评论分析」从 20 提到 21，写死的 `"20"` 就此**变成合法**
    #   → 不再抛错 → 测试假失败（这不是被测代码的 bug，是测试写死了常量）。
    over = str(server.STEP_COUNT)
    for bad in ("", "abc", "-1", over):
        with pytest.raises(ValueError):
            h._api_step_files({"file": [f"data/{CAT}/config.yaml"], "step": [bad]})


# --------------------------------------------------------------- ③ 下载白名单放宽
def test_download_allows_any_file_inside_category_dir(server, ws_tmp):
    """弹窗里能列出的文件就应该能下载(含截图/日志/中间产物)。"""
    d = _mk(ws_tmp)
    p = _touch(d / "17_备份与监控" / "screenshots" / "shot.png", "png")
    got = server._artifact_abs(CAT, "data", 17, "17_备份与监控/screenshots/shot.png")
    assert got is not None and got.resolve() == p.resolve()


def test_download_still_blocks_traversal_and_outside(server, ws_tmp):
    """安全边界不变: 只能下类目目录**内**的文件(防目录穿越)。"""
    d = _mk(ws_tmp)
    (Path(ws_tmp) / "secret.txt").write_text("secret", encoding="utf-8")
    _touch(d / f"{CAT}-keyword-report.xlsx")
    assert server._artifact_abs(CAT, "data", 8, "../secret.txt") is None
    assert server._artifact_abs(CAT, "data", 8, f"../../{(Path(ws_tmp) / 'secret.txt').name}") is None
    assert server._artifact_abs(CAT, "data", 8, "not-exists.xlsx") is None
    assert server._artifact_abs(CAT, "data", 99, f"{CAT}-keyword-report.xlsx") is None, "步骤号越界要拒"


# --------------------------------------------------------------- ④ 前端页面
def test_index_html_step_card_modal(server):
    """前端: 产物下载列表已移除, 改为点击步骤卡片弹窗。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    # 旧的产物下载区块/逻辑必须清干净
    for gone in ("fileBox", "filesCat", "btnRefreshFiles", "refreshFiles(", "产物下载</b>"):
        assert gone not in html, f"旧的产物下载实现没清干净: {gone}"
    # 新弹窗要素
    assert 'id="stepFilesOverlay"' in html and 'class="overlay"' in html, "弹窗容器缺失"
    assert "/api/step-files?file=" in html, "弹窗数据接口未接"
    assert "function openStepFiles" in html and "function loadStepFiles" in html
    assert 'el.onclick = () => openStepFiles(Number(el.dataset.step))' in html, "步骤卡片没绑定点击"
    assert 'el.dataset.step = s.number' in html
    for btn in ('id="sfRefresh"', 'id="sfClose"'):
        assert btn in html
    assert 'key === "Escape"' in html, "Esc 关闭"


# --------------------------------------------------------------- ⑤ 错误语义(2026-09-15 体检修复)
def test_get_bad_param_returns_400_not_500(server, ws_tmp):
    """**真起一个 HTTP 服务**打一次: 非法参数必须 400, 不能是 500。

    原 `do_GET` 没有 `except ValueError` 分支 → `/api/step-files?step=abc` 落到兜底返回
    **500 + "ValueError: …"**, 前端弹给用户看着像服务端崩了。
    """
    import json as _json
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    _mk(ws_tmp)                                   # 让 /api/step-files 有类目可解析
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # 越界样例跟着 STEP_COUNT 走(2026-10-09: STEP_COUNT 因新增步骤20 由 20 → 21,
        # 写死的 "20" 已变成合法步骤号 → 这里改为动态取值, 避免再次假失败)
        for step, expect in (("abc", 400), (str(server.STEP_COUNT), 400), ("-1", 400)):
            url = (f"{base}/api/step-files?file=" + urllib.parse.quote(f"data/{CAT}/config.yaml")
                   + f"&step={step}")
            try:
                with urllib.request.urlopen(url, timeout=10) as f:
                    code, body = f.status, f.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                code, body = e.code, e.read().decode("utf-8", "replace")
            assert code == expect, f"step={step} 应 {expect}, 实际 {code} {body[:80]}"
            assert _json.loads(body)["ok"] is False
        # 正常参数仍 200
        url = (f"{base}/api/step-files?file=" + urllib.parse.quote(f"data/{CAT}/config.yaml")
               + "&step=8")
        with urllib.request.urlopen(url, timeout=10) as f:
            assert f.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_user_facing_rejection_uses_dedicated_type_and_409(server, ws_tmp, monkeypatch):
    """用户可见的"拒绝"(同类目重复启动等)必须是 `UserFacingError` 并映射 409, 不是 500。

    原先裸 `RuntimeError` → 前端弹出 "RuntimeError: 类目「X」已有流水线在本服务运行中…",
    看着像服务端故障; 实际只是"请先中断再启动"。
    """
    _mk(ws_tmp)

    class _R:
        def active_for(self, cat):
            return True

        def snapshots(self):
            return []

    monkeypatch.setattr(server, "_lock_live_started", lambda cat, droot: (False, None, False))
    with pytest.raises(server.UserFacingError) as ei:
        server.run_start_guard(CAT, "data", runner=_R())
    assert "请先中断再启动" in str(ei.value)

    # 两个处理器都把它映射成 409(源码级钉住, 防回退成 500)
    src = (ROOT / "webui" / "server.py").read_text(encoding="utf-8")
    assert src.count("except UserFacingError as exc:") == 2, "do_GET/do_POST 都要有这一支"
    assert src.count('"error": str(exc)}, 409)') == 2, "两处都要返回 409"


# --------------------------------------- ⑥ 卡片改为"读归档快照"(2026-09-16 用户定调)
# 用户原话: "你可以改读取方式: 读取每个类目 archive 中每个步骤里的数据" → 卡片不再按
# "声明 + 实时目录扫描"拼, 而是直接列**该类目归档快照里该步骤目录**的内容;
# 归档里没有的文件(从未被任何步骤声明)只给一行计数提示, 不再有"未归属产物"分节。
def _mk_snap(ws_tmp, name: str) -> Path:
    snap = Path(ws_tmp) / "data" / "archive" / name
    snap.mkdir(parents=True, exist_ok=True)
    return snap


def test_archive_mode_lists_latest_snapshot_step_dir(server, ws_tmp):
    """归档模式: 取**最新**快照, 列 `NN_步骤名/` 下的文件, name 是相对**快照根**的路径。"""
    _mk(ws_tmp)
    old = _mk_snap(ws_tmp, f"{CAT}-20260915-100000")
    _touch(old / "10_父项变体" / "old.jsonl")                     # 旧快照里的文件
    new = _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    _touch(new / "10_父项变体" / f"{CAT}-results.jsonl", "{}")
    out = server.step_files(CAT, "data", 10, source="archive")
    assert out["source"] == "archive"
    assert out["snapshot"] == f"{CAT}-20260916-120000", "应取最新一份快照"
    assert out["snapshot_count"] == 2
    assert out["dir"] == "10_父项变体"
    assert [f["name"] for f in out["files"]] == [f"10_父项变体/{CAT}-results.jsonl"]
    assert out["files"][0]["archive"] is True and out["exists"] is True
    assert out["unclaimed"] == [], "归档模式不再有未归属分节"
    assert out["snapshot_created"] == "2026-09-16 12:00:00", "创建时间从目录名解析"


def test_archive_mode_explicit_snapshot(server, ws_tmp):
    """可用 `snapshot=` 指定看哪一份快照(缺省才是最新)。"""
    _mk(ws_tmp)
    old = _mk_snap(ws_tmp, f"{CAT}-20260915-100000")
    _touch(old / "00_榜单收集" / "old.json")
    _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    out = server.step_files(CAT, "data", 0, source="archive", snapshot=f"{CAT}-20260915-100000")
    assert out["snapshot"] == f"{CAT}-20260915-100000"
    assert [f["name"] for f in out["files"]] == ["00_榜单收集/old.json"]
    assert out["dir"] == "00_榜单收集", "目录名来自 STEP_DIR_NAMES"


def test_archive_mode_without_any_snapshot(server, ws_tmp):
    """该类目从没归档过 → 空清单 + 无快照信息(前端据此提示"还没有归档快照")。"""
    _mk(ws_tmp)
    _touch(Path(ws_tmp) / "data" / CAT / f"{CAT}-keyword-report.xlsx")
    out = server.step_files(CAT, "data", 8, source="archive")
    assert out["snapshot"] == "" and out["files"] == [] and out["exists"] is False
    assert out["snapshot_count"] == 0


def test_archive_mode_step_dir_missing(server, ws_tmp):
    """快照存在但该步骤还没归档 → 空清单(但 dir 名仍给出, 便于前端提示)。"""
    _mk(ws_tmp)
    _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    out = server.step_files(CAT, "data", 16, source="archive")
    assert out["snapshot"] and out["files"] == [] and out["exists"] is False
    # 2026-10-14: 目录名统一到实际落盘名(原断言写的是 paths 表里的死条目 `16_属性集提取与确认`)
    assert out["dir"] == "16_产品属性标注"


def test_archive_mode_has_no_unclaimed_at_all(server, ws_tmp):
    """归档模式**完全不报"未归属"** —— 既无分节也无计数。

    2026-09-16 用户追加要求: 卡片末尾那行"另有 N 个类目根文件未被任何步骤声明…"的提示不要了,
    卡片只呈现归档内容本身(此前只剩计数提示, 现在连计数一起撤掉)。
    """
    d = _mk(ws_tmp)
    _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    _touch(d / f"{CAT}-variant-texts.jsonl")            # 步骤10 未声明的附档(归档里没有)
    _touch(d / f"{CAT}-keyword-report.xlsx")            # 步骤8 已声明
    out = server.step_files(CAT, "data", 10, source="archive")
    assert out["unclaimed"] == [], "归档模式不该有未归属清单"
    assert "unclaimed_count" not in out, "连计数提示也撤掉了(用户 2026-09-16 要求)"


def test_pick_snapshot_validates_name_and_prefix_collision(server, ws_tmp):
    """`pick_snapshot`: 只认 `<CAT>-<时间戳>` 精确模式(目录穿越/别类目名一律拒)。"""
    _mk(ws_tmp)
    _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    _mk_snap(ws_tmp, "Cutting-Boards-US-202608-20260916-120000")   # 名字互为前缀的别类目
    assert server.pick_snapshot(CAT, "data", "") is not None
    assert server.pick_snapshot(CAT, "data", "../") is None
    assert server.pick_snapshot(CAT, "data", "Cutting-Boards-US-202608-20260916-120000") is None
    assert server.pick_snapshot(CAT, "data", "不存在的快照") is None
    assert server.pick_snapshot("Cutting-Boards", "data", "Cutting-Boards-US-202608-20260916-120000") is None


def test_download_from_archive_ok_and_containment(server, ws_tmp):
    """从归档下载: 允许根 = 该快照目录; 目录穿越/越界/换根一律拒。"""
    d = _mk(ws_tmp)
    snap = _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    p = _touch(snap / "10_父项变体" / f"{CAT}-results.jsonl", "{}")
    rel = f"10_父项变体/{CAT}-results.jsonl"
    got = server._artifact_abs(CAT, "data", 10, rel, source="archive",
                               snapshot=f"{CAT}-20260916-120000")
    assert got is not None and got.resolve() == p.resolve()
    assert server._artifact_abs(CAT, "data", 10, "../config.yaml", source="archive") is None
    assert server._artifact_abs(CAT, "data", 10, rel, source="archive", snapshot="乱写的") is None
    # 同一个相对路径在"实时根"下不存在(实时目录里是**平铺**的), 说明两种来源不会串
    _touch(d / f"{CAT}-results.jsonl")
    assert server._artifact_abs(CAT, "data", 10, rel, source="live") is None


def test_api_step_files_defaults_to_archive_and_can_fall_back_to_live(server, ws_tmp):
    """接口: 缺省 source=archive(界面用); 显式 `source=live` 仍可拿到旧口径。"""
    d = _mk(ws_tmp)
    snap = _mk_snap(ws_tmp, f"{CAT}-20260916-120000")
    _touch(snap / "10_父项变体" / f"{CAT}-results.jsonl", "{}")
    _touch(d / f"{CAT}-variant-texts.jsonl")             # 只在实时目录(未声明)
    h = server.Handler.__new__(server.Handler)
    q = {"file": [f"data/{CAT}/config.yaml"], "step": ["10"]}
    arch = h._api_step_files(q)
    assert arch["ok"] is True and arch["source"] == "archive"
    assert [f["name"] for f in arch["files"]] == [f"10_父项变体/{CAT}-results.jsonl"]
    assert "unclaimed_count" not in arch, "归档响应不再带未归属计数(用户 2026-09-16 要求)"

    live = h._api_step_files({**q, "source": ["live"]})
    assert live["source"] == "live"
    assert f"{CAT}-variant-texts.jsonl" in [f["name"] for f in live["unclaimed"]]


def test_index_html_card_reads_archive(server):
    """前端: 卡片请求带 `source=archive`, 渲染"归档内容"表与快照信息, 且不再渲染未归属表。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert "&step=${want}&source=archive" in html, "卡片请求要带 source=archive"
    assert "归档内容 · " in html, "应有'归档内容'表头"
    assert "归档快照: " in html and "snapshot_created" in html, "要显示快照创建/同步时间"
    assert "尚无归档快照" in html, "无快照时要有明确提示"
    assert "该快照里还没有" in html, "该步骤未归档时要有明确提示"
    assert '<div class="hint" style="margin-top:10px">另有' not in html, \
        "末尾那行'另有 N 个类目根文件未被任何步骤声明…'的提示必须已移除(用户 2026-09-16 要求)"
    assert "d.unclaimed_count" not in html, "前端不再读未归属计数"
    assert "未归属产物 · 类目根其它文件" in html, "旧口径(source=live)的回退分支要保留"
    # 下载链接在归档模式下要带 source/snapshot(旧服务进程则自动不带)
    assert "&source=archive&snapshot=" in html and "sfArchive" in html
