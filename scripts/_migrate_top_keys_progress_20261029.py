# -*- coding: utf-8 -*-
"""一次性迁移：把「派生/过程字段」从各类目 rules.json 的 top_keys 挪到 allow_keys。

背景（2026-10-26 代码默认值已改）：
  DEFAULT_TOP_KEYS 只保留 xl_row/category_check/attributes；
  尺寸_拆列/尺寸_依据/pack_count_数值/suitable_scene_规范串/词表外词/跨列纠错/冲突
  改为 DEFAULT_ALLOW_KEYS（允许但不要求）。
  但旧类目 rules.json 自己写死了旧的 top_keys（含这些派生字段）→ 闸门要求每行必须写
  → LLM 不写 → 每行"缺键" → 整份 REJECT（Salt and Pepper 的 1308 错就是这么来的）。

本脚本只动「代码默认 allow_keys 里登记的那 7 个派生字段」，不动别的必需键；
跳过 Salt and Pepper（还在跑）。

用法:
  python scripts/_migrate_top_keys_progress_20261029.py            # 干跑(只报告)
  python scripts/_migrate_top_keys_progress_20261029.py --apply    # 实际改（先备份 .bak-...）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

ROOT = r"D:\deepseek工作区\auto-workflow-python\data"
# 与 tools/attr_stability/attr_stability.py 的 DEFAULT_ALLOW_KEYS 保持一致
DERIVED = {
    "尺寸_拆列", "尺寸_依据", "pack_count_数值", "suitable_scene_规范串",
    "词表外词", "跨列纠错", "冲突",
}
SKIP_CATS = set()   # Salt and Pepper 已重置（2026-10-29 09:17），不再跳过
# 非类目目录（不扫）
SKIP_DIRS = {"_catalog", "archive", "profiles", "_synonyms"}


def find_rule_files():
    out = []
    for cat in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, cat)
        if not os.path.isdir(p) or cat in SKIP_CATS or cat in SKIP_DIRS:
            continue
        # 只认类目目录（有 config.yaml 或 有 *.rules.json）
        if not (os.path.exists(os.path.join(p, "config.yaml"))
                or glob.glob(os.path.join(p, "*.rules.json"))):
            continue
        for rp in sorted(glob.glob(os.path.join(p, "*.rules.json"))):
            out.append((cat, rp))
    return out


def plan(path):
    """返回 (move, new_top, new_allow) 或 None（无需迁移）。"""
    with open(path, encoding="utf-8-sig") as f:
        r = json.load(f)
    top = [str(k) for k in (r.get("top_keys") or [])]
    allow = [str(k) for k in (r.get("allow_keys") or [])]
    move = [k for k in top if k in DERIVED]
    if not move:
        return None
    new_top = [k for k in top if k not in DERIVED]
    new_allow = allow + [k for k in move if k not in allow]
    return r, move, new_top, new_allow


def apply(path, r, new_top, new_allow):
    shutil.copy2(path, path + ".bak-pretopkeys-20261029")
    r["top_keys"] = new_top
    r["allow_keys"] = new_allow
    with open(path, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)


def verify(cat, path):
    """迁移后重跑闸门：用该类目最新 run 文件抽样，报错误数。"""
    try:
        sys.path.insert(0, os.path.join(ROOT, ".."))
        from tools.attr_stability.attr_stability import RuleSource, gate_run
        slug = os.path.basename(path)[: -len(".rules.json")]
        rules = RuleSource.load(os.path.dirname(path), slug)
        run_dir = os.path.join(os.path.dirname(path), "17_属性标注", "runs")
        files = sorted(glob.glob(os.path.join(run_dir, "*.json")),
                       key=os.path.getmtime, reverse=True)
        if not files:
            return "  (无 17_属性标注/runs 产物, 跳过闸门验证)"
        run = json.load(open(files[0], encoding="utf-8"))
        rows = run.get("rows") if isinstance(run, dict) and "rows" in run else run
        items = list(rows.values()) if isinstance(rows, dict) else list(rows)
        sample = items[:30]
        res = gate_run(sample, rules)
        return ("  验证(%s 前%d行): ERR %d / WARN %d | top_keys=%s | allow_keys=%s"
                % (os.path.basename(files[0]), len(sample), res.errors, res.warnings,
                   sorted(rules.top_keys), sorted(rules.allow_keys)))
    except Exception as exc:  # noqa: BLE001
        return "  (验证跳过: %s)" % str(exc)[:100]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认干跑）")
    args = ap.parse_args()

    files = find_rule_files()
    print("扫描到 %d 个类目 rules.json（已跳过: %s）" % (len(files), ", ".join(sorted(SKIP_CATS))))
    changed = 0
    for cat, rp in files:
        res = plan(rp)
        if not res:
            print("  [无需改] %s" % cat)
            continue
        r, move, new_top, new_allow = res
        changed += 1
        print("\n  [需迁移] %s" % cat)
        print("     从 top_keys 移出: %s" % move)
        print("     top_keys → %s" % new_top)
        print("     allow_keys → %s" % new_allow)
        if args.apply:
            apply(rp, r, new_top, new_allow)
            print(verify(cat, rp))
    print("\n合计需要迁移: %d 个类目%s" % (changed, "（已写入）" if args.apply else "（干跑，加 --apply 才写）"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
