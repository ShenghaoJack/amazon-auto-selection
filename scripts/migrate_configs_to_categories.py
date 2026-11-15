# -*- coding: utf-8 -*-
"""迁移现有根目录配置到"类目配置"模型(2026-09): 每个真实类目 -> data/<CAT>/config.yaml。

- 读取项目根 config*.yaml, 取 CAT; 占位/缺 CAT 的跳过(模板/示例/smoke 由用户自定);
- 目标 data/<CAT>/config.yaml 不存在则复制(已存在**不覆盖**——该类目配置已是真源);
- 原根文件移入 .webui/migrated-configs/<时间戳>/ 保留留档, 根目录不再平铺;
- 最后确保根目录有一份模板 config.yaml(CAT=占位, 供"新建类目/CLI 默认"用)。

用法: python scripts/migrate_configs_to_categories.py [--dry-run]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))  # noqa: PGH003

from core.category_config import is_placeholder_cat  # noqa: E402


def _cat_of(p: Path) -> str:
    import yaml  # noqa: PLC0415
    try:
        return str((yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}).get("CAT", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移根目录配置到 data/<CAT>/config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    migrated, skipped, existed, errors = [], [], [], []
    for p in sorted(ROOT.glob("config*.yaml")):
        if p.name == "config.example.yaml":   # 模板示例, 不迁移
            skipped.append((p.name, "模板示例(config.example)"))
            continue
        cat = _cat_of(p)
        if not cat or is_placeholder_cat(cat):
            skipped.append((p.name, "缺 CAT 或占位模板"))
            continue
        target = ROOT / "data" / cat / "config.yaml"
        if target.is_file():
            existed.append((p.name, target.name))
        else:
            if not args.dry_run:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
                except OSError as exc:
                    errors.append((p.name, str(exc)))
                    continue
            migrated.append((p.name, str(target)))
    print(f"迁移 {len(migrated)} 个, 目标已存在 {len(existed)} 个, 跳过 {len(skipped)} 个, 错误 {len(errors)} 个")
    for a, b in migrated:
        print(f"  迁移: {a} -> {b}")
    for a, b in existed:
        print(f"  已存在(不覆盖): {a} -> data/.../{b}")
    for a, why in skipped:
        print(f"  跳过: {a} ({why})")
    for a, why in errors:
        print(f"  错误: {a} ({why})")
    if args.dry_run:
        print("(dry-run, 未执行)")
        return 0
    # 移动原文件到 .webui/migrated-configs/<ts>/
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    keepdir = ROOT / ".webui" / "migrated-configs" / ts
    for p in sorted(ROOT.glob("config*.yaml")):
        cat = _cat_of(p)
        if not cat or is_placeholder_cat(cat):
            continue
        keepdir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(p), str(keepdir / p.name))
            print(f"  原文件移入: {p.name} -> {keepdir.name}/")
        except OSError as exc:
            print(f"  移动失败(保留原位): {p.name} ({exc})")
    # 根目录模板(占位 CAT, 供新建类目/CLI 默认兜底)
    tpl = ROOT / "config.yaml"
    if not tpl.exists():
        tpl.write_text(
            "# 模板(占位): 真实类目配置请用 scripts/new_month_config.py 生成到 data/<类目>/config.yaml\n"
            '# 或复制本文件到 data/<新类目名>/config.yaml 后修改 CAT 与月份。\n'
            'CAT: "your-category-US-YYYYMM"\n'
            'LIST_URLS: ""\nSITE: "美国"\nMARKET: "amazon.com"\nDRY_RUN: true\n',
            encoding="utf-8", newline="\n",
        )
        print("已生成根目录模板 config.yaml(占位 CAT, 非真实类目)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
