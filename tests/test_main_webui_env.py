# 回归测试: web 启动方式(run/step)不再因单实例锁引用未定义 cat 而崩溃
# 背景: 方案A 单实例锁上线时, cat/droot 只在"非 webui(CLI)"分支赋值;
#       web 启动(PIPELINE_CONFIG_SOURCE 已注入 → webui_run=True)直接跳到锁判断
#       `if func in ("run","step") and cat:` → UnboundLocalError → 子进程启动即崩,
#       表现 = web 点运行只写 run-<ts>-config.yaml 快照、无任何运行日志。
# 修复(main.py): cat/droot 定义提前到分支前, web/CLI 两条路径都可用。
import os

import main as main_mod
from core.orchestrator import PipelineRunner


def _cfg(tmp_path, cat: str) -> str:
    data_root = tmp_path / "droot"
    path = tmp_path / "config.yaml"
    path.write_text(
        f"CAT: {cat}\nDATA_ROOT: {str(data_root)}\nDRY_RUN: true\nMARKET: amazon.com\n",
        encoding="utf-8",
    )
    return str(path)


def _stub_run(monkeypatch):
    """打桩 run/run_single: 只验证启动段(配置加载+锁), 不真正执行流水线。"""
    def _run(self, from_step: int = 0) -> int:
        return 0

    monkeypatch.setattr(PipelineRunner, "run", _run)
    monkeypatch.setattr(PipelineRunner, "run_single", _run)


def test_webui_env_run_startup_no_crash(tmp_path, monkeypatch):
    """web 注入 PIPELINE_CONFIG_SOURCE 时 run 启动段不抛 UnboundLocalError。"""
    monkeypatch.setenv("PIPELINE_CONFIG_SOURCE", "injected-by-server")
    _stub_run(monkeypatch)
    rc = main_mod.main(["run", "--config", _cfg(tmp_path, "webui-probe")])
    assert rc == 0


def test_webui_env_step_startup_no_crash(tmp_path, monkeypatch):
    """web 方式单步执行启动段同样不崩溃。"""
    monkeypatch.setenv("PIPELINE_CONFIG_SOURCE", "injected-by-server")
    _stub_run(monkeypatch)
    rc = main_mod.main(["step", "10", "--config", _cfg(tmp_path, "webui-probe")])
    assert rc == 0


def test_cli_run_startup_still_ok(tmp_path, monkeypatch):
    """CLI(无注入 env)路径不受影响: 仍走 percat 切换 + 锁。"""
    monkeypatch.delenv("PIPELINE_CONFIG_SOURCE", raising=False)
    _stub_run(monkeypatch)
    rc = main_mod.main(["run", "--config", _cfg(tmp_path, "cli-probe")])
    assert rc == 0
    # CLI 路径应把种子配置同步为 data/<CAT>/config.yaml(percat 真源)
    percat = tmp_path / "droot" / "cli-probe" / "config.yaml"
    assert percat.exists()
