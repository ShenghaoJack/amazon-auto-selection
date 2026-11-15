# 单元测试: 分端部署交接层 `scripts/sync_artifacts.py`(2026-10-08)
#
# 背景见 SPLIT_DEPLOYMENT_GUIDE.md: 把"爬数据的步骤"放本地、"LLM/数据处理/MCP 步骤"放服务器时,
# 20 步之间**只靠 `data/<CAT>/` 里的文件衔接** —— 所以必须能回答两个问题:
#   ① 我这端看到的产物, 是不是产出端**完整**写出来的那份? (半同步的大文件绝不能被当成"跑好了")
#   ② 哪些文件在 manifest 之后被动过(可能是"两端同时在写"的征兆)?
# 本文件钉住: 指纹写入/校验(缺文件、同体积被改、mtime 变化不算冲突)、extra 提示、
# 机器本地配置的优先级、push/pull 只传选定步骤且不删目标端文件、以及退出码语义。
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAT = "Demo-Cat-202608"


def _load():
    spec = importlib.util.spec_from_file_location("_sync_artifacts", ROOT / "scripts" / "sync_artifacts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sa = _load()


# --------------------------------------------------------------------------- 夹具

def _mk_cat(tmp: Path, *, steps=(0, 8, 20)) -> tuple[Path, dict]:
    """造一个最小类目目录 + 声明表: data/<CAT>/{平铺产物, 08_关键词报告/, 17_备份与监控/}。"""
    cat_root = tmp / "data" / CAT
    (cat_root / "17_备份与监控").mkdir(parents=True, exist_ok=True)
    (cat_root / "08_关键词报告").mkdir(parents=True, exist_ok=True)
    (cat_root / "00_榜单收集").mkdir(parents=True, exist_ok=True)
    (cat_root / "00_榜单收集" / "榜单.xlsx").write_bytes(b"bestsellers" * 10)
    (cat_root / "08_关键词报告" / "关键词报告.xlsx").write_bytes(b"report" * 100)
    (cat_root / "08_关键词报告" / "附注.txt").write_text("notes\n", encoding="utf-8")
    (cat_root / "07_筛选" / "占位").parent.mkdir(parents=True, exist_ok=True)
    (cat_root / "17_备份与监控" / "step-status.txt").write_text(
        "0|DONE|2026-10-08 10:00:00|2026-10-08 10:05:00||0|0|0\n"
        "8|DONE|2026-10-08 11:00:00|2026-10-08 11:02:00||0|1|0\n"
        "9|RUNNING|2026-10-08 11:30:00|||0|0|0\n", encoding="utf-8")
    decls = {0: ["00_榜单收集/榜单.xlsx"], 8: ["08_关键词报告/关键词报告.xlsx",
                                              "08_关键词报告/附注.txt"],
             20: ["17_备份与监控/step-status.txt"]}
    return cat_root, decls


def _cfg(tmp: Path, cat_root: Path) -> Path:
    """写一个最小 config.yaml(只给 Config.load + Paths 需要的键)。

    注意路径用 **POSIX 斜杠**: YAML 双引号里的 `\\d` 会被当转义序列(`unknown escape character 'd'`)。
    """
    p = cat_root / "config.yaml"
    p.write_text(f'CAT: "{CAT}"\nDATA_ROOT: "{tmp.as_posix()}/data"\nDRY_RUN: true\n', encoding="utf-8")
    return p


# --------------------------------------------------------------------------- 指纹

def test_file_sig_full_and_partial_modes(tmp_path, monkeypatch):
    small = tmp_path / "s.bin"
    small.write_bytes(b"x" * 1000)
    sig = sa.file_sig(small)
    assert sig["hash_mode"] == "full" and sig["size"] == 1000 and len(sig["sha256"]) == 64

    big = tmp_path / "b.bin"
    big.write_bytes(b"x" * 5000)
    sig2 = sa.file_sig(big, full_max=100)          # 阈值调小 → 走部分哈希
    assert sig2["hash_mode"] == "partial" and "-" in sig2["sha256"] and sig2["size"] == 5000
    # 内容变了(哪怕同体积) → 部分哈希也要变
    big.write_bytes(b"y" * 5000)
    assert sa.file_sig(big, full_max=100)["sha256"] != sig2["sha256"]


def test_step_files_only_declared_for_flat_steps_plus_realtime_dirs(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    got = {str(p.relative_to(cat_root)).replace("\\", "/") for p in sa.step_files(cat_root, 8, decls)}
    assert got == {"08_关键词报告/关键词报告.xlsx", "08_关键词报告/附注.txt"}
    # 平铺步骤(0)只认声明产物 —— 不能把别步的文件也扫进来
    assert {str(p.relative_to(cat_root)) for p in sa.step_files(cat_root, 0, decls)} == {
        os.path.join("00_榜单收集", "榜单.xlsx")}


def test_step_files_excludes_backups_and_temp(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    d = cat_root / "08_关键词报告"
    (d / "config-backups").mkdir()
    (d / "config-backups" / "v1.bak").write_text("backup", encoding="utf-8")
    (d / "半份.xlsx.tmp").write_text("half", encoding="utf-8")
    rels = {str(p.relative_to(cat_root)).replace("\\", "/") for p in sa.step_files(cat_root, 8, decls)}
    assert not any("config-backups" in r or r.endswith(".tmp") for r in rels)


# --------------------------------------------------------------------------- write

def test_write_manifest_records_role_machine_and_step_status(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    out = sa.write_manifests(cat_root, [8], role="server", cat=CAT, decls=decls)
    man = out[0]
    assert man["schema"] == sa.SCHEMA and man["step"] == 8 and man["role"] == "server"
    assert man["machine"] and man["file_count"] == 2 and man["total_size"] > 0
    assert man["state"] == "DONE" and man["finished_at"] == "2026-10-08 11:02:00" and man["fails"] == 1
    assert sa.manifest_path(cat_root, 8).is_file()
    idx = json.loads((sa.manifest_dir(cat_root) / "index.json").read_text(encoding="utf-8"))
    assert idx["steps"]["8"]["role"] == "server" and idx["steps"]["8"]["state"] == "DONE"


def test_write_manifest_flags_declared_but_missing(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    decls[8].append("08_关键词报告/还没产出.xlsx")
    man = sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)[0]
    assert man["missing_declared"] == ["08_关键词报告/还没产出.xlsx"]


def test_write_is_idempotent_and_leaves_no_tmp(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    a = sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)[0]
    b = sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)[0]
    assert [(f["rel"], f["sha256"]) for f in a["files"]] == [(f["rel"], f["sha256"]) for f in b["files"]]
    assert not list(sa.manifest_dir(cat_root).glob("*.tmp"))


# --------------------------------------------------------------------------- verify

def test_verify_ok_roundtrip(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [0, 8], role="local", cat=CAT, decls=decls)
    res = {r["step"]: r for r in sa.verify_many(cat_root, [0, 8], decls=decls)}
    assert res[0]["status"] == "ok" and res[8]["status"] == "ok"
    assert not res[8]["missing"] and not res[8]["changed"]


def test_verify_detects_same_size_content_change(tmp_path):
    """同体积改内容: 只比 size 会漏, 必须靠 hash 抓住。"""
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)
    f = cat_root / "08_关键词报告" / "关键词报告.xlsx"
    f.write_bytes(b"REPORT" * 100)                 # 同样 600 字节
    r = sa.verify_step(cat_root, 8, decls=decls)
    assert r["status"] == "changed" and any("内容已变" in x for x in r["changed"])
    # 只比体积时看不出来(fast 模式的已知代价)
    assert sa.verify_step(cat_root, 8, check_hash=False, decls=decls)["status"] == "ok"


def test_verify_detects_missing_file(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)
    (cat_root / "08_关键词报告" / "附注.txt").unlink()
    r = sa.verify_step(cat_root, 8, decls=decls)
    assert r["status"] == "missing" and r["missing"] == ["08_关键词报告/附注.txt"]


def test_verify_tolerates_mtime_only_change(tmp_path):
    """网盘/SMB 复制必然改 mtime —— 内容一致就不能算冲突(否则分端没法用)。"""
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)
    f = cat_root / "08_关键词报告" / "附注.txt"
    os.utime(f, (1_700_000_000, 1_700_000_000))
    r = sa.verify_step(cat_root, 8, decls=decls)
    assert r["status"] == "ok" and r["touched"] == ["08_关键词报告/附注.txt"]


def test_verify_reports_extra_as_warning_not_error(tmp_path):
    """manifest 里没记录的文件 = 可能是另一端刚写入(或两端同时在写) → 默认警告, --strict 才算错。"""
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)
    # 声明产物是"扫描集合"的来源: 新出现的声明文件(manifest 里没有)才算 extra
    (cat_root / "08_关键词报告" / "另一端新增.txt").write_text("x", encoding="utf-8")
    decls[8].append("08_关键词报告/另一端新增.txt")
    r = sa.verify_step(cat_root, 8, decls=decls)
    assert r["status"] == "extra" and r["extra"] == ["08_关键词报告/另一端新增.txt"]
    assert r["changed"] == [] and r["missing"] == []


