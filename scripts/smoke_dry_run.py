"""dry-run 全流程冒烟脚本。

用法: python scripts/smoke_dry_run.py [--config config.smoke.yaml]

以 DRY_RUN=true 跑通 0-19 全流程(不访问网络/不需要密钥),
并断言每步关键产物存在, 供逐步骤审查数据契约。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXPECTED = {
    0: ["<CAT>-bestseller-newrelease.json", "<CAT>-bestseller-newrelease-all.txt"],
    1: ["<CAT>-dual-details.jsonl"],
    2: ["filter-<CAT>-prompt.json"],
    3: ["<CAT>-dual-details-filtered.jsonl", "<CAT>-filtered-dual.txt"],
    4: ["<CAT>-keywords-candidates.txt", "<CAT>-keywords.txt", "<CAT>-keywords-entities.txt"],
    5: ["<CAT>-amazon-keyword-data.json"],
    6: ["<CAT>-amazon-monthly.json"],
    7: ["screen-<CAT>.py", "<CAT>-screen-results.json"],
    8: ["<CAT>-keyword-report.xlsx"],
    9: ["<CAT>-search-asins.json", "<CAT>-search-asins-all.txt"],
    10: [
        "<CAT>-results.jsonl",
        "<CAT>-父项变体明细.xlsx",
        "<CAT>-parents.txt",
        "<CAT>-variants.txt",
        "<CAT>-过滤审计.csv",
    ],
    11: ["11_查流量来源/合并-11_查流量来源.xlsx"],
    12: ["12_查竞品/<CAT>-submitted.json"],
    13: ["13_查竞品下载/合并-13_查竞品下载.xlsx"],
    14: ["14_流量拆分/流量拆分数据-需人工补充.xlsx"],
    15: ["<CAT>-初步类目分析报告.xlsx"],
    16: ["<CAT>-attribute-spec.json"],
    17: ["17_属性标注/<CAT>-属性标注-单遍原始.xlsx",
        "17_属性标注/唯一值统计.json"],
    18: ["18_标准化/<CAT>-属性标注-标准化交付.xlsx"],
    19: ["19_VOC报告/<CAT>-VOC报告.xlsx"],
    20: ["20_备份与监控/health-report.json"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.smoke.yaml")
    args = parser.parse_args()

    from core.config import Config
    from core.orchestrator import PipelineRunner

    config = Config.load(args.config)
    cat = config.cat or "smoke-test"
    runner = PipelineRunner(config, dry_run=True)
    code = runner.run(from_step=0)
    print(f"\n流水线退出码: {code} (0=正常 1=有警告 2=严重)")

    failed = []
    for num, names in sorted(EXPECTED.items()):
        for name in names:
            rel = name.replace("<CAT>", cat).replace("/", os.sep)
            # 定义层走解析器（自己的目录优先 → 共享沉淀层继承）—— 迁移后仍能找到
            try:
                from tools.attr_stability.catalog import resolve_definition_file
                path = str(resolve_definition_file(runner.paths.cat_root, rel))
            except Exception:                                    # noqa: BLE001 — 兜底原行为
                path = os.path.join(runner.paths.cat_root, rel)
            if not os.path.exists(path):
                failed.append(f"步骤{num}: {name}")
    if failed:
        print("缺失产物:")
        for f in failed:
            print("  -", f)
        print("冒烟测试失败")
        return 1
    print("全部关键产物存在, 冒烟测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
