# 单元测试: 类目中心的「📊 整合报告」入口(2026-10-09)
#
# 用户口径: ① 三份输入(步骤8 关键词 / 步骤18 标准化交付 / 步骤19 VOC)**缺一不可**, 缺则不生成;
#            ② 入口做在**类目中心**(不是产物面板); ③ 生成后点开即看(HTML)。
# 本文件钉住: 状态探测(缺哪一份/成品在不在)、类目名白名单(防目录穿越)、
# 生成接口的两条路径(门槛拒 / 真跑并回报)、以及前端接线(类目中心行内按钮 + 两个接口路径)。
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import webui.server as server                                    # noqa: E402

CAT = "Demo Cat-US-202608"


def _mk_inputs(data_root: Path, *, kw=True, attr=True, voc=True) -> Path:
    cat_dir = data_root / CAT
    (cat_dir / "18_标准化").mkdir(parents=True, exist_ok=True)
    (cat_dir / "19_VOC报告").mkdir(parents=True, exist_ok=True)
    if kw:
        (cat_dir / f"{CAT}-keyword-report.xlsx").write_bytes(b"kw")
    if attr:
        (cat_dir / "18_标准化" / f"{CAT}-属性标注-标准化交付.xlsx").write_bytes(b"attr")
    if voc:
        (cat_dir / "19_VOC报告" / f"{CAT}-VOC报告.xlsx").write_bytes(b"voc")
    return cat_dir


# --------------------------------------------------------------------------- 状态探测

def test_status_all_present_but_not_generated(tmp_path):
    _mk_inputs(tmp_path)
    st = server.integrated_report_status(CAT, str(tmp_path))
    assert st["inputs_ok"] is True and st["missing"] == []
    assert st["html"] is None and st["json"] is False       # 还没生成


@pytest.mark.parametrize("kw,attr,voc,step", [(False, True, True, 8), (True, False, True, 18),
                                              (True, True, False, 19)])
def test_status_reports_which_input_is_missing(tmp_path, kw, attr, voc, step):
    _mk_inputs(tmp_path, kw=kw, attr=attr, voc=voc)
    st = server.integrated_report_status(CAT, str(tmp_path))
    assert st["inputs_ok"] is False
    assert [m["step"] for m in st["missing"]] == [step]
    assert st["missing"][0]["name"]                          # 必须点名(前端 toast 直接展示)


def test_status_detects_generated_html(tmp_path):
    cat_dir = _mk_inputs(tmp_path)
    out = cat_dir / "20_整合市场报告" / "html"
    out.mkdir(parents=True)
    (out / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (out / "report.json").write_text("{}", encoding="utf-8")
    st = server.integrated_report_status(CAT, str(tmp_path))
    assert st["html"] and st["html"]["size"] > 0 and st["html"]["mtime"]
    assert st["json"] is True and st["dir"].endswith("20_整合市场报告\\html") or st["dir"].endswith("20_整合市场报告/html")


# --------------------------------------------------------------------------- 类目名白名单

@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "../x", "a/b", "a\\b", "C:evil", "a*b", 'a"b'])
def test_safe_cat_name_rejects_path_tricks(bad):
    with pytest.raises(server.UserFacingError):
        server.safe_cat_name(bad)


@pytest.mark.parametrize("good", ["Dog Dryer-US-202608", "Cutting Boards-US-202608", "salt-pepper-shaker-sets"])
def test_safe_cat_name_accepts_normal_names(good):
    assert server.safe_cat_name(good) == good


# --------------------------------------------------------------------------- GET 状态接口

def test_api_integrated_returns_status(tmp_path):
    _mk_inputs(tmp_path, voc=False)
    out = server.Handler._api_integrated(None, {"cat": [CAT], "data_root": [str(tmp_path)]})
    assert out["ok"] is True and out["inputs_ok"] is False
    assert [m["step"] for m in out["missing"]] == [19]


def test_api_integrated_rejects_bad_cat(tmp_path):
    with pytest.raises(server.UserFacingError):
        server.Handler._api_integrated(None, {"cat": ["../etc"], "data_root": [str(tmp_path)]})


# --------------------------------------------------------------------------- POST 生成接口

def test_build_refuses_when_any_input_missing(tmp_path, monkeypatch, capsys):
    _mk_inputs(tmp_path, voc=False)
    called = []
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: called.append(a) or None)
    with pytest.raises(server.UserFacingError) as ei:
        server.Handler._api_integrated_build(None, {"cat": CAT, "data_root": str(tmp_path)})
    msg = str(ei.value)
    assert "缺一不可" in msg and "步骤19" in msg
    assert called == [], "门槛不过时绝不能真去跑脚本"