def test_verify_extra_detected_in_realtime_step_dir(tmp_path):
    """有独立实时子目录的步骤(如 20=备份与体检): 目录里多出的文件也算 extra(不必进声明表)。"""
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [20], role="local", cat=CAT, decls=decls)
    (cat_root / "17_备份与监控" / "新日志.txt").write_text("hi", encoding="utf-8")
    r = sa.verify_step(cat_root, 20, decls=decls)
    assert r["status"] == "extra" and "17_备份与监控/新日志.txt" in r["extra"]


def test_verify_missing_key_maps_to_live_not_error(tmp_path):
    """步骤20 的目录里含 manifest 自己 —— 必须排除, 否则每次 write 后 verify(19) 必报"已变"。"""
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [20], role="local", cat=CAT, decls=decls)
    again = sa.verify_step(cat_root, 20, decls=decls)
    assert again["status"] == "ok", f"manifest 目录不能算进指纹: {again}"
    assert not again["changed"] and not again["extra"]


def test_volatile_status_files_do_not_fail_verify(tmp_path):
    """追加型状态文件(step-status.txt / 实时日志)只同步不判定 —— 否则每次交接都误报不一致。"""
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [20], role="local", cat=CAT, decls=decls)
    st = cat_root / "17_备份与监控" / "step-status.txt"
    st.write_text(st.read_text(encoding="utf-8") + "20|DONE|2026-10-08 12:00:00|2026-10-08 12:01:00||0|0|0\n",
                  encoding="utf-8")
    r = sa.verify_step(cat_root, 20, decls=decls)
    assert r["status"] == "ok" and r["changed"] == []
    assert any("step-status.txt" in x for x in r["live"])


