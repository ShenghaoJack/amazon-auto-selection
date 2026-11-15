"""LLM 用量/费用监控冒烟脚本(Step 1 验收用)。

用法:
  python scripts/smoke_usage.py                      # 自动挑一个有密钥的类目配置
  python scripts/smoke_usage.py --config data/<CAT>/config.yaml
  python scripts/smoke_usage.py --mode builtin       # 强制离线价表, 不联网
  python scripts/smoke_usage.py --jsonl out.jsonl    # 顺带落一份明细, 检查文件格式
  python scripts/smoke_usage.py --no-vision          # 只跑文本通道

做三件事:
  1. 解析价格表并打印**来源**(实时/缓存/内置快照) —— 费用是照哪份价算的必须一眼可见;
  2. 真发两个文本请求(**同一长前缀发两次**) —— 第二次应命中前缀缓存, 用来实测
     "命中/未命中分开计价"这条口径;
  3. 可选发一个视觉请求, 验证 vision 通道也归到同一本账。

花费极小(约 3.5k tokens, 不到 0.01 元)。默认**不写任何文件**, 不污染类目数据目录。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 稳定的公网图片(DeepSeek 官方文档 CDN), 用于验证"图片 URL 直传"通道
DEFAULT_VISION_URL = "https://cdn.deepseek.com/official_account.jpg"

# 长前缀: 约 1.6k token, 第二次调用应命中前缀缓存
FILLER = ("The quick brown fox jumps over the lazy dog. "
          "Amazon category research pipeline step verification content. ") * 90


def _pick_config() -> str:
    """自动挑第一个配了 LLM_API_KEY 的类目配置。"""
    from core.category_config import list_category_configs
    from core.config import Config

    for path in list_category_configs("data"):
        try:
            if Config.load(path).get("LLM_API_KEY"):
                return str(path)
        except Exception:                        # noqa: BLE001
            continue
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="", help="类目 config.yaml(默认自动挑选)")
    ap.add_argument("--mode", default="", help="覆盖 LLM_PRICES_MODE: live/cache/builtin/manual")
    ap.add_argument("--jsonl", default="", help="把明细写到这里(默认不落盘)")
    ap.add_argument("--csv", default="", help="把步骤汇总写到这里(默认不落盘)")
    ap.add_argument("--vision-url", default=DEFAULT_VISION_URL)
    ap.add_argument("--no-vision", action="store_true", help="跳过视觉通道")
    args = ap.parse_args()

    from core.config import Config
    from core.llm import LLMClient
    from core.logger import setup_logger
    from core.usage import build_meter

    cfg_path = args.config or _pick_config()
    if not cfg_path or not os.path.exists(cfg_path):
        print("找不到可用的类目配置(需含 LLM_API_KEY)。请用 --config 显式指定。")
        return 2

    config = Config.load(cfg_path)
    if args.mode:
        config.set("LLM_PRICES_MODE", args.mode)
    log = setup_logger()
    print(f"配置: {cfg_path}")
    print(f"模型: {config.get('LLM_MODEL')} / 视觉 {config.get('VISION_MODEL')}")
    print(f"价格模式: {config.get('LLM_PRICES_MODE')}")

    meter = build_meter(config, None, log=log)
    meter.jsonl_path = args.jsonl or None
    meter.csv_path = args.csv or None
    if not meter.enabled:
        print("该配置是 DRY_RUN=true, 冒烟需要真实调用; 请换一个真实类目配置。")
        return 2

    meter.begin_step(0)
    client = LLMClient(config, meter=meter)
    model = str(config.get("LLM_MODEL", ""))
    print(f"\nLLM 价格来源: {meter.prices.source}\n")

    print("① 文本通道 第 1 次(冷启动, 输入应全部未命中) ...")
    client.chat(FILLER + "\n只回复: OK", temperature=0.0, max_tokens=8)
    print("   usage =", meter.totals())

    print("② 文本通道 第 2 次(同前缀, 应有命中) ...")
    client.chat(FILLER + "\n只回复: OK", temperature=0.0, max_tokens=8)
    print("   usage =", meter.totals())

    if not args.no_vision and config.get("VISION_API_KEY"):
        print("③ 视觉通道(图片 URL 直传) ...")
        try:
            client.vision("这张图里有什么? 用 JSON 回: {\"desc\": \"...\"}",
                          [args.vision_url], max_tokens=64)
            print("   usage =", meter.totals())
        except Exception as exc:                 # noqa: BLE001 — 视觉失败不影响文本结论
            print(f"   视觉通道失败(不影响文本结论): {type(exc).__name__}: {exc}")
    elif not config.get("VISION_API_KEY"):
        print("③ 视觉通道: VISION_API_KEY 未配置, 跳过")

    text = meter.finish_step(0, "SMOKE")
    print("\n" + "=" * 68)
    print(text)
    print("-" * 68)
    print(meter.as_text())
    print("=" * 68)

    if args.mode == "builtin":
        print("\n[离线模式] 若把网络断开再跑一次, 来源应变成「磁盘缓存」或「内置快照」,")
        print("           且脚本**不报错** —— 这就是抓取失败时的兜底行为。")
    if args.jsonl:
        print(f"\n明细已写入: {args.jsonl}(首行 meta, 之后每次调用一行)")
    if args.csv:
        print(f"步骤汇总已写入: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
