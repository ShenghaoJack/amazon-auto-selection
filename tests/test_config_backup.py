# 单元测试: core/config_backup.py(2026-09 版: 版本化+去重+事件日志)
import re
import shutil
from pathlib import Path

import core.config_backup as cb

_TMP = Path(__file__).resolve().parent / "_cb_tmp"


def _setup(monkeypatch):
    shutil.rmtree(_TMP, ignore_errors=True)
    _TMP.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cb, "BACKUP_DIR", _TMP)
    # 2026-09-11 修复(测试隔离): backup_dirs()/backup_paths() 会扫描 PROJECT_ROOT/data/*/config-backups。
    # 老写法只换 BACKUP_DIR 且用例里用了**真类目名**(Salt and Pepper Mill Sets-US-202608), 于是:
    #   ① _next_version 看到真实目录已有 v1 → 新建备份变成 v2(断言 v1 失败);
    #   ② prune(keep=2) 把**真实类目备份**当测试数据删掉(不可恢复)。
    # 这里把 PROJECT_ROOT 一并指向临时目录, 让扫描范围与版本号完全隔离。
    monkeypatch.setattr(cb, "PROJECT_ROOT", _TMP)


def _cfg(tmp_name="tstcfg"):
    p = _TMP / f"{tmp_name}.yaml"
    p.write_text('CAT: "Salt and Pepper Mill Sets-US-202608"\nTRAFFIC_MONTH: "2026-08"\n', encoding="utf-8")
    return p


def test_versioned_name_and_header(monkeypatch):
    _setup(monkeypatch)
    p = _cfg()
    b1 = cb.make_backup(p, reason="webui 保存前自动备份", changed_to='CAT: "x"\n')
    assert b1 is not None
    assert b1.name == "Salt-and-Pepper-Mill-Sets-US-202608-v1.bak"
    text = b1.read_text(encoding="utf-8")
    assert "# 版本: v1" in text
    assert "# 备份原因: webui 保存前自动备份" in text
    assert "# 备份时间: " in text          # 日期时间记在头部/日志, 不在文件名
    b2 = cb.make_backup(p, reason="webui 保存前自动备份", changed_to='CAT: "y"\n')
    assert b2 is not None and b2.name.endswith("-v2.bak")
    info = cb.parse_name(b1.name)
    assert info and info["cat"].startswith("Salt-and-Pepper") and info["version"] == 1


def test_no_change_skips_backup(monkeypatch):
    _setup(monkeypatch)
    p = _cfg()
    orig = p.read_text(encoding="utf-8")
    log_lines = []
    # changed_to == 当前内容 → 不生成(仍是原数据), 且记日志
    assert cb.make_backup(p, reason="webui 保存前自动备份",
                          log=log_lines.append, changed_to=orig) is None
    assert not list(cb.BACKUP_DIR.glob("*.bak"))
    assert any("无实际改动" in x for x in log_lines)


def test_prune_by_category_keeps_latest(monkeypatch):
    import os
    _setup(monkeypatch)
    files = []
    for v, mtime in ((1, 100.0), (2, 200.0), (3, 300.0)):
        f = cb.BACKUP_DIR / f"Salt-and-Pepper-Mill-Sets-US-202608-v{v}.bak"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (mtime, mtime))
        files.append(f)
    other = cb.BACKUP_DIR / "Other-Cat-v1.bak"
    other.write_text("x", encoding="utf-8")
    os.utime(other, (999.0, 999.0))
    removed = cb.prune(keep=2)
    assert len(removed) == 1 and "v1" in removed[0].name
    left = sorted(x.name for x in cb.BACKUP_DIR.glob("*.bak"))
    assert any("v2" in x for x in left) and any("v3" in x for x in left)
    assert any("Other-Cat" in x for x in left)


def test_restore_strips_header_and_autobackup(monkeypatch):
    _setup(monkeypatch)
    p = _cfg()
    orig = p.read_text(encoding="utf-8")
    bak = cb.make_backup(p, reason="测试备份", changed_to='CAT: "zzz"\n')
    p.write_text("CAT: broken\n", encoding="utf-8")
    target = cb.restore_backup(bak, to=p)
    assert target == p
    assert p.read_text(encoding="utf-8") == orig
    reasons = [r["reason"] for r in cb.list_backups()]
    assert any("恢复备份" in (r or "") for r in reasons)


def test_backup_goes_to_category_dir(monkeypatch):
    """类目配置(data/<CAT>/config.yaml)的备份应落在该类目自己的 config-backups/ 里。"""
    fake = _TMP / "proj"
    catdir = fake / "data" / "Sim Cat A-US-202608"
    catdir.mkdir(parents=True)
    cfg = catdir / "config.yaml"
    cfg.write_text('CAT: "Sim Cat A-US-202608"\nTRAFFIC_MONTH: "2026-08"\n', encoding="utf-8")
    monkeypatch.setattr(cb, "PROJECT_ROOT", fake)
    monkeypatch.setattr(cb, "BACKUP_DIR", fake / ".webui" / "config-backups")
    b = cb.make_backup(cfg, reason="保存前自动备份",
                       changed_to='CAT: "Sim Cat A-US-202608"\nTRAFFIC_MONTH: "2026-09"\n')
    assert b is not None
    assert b.parent == catdir / "config-backups"
    assert b.name == "Sim-Cat-A-US-202608-v1.bak"
    assert any(x["file"] == b.name for x in cb.list_backups())
    assert cb.prune(keep=10) == []
    restored = cb.restore_backup(b)   # 自动定位回 data/<CAT>/config.yaml
    assert restored == cfg
    assert 'TRAFFIC_MONTH: "2026-08"' in cfg.read_text(encoding="utf-8")


