"""Phase 3 冒烟: 验证 LLM 工厂的纯函数(不依赖 crewAI)。

用法:
    python -m agent.scripts.smoke_llm [data/<CAT>/config.yaml]

不构造 LLM 实例(避免真网络/真密钥); 仅验证配置读取、模型名归一化、
参数组装与校验, 以及(若有 crewAI)是否可构造。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent._vendor import Config  # noqa: E402
from agent.crews.llm_factory import (  # noqa: E402
    build_llm,
    describe_llm,
    llm_kwargs,
    normalize_model,
    validate_config,
)


def main(config_path: str) -> int:
    config = Config.load(config_path)
    print(f"== LLM 描述: {describe_llm(config)}")
    print(f"== 模型归一化('deepseek-chat') -> '{normalize_model('deepseek-chat')}'")

    ok, missing = validate_config(config)
    print(f"== 配置校验: {'通过' if ok else '缺失 ' + str(missing)}")

    if ok:
        print(f"== 组装参数: {llm_kwargs(config)}")
        if importlib.util.find_spec("crewai") is not None:
            llm = build_llm(config)
            print(f"== build_llm 成功: {type(llm).__name__}")
        else:
            print("== crewai 未安装, 不调用 build_llm(需要先 pip install -r agent/requirements.txt)")
    else:
        print(f"== LLM 密钥/模型未配置, 跳过 build_llm(真实运行前请在 config 填写)")
    return 0


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "data/salt-pepper-shaker-sets/config.yaml"
    raise SystemExit(main(cfg))