def test_build_runs_script_and_reports_success(tmp_path, monkeypatch):
    cat_dir = _mk_inputs(tmp_path)
    seen = {}

    class _R:
        returncode, stdout, stderr = 0, "三份输入齐备, 继续生成。\n✓ 整合报告(HTML): …", ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        out = cat_dir / "20_整合市场报告" / "html"
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text("<html>integrated</html>", encoding="utf-8")
        return _R()

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    lines = []
    monkeypatch.setattr(server, "push_cat", lambda cat, text, level="INFO": lines.append((cat, text, level)))
    out = server.Handler._api_integrated_build(None, {"cat": CAT, "data_root": str(tmp_path)})
    assert out["ok"] is True and out["status"]["html"]
    cmd = seen["cmd"]
    assert cmd[0] == server.sys.executable and "--cat" in cmd and CAT in cmd
    assert str(tmp_path) in cmd                              # data_root 必须透传(否则找不到输入)
    assert any("生成整合报告" in t for _, t, _ in lines) and any("已生成" in t for _, t, _ in lines)
    assert seen["kw"]["cwd"] == str(server.PROJECT_ROOT)


def test_build_reports_failure_with_exit_code(tmp_path, monkeypatch):
    _mk_inputs(tmp_path)

    class _R:
        returncode, stdout, stderr = 1, "boom", "Traceback: xxx"

    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(server, "push_cat", lambda *a, **k: None)
    out = server.Handler._api_integrated_build(None, {"cat": CAT, "data_root": str(tmp_path)})
    assert out["ok"] is False and out["code"] == 1 and "Traceback" in out["error"]


# --------------------------------------------------------------------------- 进度上报(按钮上显示)

def _write_progress(cat_dir: Path, *, phase="读取步骤18 属性标注-标准化交付", pct=30,
                    done=False, error="", age_sec=0) -> Path:
    import json as _json
    import os as _os
    import time as _time
    p = cat_dir / "20_整合市场报告" / ".build-progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({"phase": phase, "pct": pct, "done": done, "error": error,
                              "ts": "2026-10-09 12:00:00", "pid": 1234}, ensure_ascii=False),
                 encoding="utf-8")
    if age_sec:
        old = _time.time() - age_sec
        _os.utime(p, (old, old))
    return p


def test_progress_absent_means_not_running(tmp_path):
    _mk_inputs(tmp_path)
    assert server.integrated_progress(CAT, str(tmp_path)) == {"running": False}


def test_progress_fresh_reports_phase_and_pct(tmp_path):
    cat_dir = _mk_inputs(tmp_path)
    _write_progress(cat_dir, phase="渲染单文件 HTML", pct=95)
    pr = server.integrated_progress(CAT, str(tmp_path))
    assert pr["running"] is True and pr["phase"] == "渲染单文件 HTML" and pr["pct"] == 95
    assert pr["stale"] is False


def test_progress_done_is_not_running(tmp_path):
    cat_dir = _mk_inputs(tmp_path)
    _write_progress(cat_dir, phase="完成", pct=100, done=True)
    pr = server.integrated_progress(CAT, str(tmp_path))
    assert pr["running"] is False and pr["done"] is True


def test_progress_stale_is_not_running(tmp_path):
    """进程被杀/看板重启留下的僵尸进度, 不能让按钮永远卡在"生成中"。"""
    cat_dir = _mk_inputs(tmp_path)
    _write_progress(cat_dir, pct=30, age_sec=server.INTEGRATED_PROGRESS_STALE_SEC + 60)
    pr = server.integrated_progress(CAT, str(tmp_path))
    assert pr["running"] is False and pr["stale"] is True


def test_status_carries_progress(tmp_path):
    cat_dir = _mk_inputs(tmp_path)
    _write_progress(cat_dir, phase="解析步骤19 VOC报告", pct=72)
    st = server.integrated_report_status(CAT, str(tmp_path))
    assert st["progress"]["running"] is True and st["progress"]["pct"] == 72


def test_api_progress_endpoint(tmp_path):
    cat_dir = _mk_inputs(tmp_path)
    _write_progress(cat_dir, phase="契约自检", pct=88)
    out = server.Handler._api_integrated_progress(None, {"cat": [CAT], "data_root": [str(tmp_path)]})
    assert out["ok"] is True and out["phase"] == "契约自检" and out["running"] is True


# --------------------------------------------------------------------------- 类目中心下载

