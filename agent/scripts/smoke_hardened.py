"""Phase 6 冒烟: 验证硬化 LLM(看门狗/非思考/JSON/mock)。

用法:
    python -m agent.scripts.smoke_hardened [data/<CAT>/config.yaml]

- 纯函数: messages_to_prompt / build_llm_payload / extract_json 直接验证。
- HardenedLLM: 用 dry-run 配置走内置 mock(无网络、无 key), 验证 chat/chat_json。
- build_hardened_llm: 仅当 crewAI 已装才实例化(否则跳过并说明)。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent._vendor import Config  # noqa: E402
from agent.crews.hardened_llm import (  # noqa: E402
    HardenedLLM,
    build_hardened_llm,
    build_llm_payload,
    describe_hardened,
    extract_json,
    messages_to_prompt,
)


def main(config_path: str) -> int:
    config = Config.load(config_path)
    print(f"== 描述: {describe_hardened(config)}")

    print("\n[messages_to_prompt]")
    msgs = [{"role": "user", "content": "分析该品类"}, {"role": "assistant", "content": "好的"}]
    print("   ", messages_to_prompt(msgs).replace("\n", " | "))
    print("   str 原样:", messages_to_prompt("直接字符串"))

    print("\n[build_llm_payload: 非思考+JSON]")
    p = build_llm_payload("deepseek-chat", "hi", 0.2, 4000, json_mode=True, no_think=True)
    print("   ", p)

    print("\n[extract_json 容错]")
    print("   ", extract_json('```json\n{"a": 1}\n```'))

    print("\n[HardenedLLM(mock, dry-run)]")
    llm = HardenedLLM(config)
    print("   chat: ", llm.chat("你好"))
    print("   chat_json: ", llm.chat_json("提取属性"))

    if importlib.util.find_spec("crewai") is not None:
        adapt = build_hardened_llm(config)
        print(f"\n[build_hardened_llm] 成功: {type(adapt).__name__}")
    else:
        print("\n[build_hardened_llm] crewai 未安装, 跳过(需先 pip install -r agent/requirements.txt)")
    return 0


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "data/salt-pepper-shaker-sets/config.yaml"
    raise SystemExit(main(cfg))
