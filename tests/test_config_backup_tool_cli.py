# 单元测试: scripts/config_backup_tool.py 的 CLI 行为(2026-09-13 修复)
# ① `prune --keep N` 原来只用于打印提示、没传给 prune() → 实际不生效(按默认 10 算);
# ② `restore <文件名>` 原来只在 .webui/config-backups/ 里找 → 备份主位置已改为
#    data/<CAT>/config-backups/, 于是"list 里看得见、restore 却说不存在";
# ③ `list` 增加"版本缺号"巡检(改过 CAT / 被 prune 清理都会造成缺号)。
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.config_backup as cb  # noqa: E402

_TMP = Path(__file__).resolve().parent / "_cb_cli_tmp"


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "cfg_backup_tool", ROOT / "scripts" / "config_backup_tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool(monkeypatch):
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    proj = _TMP / "proj"
    catdir = proj / "data" / "Sim Cat A-US-202608" / "config-backups"
    catdir.mkdir(parents=True)
    monkeypatch.setattr(cb, "PROJECT_ROOT", proj)
    monkeypatch.setattr(cb, "BACKUP_DIR", proj / ".webui" / "config-backups")
    for v, mtime in ((1, 100.0), (2, 200.0), (3, 300.0)):
        f = catdir / f"Sim-Cat-A-US-202608-v{v}.bak"
        f.write_text(f"# CAT: Sim Cat A-US-202608\n# 备份原因: 测试\nbody{v}\n", encoding="utf-8")
        os.utime(f, (mtime, mtime))
    mod = _load_tool()
    monkeypatch.setattr(mod, "prune", cb.prune)          # 让工具用到被 patch 过的根路径
    return mod, catdir


def _run(mod, monkeypatch, argv: list[str]) -> int:
    """工具的 main() 读 sys.argv —— 测试里替换掉再调用。"""
    monkeypatch.setattr(sys, "argv", ["config_backup_tool.py", *argv])
    return mod.main()


def test_prune_keep_flag_actually_applies(tool, monkeypatch, capsys):
    mod, catdir = tool
    assert _run(mod, monkeypatch, ["prune", "--keep", "1"]) == 0
    left = sorted(x.name for x in catdir.glob("*.bak"))
    assert left == ["Sim-Cat-A-US-202608-v3.bak"]        # 只留最新 1 份(修复前会留 3 份)
    out = capsys.readouterr().out
    assert "保留最近 1 份" in out and "Sim-Cat-A-US-202608-v1.bak" in out


def test_prune_dry_run_keeps_all_files(tool, monkeypatch, capsys):
    mod, catdir = tool
    assert _run(mod, monkeypatch, ["prune", "--keep", "1", "--dry-run"]) == 0
    assert len(list(catdir.glob("*.bak"))) == 3
    assert "将删除 2 份" in capsys.readouterr().out


def test_prune_writes_cleanup_log(tool, monkeypatch, capsys):
    mod, catdir = tool
    _run(mod, monkeypatch, ["prune", "--keep", "2"])
    capsys.readouterr()
    central = cb.prune_log_path()
    assert central.is_file() and "备份清理" in central.read_text(encoding="utf-8")


def test_restore_finds_backup_in_category_dir(tool, monkeypatch, capsys):
    mod, catdir = tool
    dst = catdir.parent / "config.yaml"
    dst.write_text('CAT: "Sim Cat A-US-202608"\nTRAFFIC_MONTH: "2026-09"\n', encoding="utf-8")
    assert _run(mod, monkeypatch, ["restore", "Sim-Cat-A-US-202608-v1.bak", "-y"]) == 0
    text = dst.read_text(encoding="utf-8")
    assert "body1" in text                                # 恢复了 v1 的内容
    out = capsys.readouterr().out
    assert "已恢复" in out


def test_list_reports_missing_versions(tool, monkeypatch, capsys):
    mod, catdir = tool
    (catdir / "Sim-Cat-A-US-202608-v2.bak").unlink()      # 造一个中间缺号: v1、v3 在, v2 没了
    assert _run(mod, monkeypatch, ["list"]) == 0
    out = capsys.readouterr().out
    assert "版本缺号" in out and "缺 v2" in out