@pytest.mark.parametrize("kind,rel", [
    ("integrated", "20_整合市场报告/html/index.html"),
    ("keyword", "{cat}-keyword-report.xlsx"),
    ("product", "18_标准化/{cat}-属性标注-标准化交付.xlsx"),
    ("voc", "19_VOC报告/{cat}-VOC报告.xlsx"),
])
def test_download_paths_for_four_kinds(tmp_path, kind, rel):
    cat_dir = _mk_inputs(tmp_path)
    if kind == "integrated":
        (cat_dir / "20_整合市场报告" / "html").mkdir(parents=True)
        (cat_dir / "20_整合市场报告" / "html" / "index.html").write_text("<html>x</html>", encoding="utf-8")
    p = server.integrated_download_path(CAT, str(tmp_path), kind)
    assert p is not None and p.is_file(), p
    assert p.name == Path(rel.format(cat=CAT)).name


@pytest.mark.parametrize("kind", ["", "evil", "../etc/passwd", "integrated/../.."])
def test_download_kind_whitelist(tmp_path, kind):
    _mk_inputs(tmp_path)
    assert server.integrated_download_path(CAT, str(tmp_path), kind) is None


class _FakeHandler:
    """只带 `_json`/`_send_file` 的假 handler(下载接口会用到 self, 不能用 None 调)。"""

    def __init__(self):
        self.json_calls = []
        self.file_calls = []

    def _json(self, obj, code: int = 200):
        self.json_calls.append((obj, code))

    def _send_file(self, path, name):
        self.file_calls.append((Path(path), name))


def test_download_handler_rejects_unknown_kind(tmp_path):
    _mk_inputs(tmp_path)
    h = _FakeHandler()
    server.Handler._api_integrated_download(h, {"cat": [CAT], "data_root": [str(tmp_path)],
                                               "kind": ["evil"]})
    obj, code = h.json_calls[-1]
    assert code == 400 and "未知的下载类型" in obj["error"] and h.file_calls == []


def test_download_handler_404_when_file_missing(tmp_path):
    _mk_inputs(tmp_path, voc=False)
    h = _FakeHandler()
    server.Handler._api_integrated_download(h, {"cat": [CAT], "data_root": [str(tmp_path)],
                                               "kind": ["voc"]})
    obj, code = h.json_calls[-1]
    assert code == 404 and "文件不存在" in obj["error"] and h.file_calls == []


@pytest.mark.parametrize("kind,src_name,dl_name", [
    ("voc", "{cat}-VOC报告.xlsx", "{cat}-VOC报告.xlsx"),
    ("keyword", "{cat}-keyword-report.xlsx", "{cat}-关键词报告.xlsx"),
    ("product", "{cat}-属性标注-标准化交付.xlsx", "{cat}-属性标注-标准化交付.xlsx"),
])
def test_download_handler_sends_three_source_files(tmp_path, kind, src_name, dl_name):
    cat_dir = _mk_inputs(tmp_path)
    h = _FakeHandler()
    server.Handler._api_integrated_download(h, {"cat": [CAT], "data_root": [str(tmp_path)],
                                               "kind": [kind]})
    assert not h.json_calls
    path, name = h.file_calls[-1]
    assert path.is_file() and path.name == src_name.format(cat=CAT)
    assert cat_dir in path.parents                       # 必须落在该类目目录内
    assert name == dl_name.format(cat=CAT)               # 下载文件名(可读名, 未必等于源文件名)


def test_download_handler_sends_integrated_html(tmp_path):
    cat_dir = _mk_inputs(tmp_path)
    out = cat_dir / "20_整合市场报告" / "html"
    out.mkdir(parents=True)
    (out / "index.html").write_text("<html>integrated</html>", encoding="utf-8")
    h = _FakeHandler()
    server.Handler._api_integrated_download(h, {"cat": [CAT], "data_root": [str(tmp_path)],
                                               "kind": ["integrated"]})
    path, dl_name = h.file_calls[-1]
    assert path.name == "index.html" and dl_name == f"{CAT} 市场调研整合报告.html"


