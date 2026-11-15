# -*- coding: utf-8 -*-
"""静态回归: `ctx.browser(...)` 只能用 with 绑定, 不能赋值后当浏览器用。

背景(2026-09-16): `steps/step11_traffic_source.py` 的备用账号分支写成

    browser = ctx.browser("sellersprite", downloads_path=download_dir)
    browser.__enter__()
    try:
        page = browser.new_page()      # ← browser 其实是 SsBrowserLease

于是步骤11 在"主账号导出→备用账号导出"这条路径上直接失败:
`'SsBrowserLease' object has no attribute 'new_page'`(耗时 7 秒),
步骤 12~17 全部留在 PENDING。同一写法在 step12 备用账号分支里也有。

`SsBrowserLease.__enter__()` 返回的**才是** BrowserAutomation, 所以正确写法只有
`with ctx.browser(...) as b:`。本测试用 AST 静态扫描, 禁止"赋值式使用租约",
把这类错误挡在提交前(不依赖跑到真实浏览器才能发现)。
"""
from __future__ import annotations

import ast
import io
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ["steps", "scripts", "_sim"]
SCAN_FILES = ["main.py"]
# 不扫描的目录(相对仓库根, 用路径片段匹配): `scripts/_plan_pilot` 是**临时脚本/自测目录**,
# 里面的文件不是产品代码(实测出现过带 BOM 的文件 → ast.parse 直接 SyntaxError,
# 把"只扫产品源码"的这条静态回归误伤成失败, 2026-10-08)。pytest.ini 的 norecursedirs
# 已把该目录从**收集**里排除, 这里是**文件扫描**层面的同类排除。
SCAN_EXCLUDE = ("scripts/_plan_pilot",)


def _target_files() -> list[str]:
    out: list[str] = []
    for rel in SCAN_FILES:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            out.append(p)
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in ("__pycache__", ".venv")]
            rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
            if any(rel_dir.startswith(x) or x in rel_dir for x in SCAN_EXCLUDE):
                continue
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    out.append(os.path.join(dirpath, fn))
    return out