def test_verify_without_manifest_is_reported(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    r = sa.verify_step(cat_root, 8, decls=decls)
    assert r["status"] == "no_manifest" and sa.steps_to_check(cat_root) == []


# --------------------------------------------------------------------------- 机器本地配置

def test_role_and_target_resolution_priority(tmp_path, monkeypatch):
    monkeypatch.setenv(sa.ROLE_ENV, "server")
    monkeypatch.setenv(sa.TARGET_ENV, r"\\env\share")
    (tmp_path / sa.LOCAL_CFG_NAME).write_text("role: local\ntarget: 'C:/from-file'\n", encoding="utf-8")
    # 环境变量 > 本地文件
    assert sa.resolve_role(root=tmp_path) == "server"
    assert sa.resolve_target(root=tmp_path) == r"\\env\share"
    # 命令行 > 环境变量
    assert sa.resolve_role("local", root=tmp_path) == "local"
    assert sa.resolve_target("D:/cli", root=tmp_path) == "D:/cli"
    monkeypatch.delenv(sa.ROLE_ENV)
    monkeypatch.delenv(sa.TARGET_ENV)
    # 落到本地文件
    assert sa.resolve_role(root=tmp_path) == "local"
    assert sa.resolve_target(root=tmp_path) == "C:/from-file"
    # 都没有 → 默认
    assert sa.resolve_role(root=tmp_path / "nope") == "local"
    assert sa.resolve_target(root=tmp_path / "nope") == ""


def test_local_config_tolerates_broken_yaml(tmp_path):
    (tmp_path / sa.LOCAL_CFG_NAME).write_text("role: [unclosed\n", encoding="utf-8")
    assert sa.local_config(tmp_path) == {}
    assert sa.resolve_role(root=tmp_path) == "local"


# --------------------------------------------------------------------------- push / pull

def test_transfer_push_only_selected_steps_and_never_deletes(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)
    target = tmp_path / "nas" / CAT
    (target).mkdir(parents=True)
    keeper = target / "别的类目的东西.txt"
    keeper.write_text("keep me", encoding="utf-8")
    out = sa.transfer(cat_root, target, direction="push", steps=[8], decls=decls)
    assert out["files"] >= 3 and out["bytes"] > 0
    assert (target / "08_关键词报告" / "关键词报告.xlsx").is_file()
    assert (target / "17_备份与监控" / "step-status.txt").is_file(), "状态文件必须一起过去"
    assert (target / "17_备份与监控" / sa.MANIFEST_SUBDIR / "step08.json").is_file()
    assert keeper.read_text(encoding="utf-8") == "keep me", "绝不能删除目标端已有文件"


def test_transfer_pull_roundtrip(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    sa.write_manifests(cat_root, [8], role="local", cat=CAT, decls=decls)
    target = tmp_path / "nas"
    sa.transfer(cat_root, target, direction="push", steps=[8], decls=decls)

    # 服务器端: 只有 target, 拉到一个空类目目录
    server_cat = tmp_path / "server" / "data" / CAT
    server_cat.mkdir(parents=True)
    out = sa.transfer(server_cat, target, direction="pull", steps=[8], decls=decls)
    assert out["files"] >= 3
    assert (server_cat / "08_关键词报告" / "附注.txt").read_text(encoding="utf-8") == "notes\n"
    # 拉过来后校验一致(manifest 也跟着过来了)
    assert sa.verify_step(server_cat, 8, decls=decls)["status"] == "ok"


def test_transfer_dry_run_writes_nothing(tmp_path):
    cat_root, decls = _mk_cat(tmp_path)
    target = tmp_path / "nas"
    out = sa.transfer(cat_root, target, direction="push", steps=[8], decls=decls, dry_run=True)
    assert out["dry_run"] is True and out["files"] >= 2
    assert not target.exists()


# --------------------------------------------------------------------------- CLI

def test_cli_write_verify_status_roundtrip(tmp_path, capsys):
    cat_root, decls = _mk_cat(tmp_path)
    cfg = _cfg(tmp_path, cat_root)
    # 注入声明表, 避免测试依赖真实步骤类
    import unittest.mock as mock

    with mock.patch.object(sa, "step_declarations", return_value=decls):
        assert sa.main(["write", "--config", str(cfg), "--steps", "8", "--role", "server"]) == 0
        out = capsys.readouterr().out
        assert "写入 manifest" in out and "server" in out
        assert sa.main(["verify", "--config", str(cfg)]) == 0
        assert "校验通过" in capsys.readouterr().out
        assert sa.main(["status", "--config", str(cfg)]) == 0
        assert "类目 Demo-Cat-202608" in capsys.readouterr().out
        # 破坏一个产物 → verify 退出码 2
        (cat_root / "08_关键词报告" / "关键词报告.xlsx").write_bytes(b"tampered")
        assert sa.main(["verify", "--config", str(cfg), "--json"]) == 2


def test_cli_verify_without_manifest_returns_2(tmp_path, capsys):
    cat_root, _ = _mk_cat(tmp_path)
    cfg = _cfg(tmp_path, cat_root)
    assert sa.main(["verify", "--config", str(cfg)]) == 2
    assert "还没有任何 manifest" in capsys.readouterr().out


def test_cli_push_without_target_is_usage_error(tmp_path, capsys, monkeypatch):
    cat_root, _ = _mk_cat(tmp_path)
    cfg = _cfg(tmp_path, cat_root)
    monkeypatch.delenv(sa.TARGET_ENV, raising=False)
    monkeypatch.chdir(tmp_path)          # 让 local_config 读不到仓库根的 sync.local.yaml
    assert sa.main(["push", "--config", str(cfg)]) == 1
    assert "未配置目标目录" in capsys.readouterr().out


def test_cli_status_shows_local_role(tmp_path, capsys, monkeypatch):
    cat_root, decls = _mk_cat(tmp_path)
    cfg = _cfg(tmp_path, cat_root)
    monkeypatch.setenv(sa.ROLE_ENV, "local")
    assert sa.main(["status", "--config", str(cfg)]) == 0
    assert "本机角色 local" in capsys.readouterr().out
