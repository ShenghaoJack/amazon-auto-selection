# 单元测试: CLI --config 参数解析(2026-09-13 修复)
# 背景: 子解析器用 default=None 注册 --config 时会把全局 --config **覆盖成 None**
#       (argparse 子解析器默认值写回同一 namespace 的经典坑) —— 于是
#       `main.py --config X step 4` 被静默当成 `--config config.yaml`(跑根目录占位模板),
#       实测排查"配置里的键没生效"时会误判。现子解析器改用 default=argparse.SUPPRESS。
import main


def test_config_before_subcommand_kept():
    a = main.build_parser().parse_args(["--config", "data/A/config.yaml", "step", "4"])
    assert a.config == "data/A/config.yaml"
    assert a.func == "step"


def test_config_after_subcommand_kept():
    a = main.build_parser().parse_args(["step", "4", "--config", "data/A/config.yaml"])
    assert a.config == "data/A/config.yaml"


def test_config_default_when_absent():
    assert main.build_parser().parse_args(["step", "4"]).config == "config.yaml"
    assert main.build_parser().parse_args(["run"]).config == "config.yaml"


def test_config_before_subcommand_for_run():
    a = main.build_parser().parse_args(["--config", "x.yaml", "run", "--from", "3"])
    assert a.config == "x.yaml" and a.from_step == 3