def _is_ctx_browser_call(node: ast.AST) -> bool:
    """判断节点是不是 `ctx.browser(...)` / `<something>.browser(...)` 调用。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "browser"
    )


def _parents(tree: ast.AST) -> dict:
    out: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _bad_uses(path: str) -> list[str]:
    """返回该文件里所有"赋值式使用 ctx.browser 租约"的位置描述。"""
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=path)
    parents = _parents(tree)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not _is_ctx_browser_call(node):
            continue
        parent = parents.get(id(node))
        # 合法: 直接作为 with 的上下文表达式 (`with ctx.browser(...) as b:`)
        if isinstance(parent, ast.withitem):
            continue
        # 合法: 作为另一层 with 表达式的组成部分(例如条件表达式选资源)
        if isinstance(parent, ast.With) or isinstance(parent, ast.AsyncWith):
            continue
        rel = os.path.relpath(path, ROOT)
        lineno = getattr(node, "lineno", 0)
        kind = type(parent).__name__ if parent is not None else "?"
        bad.append(f"{rel}:{lineno} (父节点 {kind})")
    return bad


def test_ctx_browser_only_used_as_context_manager():
    """全仓 `ctx.browser(...)` 必须出现在 with 语句里。"""
    files = _target_files()
    assert files, "没有扫描到任何源文件, 断言会变成空转"
    offenders: list[str] = []
    for p in files:
        try:
            offenders.extend(_bad_uses(p))
        except SyntaxError as exc:  # pragma: no cover - 源码语法错误另有人管
            pytest.fail(f"{p} 语法错误: {exc}")
    assert not offenders, (
        "以下位置把 ctx.browser(...) 的返回值(租约 SsBrowserLease)当浏览器用了; "
        "应改为 `with ctx.browser(...) as b:`:\n  " + "\n  ".join(offenders)
    )


def test_step11_step12_backup_branch_uses_with():
    """针对本次事故的两个具体位置: 备用账号分支必须是 with 绑定。"""
    checked = 0
    for rel in (
        "steps/step11_traffic_source.py",
        "steps/step12_competitor_submit.py",
        "_sim/steps/step11_traffic_source.py",
        "_sim/steps/step12_competitor_submit.py",
    ):
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        checked += 1
        with io.open(p, encoding="utf-8") as fh:
            src = fh.read()
        assert "with ctx.browser(" in src, f"{rel}: 未找到 with ctx.browser(...) 绑定"
        if "step12_" in rel:
            assert "profile_ss_backup" in src, f"{rel}: 备用账号 Profile 逻辑丢了"
        assert ".__enter__()" not in src, f"{rel}: 仍有手动 __enter__() 残留"
        assert "__exit__(None, None, None)" not in src, f"{rel}: 仍有手动 __exit__ 残留"
        assert "_lease" not in src, f"{rel}: 仍有 lease 变量式用法(应直接 with ctx.browser)"
    assert checked >= 2, "至少应校验到 steps/ 下的两个文件"


def _run_with_fake_browser(tmp_path, monkeypatch, step, method: str, acct: dict):
    """用假浏览器跑指定方法, 返回 (返回值, [每个假浏览器 (构造参数, 事件)] 的列表)。"""
    import steps.base as base
    from core.config import Config
    from core.paths import Paths

    monkeypatch.setenv("PIPELINE_SS_LOCK_DIR", str(tmp_path / "locks"))
    made: list = []

    class _FakeBrowser:
        def __init__(self, config, **kw):
            self.kw = kw
            self.events: list = []
            self.page_calls = 0
            made.append(self)

        def __enter__(self):
            self.events.append("enter")
            return self

        def __exit__(self, *a):
            self.events.append("exit")
            return None

        def new_page(self):
            self.events.append("new_page")
            self.page_calls += 1
            return "PAGE"

        def set_download_dir(self, page, d):
            self.events.append("set_download_dir")
            assert page == "PAGE", f"set_download_dir 收到的不是 new_page 的返回值: {page!r}"

    monkeypatch.setattr(base, "BrowserAutomation", _FakeBrowser)
    cfg = Config({"CAT": "CatA", "DATA_ROOT": str(tmp_path), "DRY_RUN": False,
                  "SS_PROFILE": str(tmp_path / "profiles" / "ss-profile"),
                  "SS_CDP_URL": "http://127.0.0.1:9222", "SITE": "美国",
                  "SS_ACCOUNT": "main@example.com"})
    ctx = base.StepContext(cfg, Paths(cfg))
    step.log = lambda *a, **k: None
    step._ensure_login_for = lambda *a, **k: None
    dl = str(tmp_path / "dl")
    os.makedirs(dl, exist_ok=True)
    if method == "_run_groups_on_account":            # step11: (ctx, acct, todo, site, ...)
        out = step._run_groups_on_account(ctx, acct, [], "美国", "2026-08", 5, 1000, dl)
    else:                                             # step12: (ctx, acct, todo, site, ...)
        out = step._run_account_batches(ctx, acct, [], "美国", [],
                                        "fp", str(tmp_path / "submitted.json"), False)
    return out, made, Paths(cfg)


def test_step11_backup_account_gets_real_browser(tmp_path, monkeypatch):
    """行为级回归(2026-09-16 事故): 备用账号分支必须真能 new_page。

    事故现场: `browser = ctx.browser(...)` 拿到的是 SsBrowserLease, 直接
    `browser.new_page()` → `'SsBrowserLease' object has no attribute 'new_page'`,
    步骤11 失败并让 12~17 全部 PENDING。这里用真实 StepContext.browser 工厂(假浏览器)
    跑一遍备用账号分支: 使用主账号相同的普通 Chrome 连接和 Profile。
    """
    from steps.step11_traffic_source import Step11TrafficSource

    step = Step11TrafficSource()
    out, made, paths = _run_with_fake_browser(
        tmp_path, monkeypatch, step, "_run_groups_on_account",
        {"kind": "backup", "index": 1, "note": "备1"},
    )
    assert out == []
    assert made, "备用账号分支没有创建浏览器"
    b = made[-1]
    assert b.events[:2] == ["enter", "new_page"], f"事件顺序不对: {b.events}"
    assert b.page_calls == 1
    assert b.kw.get("profile_dir") == paths.profile_ss()
    assert b.kw.get("cdp_url") == "http://127.0.0.1:9222"
    assert b.events[-1] == "exit", "浏览器租约必须释放"


def test_step11_main_account_keeps_cdp(tmp_path, monkeypatch):
    """主账号仍走 CDP 直连(本次改动不能把主账号路径带偏)。"""
    from steps.step11_traffic_source import Step11TrafficSource

    step = Step11TrafficSource()
    out, made, _ = _run_with_fake_browser(
        tmp_path, monkeypatch, step, "_run_groups_on_account",
        {"kind": "main", "index": 0, "note": "主账号"},
    )
    assert out == []
    b = made[-1]
    assert b.page_calls == 1
    assert b.kw.get("cdp_url") == "http://127.0.0.1:9222"
    assert b.kw.get("headed") is None, "主账号不该强制有头(CDP 直连不弹窗)"
    assert b.kw.get("profile_dir") == str(tmp_path / "profiles" / "ss-profile")


def test_step12_backup_account_gets_real_browser(tmp_path, monkeypatch):
    """step12 备用账号分支同样必须真能 new_page(同一类写法, 一起钉住)。"""
    from steps.step12_competitor_submit import Step12CompetitorSubmit

    step = Step12CompetitorSubmit()
    out, made, paths = _run_with_fake_browser(
        tmp_path, monkeypatch, step, "_run_account_batches",
        {"kind": "backup", "index": 2, "note": "备2"},
    )
    assert out == []
    b = made[-1]
    assert b.events[:2] == ["enter", "new_page"], f"事件顺序不对: {b.events}"
    assert b.kw.get("headed") is True
    assert b.kw.get("profile_dir") == paths.profile_ss_backup(2)
    assert b.kw.get("cdp_url") is None


def test_lease_attribute_error_is_actionable():
    """误用租约时的报错要能直接指出改法(而不是只说没有这个属性)。"""
    from core.ss_lock import SsBrowserLease

    class FakeBrowser:
        profile_dir = "x"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def new_page(self):  # pragma: no cover - 只用于 hasattr 探测
            return "page"

    lease = SsBrowserLease(FakeBrowser(), wait_seconds=0)
    with pytest.raises(AttributeError) as ei:
        lease.new_page()
    msg = str(ei.value)
    assert "with ctx.browser" in msg
    assert "new_page" in msg
    # 内部属性查找不能被拦截(否则 __enter__/__exit__ 自己会坏)
    assert lease._browser is not None
    assert hasattr(lease, "__enter__")
    assert not hasattr(lease, "definitely_not_a_browser_method")
