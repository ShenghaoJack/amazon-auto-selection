# -*- coding: utf-8 -*-
"""第二遍过滤(FR-10)结果复核脚本(手动运行; 步骤10 自动抽查见 REVIEW_AUTO)。

原理: 用同一份过滤提示词对 results.jsonl 中 ok 记录**重新判定一遍**(LLM),
与原始 保留/剔除 结果逐条对比, 找出"两次判定不一致"的记录——
这些是误判风险最高、最需要人工复核的对象。

同时生成含**真实判定原因 + 商品链接**的复核审计 CSV(Excel 可直接打开)。

抽查条数优先级: 命令行 --limit > 配置文件 REVIEW_LIMIT(默认 0=全量)。

用法:
  python -X utf8 scripts/review_filter_audit.py [--config config.yaml] [--limit N] [--seed 42]

退出码: 0=一致率>=90%(正常)  1=不一致较多(建议人工复核)  2=出错
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.io_utils import read_json, read_jsonl
from core.paths import Paths
from steps._filtering import review_filter_results
from steps.base import StepContext


def main() -> int:
    ap = argparse.ArgumentParser(description="FR-10 第二遍过滤结果复核")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--limit", type=int, default=None, help="只复核 N 条(覆盖配置 REVIEW_LIMIT; 0=全量)")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    # 抽查条数: 命令行 --limit 优先, 否则读配置 REVIEW_LIMIT(默认 0=全量)
    limit = args.limit if args.limit is not None else int(cfg.get("REVIEW_LIMIT", 0) or 0)
    cat = cfg.cat
    data_root = str(cfg.get("DATA_ROOT", "data") or "data")
    results_path = os.path.join(data_root, cat, f"{cat}-results.jsonl")
    prompt_path = os.path.join(data_root, cat, f"filter-{cat}-prompt.json")
    if not (os.path.exists(results_path) and os.path.exists(prompt_path)):
        print(
            "缺少复核输入: "
            f"results.jsonl={os.path.exists(results_path)} "
            f"filter-prompt={os.path.exists(prompt_path)}"
        )
        return 2

    filter_prompt = read_json(prompt_path)
    records = [r for r in read_jsonl(results_path) if r.get("ok")]
    out_path = os.path.join(data_root, cat, f"{cat}-复核-过滤审计.csv")
    ctx = StepContext(cfg, Paths(cfg))

    limit_src = f"命令行 --limit {limit}" if args.limit is not None else (
        f"配置 REVIEW_LIMIT={limit}" if limit > 0 else "配置 REVIEW_LIMIT=0(全量)"
    )
    print(f"复核对象: CAT={cat}  ok记录={len(records)}  (抽查条数来源: {limit_src})")
    print("正在用 LLM 重新判定(与原始结果对比, 找出不一致项)...")
    stats = review_filter_results(
        ctx.llm(), cfg.market, records, filter_prompt,
        limit=limit, seed=args.seed, out_path=out_path,
    )

    print(
        f"\n复核完成: 共 {stats['total']} 条, 两次判定一致 {stats['agree']} 条, "
        f"不一致 {stats['disagree']} 条"
    )
    print(f"一致率: {stats['rate']:.1%}")
    print(f"复核审计已写入: {stats['out_path']}")
    disagree = stats["disagree_rows"]
    if disagree:
        print("\n--- 不一致项(优先人工复核) ---")
        for r in disagree[:20]:
            print(
                f"  {r['来源标识']}  {r['标题'][:40]}  "
                f"原={r['原判定']} -> 复核={r['复核判定']}  {r['原因'][:30]}"
            )
        if len(disagree) > 20:
            print(f"  ... 其余 {len(disagree) - 20} 条见 CSV")
    drop_reasons = {}
    for r in stats["rows"]:
        if r["复核判定"] == "剔除":
            drop_reasons[r["原因"]] = drop_reasons.get(r["原因"], 0) + 1
    if drop_reasons:
        print("\n--- 剔除原因分布(复核判定) ---")
        for reason, n in sorted(drop_reasons.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {reason}: {n} 条")
    return 0 if stats["rate"] >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
