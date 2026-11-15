"""回归测试: **临时覆盖不得写回类目配置**(2026-09-15 用户报障驱动)。

现象: 用户在看板 ⑤ 配置中心发现自己那个真实类目变成了 `… DRY`(DRY_RUN: true)。
根因: `POST /api/run` 的 `overrides`(前端「模式」= dry-run/真实 的单选)会被
`sync_category_config()` **写回 `data/<CAT>/config.yaml`** —— 一次性的运行模式变成永久配置,
而且这条路径**不生成备份**(与"保存前自动备份"不同), 用户毫无察觉。
实测: 一次 `{DRY_RUN: true}` 的测试运行把 `Cat Fountains-US-202608` 的 6 个历史备份(全是 false)
之外的真实文件永久改成了 true; 另一个类目的翻转则来自配置中心的一次保存。

修法: 有 overrides 时**不写回**, 只把 `PIPELINE_CONFIG_SOURCE` 指向类目配置(月份回退等
"该写回的键"照常写回); 无 overrides 时照旧同步(内容本来就一致)。
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Leak Cat-US-202609"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_override", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRunner:
    """记录 start() 入参, 不真的起进程。"""

    def __init__(self):
        self.started: list[dict] = []

    def active_for(self, cat):
        return False

    def snapshots(self):
        return []

    def start(self, cmd, label, base_config, effective_conf, cat, data_root, mode,
              from_step, overrides, extra_env=None):
        self.started.append({"cmd": cmd, "cat": cat, "env": dict(extra_env or {}),
                             "overrides": dict(overrides or {})})
        return {"active": True, "cat": cat, "pid": 4242, "label": label,
                "started_at": "2026-09-15 18:00:00", "effective_conf": str(effective_conf)}


@pytest.fixture
def server(ws_tmp, monkeypatch):
    s = _load_server()
    root = Path(ws_tmp)
    monkeypatch.setattr(s, "PROJECT_ROOT", root)
    monkeypatch.setattr(s, "RUN_DIR", root / ".webui" / "runs")
    s.RUN_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(s, "RING", s.RingLog())
    monkeypatch.setattr(s, "_lock_live_started", lambda cat, droot: (False, None, False))
    runner = _FakeRunner()
    monkeypatch.setattr(s, "RUNNER", runner)
    s._test_runner = runner
    # 类目配置(结构参照真实类目: 含 CAT/DATA_ROOT/DRY_RUN)。
    # ⚠️ DATA_ROOT 必须写**绝对路径**(指向本次临时工作区): `sync_category_config()`/
    # `category_config_path()` 是按**进程 CWD** 解析相对 DATA_ROOT 的, 写 "data" 会把测试
    # 的类目配置写到**真实项目**的 data/ 下(自测时踩到: 真的生成了 data/Leak Cat-…/config.yaml)。
    d = root / "data" / CAT
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{CAT}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n'
        f'DRY_RUN: false\nLLM_MODEL: deepseek-flash\n', encoding="utf-8")
    return s


def _cfg_path(ws_tmp) -> Path:
    return Path(ws_tmp) / "data" / CAT / "config.yaml"


def test_override_run_does_not_touch_category_config(server, ws_tmp):
    """带 `overrides={"DRY_RUN": true}` 运行: 类目配置**一字不改**, 也不生成备份。"""
    before = _cfg_path(ws_tmp).read_text(encoding="utf-8")
    h = server.Handler.__new__(server.Handler)
    out = h._api_run({"config_file": f"data/{CAT}/config.yaml", "mode": "resume",
                      "overrides": {"DRY_RUN": True}})
    assert out["ok"] is True and out["run"]["dry_run"] is True, "本次运行仍按 dry-run 跑"
    after = _cfg_path(ws_tmp).read_text(encoding="utf-8")
    assert after == before, "临时覆盖被写回了类目配置(就是那个 bug)"
    assert "DRY_RUN: false" in after
    assert not (Path(ws_tmp) / "data" / CAT / "config-backups").exists(), "不该悄悄生成备份"
    # 子进程仍以类目配置为"配置源"(月份回退等该写回的键照常写回), 但运行用的是临时快照
    started = server._test_runner.started[-1]
    assert started["env"]["PIPELINE_CONFIG_SOURCE"].endswith(f"data{os.sep}{CAT}{os.sep}config.yaml")
    assert "--dry-run" in started["cmd"], "临时覆盖要真实作用于本次运行"
    conf_arg = started["cmd"][started["cmd"].index("--config") + 1]
    assert conf_arg.endswith(".yaml") and ".webui" in conf_arg, "运行用临时快照配置"


def test_run_without_override_still_syncs_category_config(server, ws_tmp):
    """不带 overrides 的运行: 照旧把生效配置同步到类目配置(内容一致, 属正常路径)。"""
    before = _cfg_path(ws_tmp).read_text(encoding="utf-8")
    h = server.Handler.__new__(server.Handler)
    out = h._api_run({"config_file": f"data/{CAT}/config.yaml", "mode": "resume",
                      "overrides": {}})
    assert out["ok"] is True and out["run"]["dry_run"] is False
    after = _cfg_path(ws_tmp).read_text(encoding="utf-8")
    assert after == before, "同步不该改变内容(生效值 == 文件值)"
    started = server._test_runner.started[-1]
    assert "--dry-run" not in started["cmd"]
    assert started["env"]["PIPELINE_CONFIG_SOURCE"].endswith(f"data{os.sep}{CAT}{os.sep}config.yaml")