# ---------- 2026-09-13: 清理留痕(为什么加: 备份无声消失, 事后查不到谁删的) ----------

def test_prune_calls_log_with_details(monkeypatch):
    """prune 每清理一份都回调, 带 名称/CAT/原因/原目录 —— 供落盘留痕。"""
    import os
    _setup(monkeypatch)
    catdir = _TMP / "proj" / "data" / "Sim Cat A-US-202608" / "config-backups"
    catdir.mkdir(parents=True, exist_ok=True)
    for v, mtime in ((1, 100.0), (2, 200.0)):
        f = catdir / f"Sim-Cat-A-US-202608-v{v}.bak"
        f.write_text(f"# CAT: Sim Cat A-US-202608\n# 备份原因: webui 保存前自动备份\nbody{v}\n",
                     encoding="utf-8")
        os.utime(f, (mtime, mtime))
    monkeypatch.setattr(cb, "PROJECT_ROOT", _TMP / "proj")
    got = []
    removed = cb.prune(keep=1, log=got.append)
    assert len(removed) == 1 and removed[0].name.endswith("-v1.bak")
    assert len(got) == 1
    d = got[0]
    assert d["name"].endswith("-v1.bak") and d["cat"] == "Sim Cat A-US-202608"
    assert d["reason"] == "webui 保存前自动备份" and d["dir"] == "config-backups"
    assert d["dry"] is False


def test_prune_dry_run_keeps_files_but_reports(monkeypatch):
    import os
    _setup(monkeypatch)
    for v, mtime in ((1, 100.0), (2, 200.0)):
        f = cb.BACKUP_DIR / f"Salt-and-Pepper-Mill-Sets-US-202608-v{v}.bak"
        f.write_text("# CAT: Salt and Pepper Mill Sets-US-202608\n", encoding="utf-8")
        os.utime(f, (mtime, mtime))
    got = []
    removed = cb.prune(keep=1, dry=True, log=got.append)
    assert len(removed) == 1 and len(got) == 1 and got[0]["dry"] is True
    assert len(list(cb.BACKUP_DIR.glob("*.bak"))) == 2      # 文件没删


def test_default_prune_logger_writes_central_and_category_log(monkeypatch):
    """默认清理日志: 写 .webui/config-backups/prune.log + 对应类目的 pipeline-progress.log。"""
    _setup(monkeypatch)
    fake = _TMP / "proj"
    monkeypatch.setattr(cb, "PROJECT_ROOT", fake)
    monkeypatch.setattr(cb, "BACKUP_DIR", fake / ".webui" / "config-backups")
    cat = "Sim Cat A-US-202608"
    detail = {"name": f"{cat}-v1.bak".replace(" ", "-"), "cat": cat,
              "reason": "webui 保存前自动备份", "dir": "config-backups",
              "path": str(fake / "x"), "dry": False}
    cb.default_prune_logger()(detail)
    central = cb.BACKUP_DIR / "prune.log"
    percat = fake / "data" / cat / "17_备份与监控" / "pipeline-progress.log"
    assert central.is_file() and "已删除" in central.read_text(encoding="utf-8")
    assert percat.is_file() and "备份清理" in percat.read_text(encoding="utf-8")
    # dry 记录不落盘
    cb.default_prune_logger()({**detail, "dry": True})
    assert len(central.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_category_config_backup_dir_relative_path(monkeypatch):
    """2026-09-16 修: 调用方传**相对路径**时, 类目配置的备份也必须落在 `data/<CAT>/config-backups/`。

    原实现直接比 `src.parent.parent == PROJECT_ROOT/"data"` —— 相对路径下两侧不相等, 类目配置
    被误判成"非类目文件"落进集中兜底目录(实测: 批量补键时 12 个类目备份全进 .webui/config-backups/)。
    """
    root = _TMP / "relproj"
    shutil.rmtree(root, ignore_errors=True)
    cat_dir = root / "data" / "Sim Cat A-US-202608"
    cat_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cb, "PROJECT_ROOT", root)
    monkeypatch.setattr(cb, "BACKUP_DIR", root / ".webui" / "config-backups")
    monkeypatch.chdir(root)

    rel = Path("data") / "Sim Cat A-US-202608" / "config.yaml"
    rel.write_text('CAT: "Sim Cat A-US-202608"\nDRY_RUN: false\n', encoding="utf-8")
    bak = cb.make_backup(rel, reason="测试: 相对路径备份落点", changed_to='CAT: "x"\n')
    assert bak is not None
    # 返回路径沿用调用方传入的路径形态(相对入参 → 相对出参), 故两侧都 resolve 后比较
    assert bak.parent.resolve() == (cat_dir / "config-backups").resolve(), f"备份落点不对: {bak}"
    assert not (root / ".webui" / "config-backups").exists() or \
        not list((root / ".webui" / "config-backups").glob("*.bak")), "不该落到集中兜底目录"

    # 绝对路径(webui 走的路)行为不变
    ab = cb.make_backup(cat_dir / "config.yaml", reason="测试: 绝对路径备份落点",
                        changed_to='CAT: "y"\n')
    assert ab is not None and ab.parent.resolve() == (cat_dir / "config-backups").resolve()


def test_prune_error_in_log_does_not_break_cleanup(monkeypatch):
    import os
    _setup(monkeypatch)
    for v, mtime in ((1, 100.0), (2, 200.0)):
        f = cb.BACKUP_DIR / f"Salt-and-Pepper-Mill-Sets-US-202608-v{v}.bak"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (mtime, mtime))

    def _boom(_detail):
        raise RuntimeError("日志失败")

    removed = cb.prune(keep=1, log=_boom)
    assert len(removed) == 1        # 日志抛错不影响清理