def test_progress_path_agrees_between_script_and_server(tmp_path):
    """脚本写进度的位置必须与看板读的位置**同一个文件**(否则按钮永远显示不出进度)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_birh_pp", ROOT / "scripts" / "build_integrated_report_html.py")
    birh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(birh)
    out_dir = server.cat_root_dir(CAT, str(tmp_path)) / server.INTEGRATED_REPORT_SUBDIR   # …/20_整合市场报告/html
    written = birh.progress_path(out_dir)                               # 脚本: 产出目录的上一级
    read_dir = server.cat_root_dir(CAT, str(tmp_path)) / server.INTEGRATED_REPORT_SUBDIR.parent
    assert written.parent == read_dir and written.name == server.INTEGRATED_PROGRESS_NAME
    # 真的写一份, 看板要能读到
    birh._progress(written, "渲染单文件 HTML", 95)
    pr = server.integrated_progress(CAT, str(tmp_path))
    assert pr["running"] is True and pr["phase"] == "渲染单文件 HTML" and pr["pct"] == 95


# --------------------------------------------------------------------------- 生成中的互斥

def test_build_refuses_while_another_build_is_running(tmp_path, monkeypatch):
    cat_dir = _mk_inputs(tmp_path)
    _write_progress(cat_dir, phase="装配市场大盘模块", pct=60)
    called = []
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: called.append(a) or None)
    with pytest.raises(server.UserFacingError) as ei:
        server.Handler._api_integrated_build(None, {"cat": CAT, "data_root": str(tmp_path)})
    assert "正在生成中" in str(ei.value)
    assert called == []


def test_build_failure_surfaces_stdout_tail_when_stderr_empty(tmp_path, monkeypatch):
    """脚本把失败原因打在 stdout(最后几行), stderr 常为空 —— 不留兜底前端只会看到 "HTTP 200"
    (2026-10-10 真事故: 确实显示成了 "整合报告生成失败: HTTP 200")。"""
    _mk_inputs(tmp_path)

    class _R:
        returncode = 1
        stdout = "门槛通过\n✗ 生成失败: ValueError: VOC 工作簿路径不存在: None\n"
        stderr = ""

    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(server, "push_cat", lambda *a, **k: None)
    out = server.Handler._api_integrated_build(None, {"cat": CAT, "data_root": str(tmp_path)})
    assert out["ok"] is False and out["code"] == 1
    assert out["error"].strip(), "error 不能为空(否则前端退回显示 HTTP 200)"
    assert "VOC 工作簿路径不存在" in out["error"]


def test_build_failure_without_any_output_still_has_message(tmp_path, monkeypatch):
    _mk_inputs(tmp_path)

    class _R:
        returncode, stdout, stderr = 2, "", ""

    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(server, "push_cat", lambda *a, **k: None)
    out = server.Handler._api_integrated_build(None, {"cat": CAT, "data_root": str(tmp_path)})
    assert out["ok"] is False and "退出码 2" in out["error"]


# --------------------------------------------------------------------------- 接线(源码级)

def test_server_routes_wired():
    src = (ROOT / "webui" / "server.py").read_text(encoding="utf-8")
    for probe in ('elif path == "/api/integrated-report":',
                  'elif path == "/api/integrated-report/view":',
                  'elif path == "/api/integrated-report/progress":',
                  'elif path == "/api/integrated-report/download":',
                  'elif path == "/api/integrated-report/build":',
                  "self._api_integrated(q)", "self._serve_integrated_report(q)",
                  "self._api_integrated_progress(q)", "self._api_integrated_download(q)",
                  "self._api_integrated_build(self._read_body())",
                  '"integrated": integrated_report_status(cat, _droot)',
                  "def integrated_download_path(", "def integrated_progress("):
        assert probe in src, probe


def test_index_report_overlay_wired():
    """2026-10-10 用户要求: **网页内直接看报告**(不跳走) —— 弹层 + iframe + 新标签/下载入口。"""
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    for probe in ('id="reportOverlay"', 'id="reportFrame"', "function reportUrl(",
                  "function closeIntegratedReport(", "closeIntegratedReport();",
                  'id="rpNewTab"', 'id="rpDownload"', 'id="rpClose"',
                  "box report", "#reportFrame{flex:1", "Escape"):
        assert probe in html, probe
    # 打开 = 弹层里加载 /view; 打开与下载必须分开(下载走 attachment)
    assert 'src = reportUrl(cat)' in html
    assert "🔍 打开报告(新标签)" in html and "⬇ 整合报告(HTML)" in html


def test_index_center_button_and_download_wired():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    for probe in ("function hubIntegratedHtml(", "function hubDownloadHtml(",
                  "function openIntegratedReport(", "function buildIntegratedReport(",
                  'data-act="report-open"', 'data-act="report-build"',
                  "/api/integrated-report/view?cat=", 'api("/api/integrated-report/build"',
                  "hubIntegratedHtml(it)", "hubDownloadHtml(it)",
                  "/api/integrated-report/progress?cat=",          # 按钮进度轮询
                  "/api/integrated-report/download?cat=",           # 类目中心下载
                  "setInterval(poll, 800)", "clearInterval(timer)"):
        assert probe in html, probe
    for kind in ('"integrated"', '"keyword"', '"product"', '"voc"'):
        assert kind in html, kind
    # 四个下载项都要出现在类目中心那一行(与生成按钮同格)
    row = next(ln for ln in html.splitlines() if "hubIntegratedHtml(it)" in ln
               and "hubDownloadHtml(it)" in ln and "data-act=\"view\"" in ln)
    assert row                                          # 与「打开运行台/续跑」同一格
