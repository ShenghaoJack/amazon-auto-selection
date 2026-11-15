# -*- coding: utf-8 -*-
"""一键生成"下月/新类目"配置文件(2026-09 配置治理)。

从源配置复制为 configs/<名>-<月份>.yaml, 自动:
  - 新 CAT = 源 CAT 的月份段替换为新月份(形如 -202607 → -202608); 未带月份则追加
  - TRAFFIC_MONTH / COMPETITOR_MONTH = <YYYY>-<MM>, TREND_MONTH = <YYYYMM>
其余行原样保留(整行改写, 注释不动), 不改动源文件。

用法:
  python scripts/new_month_config.py --source configs/xxx-202607.yaml --month 202608
  python scripts/new_month_config.py --source config.yaml --month 202608 --cat "salt and pepper mill sets-US-202608" [--out configs/xxx.yaml] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("TRAFFIC_MONTH", "COMPETITOR_MONTH", "TREND_MONTH")


def next_cat(cat: str, month: str) -> str:
    """CAT 月份段替换: 'xxx-US-202607' + 202608 -> 'xxx-US-202608'; 无月份段则追加。"""
    base = str(cat or "").strip()
    if not base:
        return f"cat-{month}"
    if re.search(r"-(20\d{4})$", base):
        return re.sub(r"-(20\d{4})$", f"-{month}", base)
    return f"{base}-{month}"


def default_out(cat: str, month: str) -> Path:
    """默认输出(2026-09 类目配置模型): data/<新CAT>/config.yaml(配置随类目)。"""
    return ROOT / "data" / cat / "config.yaml"


def _rewrite_key_lines(lines: list[str], key: str, value: str) -> bool:
    for i, ln in enumerate(lines):
        if re.match(r"^[ \t]*" + re.escape(key) + r"\s*:", ln):
            lead = ln[: len(ln) - len(ln.lstrip())]
            lines[i] = f"{lead}{key}: \"{value}\""
            return True
    return False


def build(source: Path, month: str, cat: str | None, out: Path) -> tuple[Path, str, str, str]:
    """读取源并生成新配置内容写出, 返回 (out, cat, traffic_month, trend_month)。"""
    text = source.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    src_cat = ""
    for ln in lines[:30]:
        m = re.match(r"^[ \t]*CAT\s*:\s*(.*)$", ln)
        if m:
            src_cat = m.group(1).strip().strip("\"'")
            break
    new_cat = cat or next_cat(src_cat, month)
    yyyymm = re.fullmatch(r"(\d{4})(\d{2})", month)
    if not yyyymm:
        raise ValueError(f"月份格式应为 YYYYMM, 收到: {month}")
    traffic = f"{yyyymm.group(1)}-{yyyymm.group(2)}"
    changed = []
    if _rewrite_key_lines(lines, "CAT", new_cat):
        changed.append("CAT")
    for key, val in (("TRAFFIC_MONTH", traffic), ("COMPETITOR_MONTH", traffic), ("TREND_MONTH", month)):
        if _rewrite_key_lines(lines, key, val):
            changed.append(key)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out, new_cat, traffic, month


def main() -> int:
    ap = argparse.ArgumentParser(description="一键生成下月/新类目配置(写入 configs/)")
    ap.add_argument("--source", required=True, help="源配置文件(root 或 configs/, 如 config.yaml / configs/xxx-202607.yaml)")
    ap.add_argument("--month", required=True, help="目标月份 YYYYMM, 如 202608")
    ap.add_argument("--cat", default=None, help="新 CAT(缺省自动: 源 CAT 月份段替换/追加)")
    ap.add_argument("--out", default=None, help="输出路径(缺省 configs/<源名>-<month>.yaml)")
    ap.add_argument("--dry-run", action="store_true", help="只打印将生成的路径与改动, 不写盘")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.is_absolute():
        source = ROOT / source
    if not source.is_file():
        print(f"源配置不存在: {source}")
        return 2
    text = source.read_text(encoding="utf-8-sig")
    src_cat = next((m.group(1).strip().strip("\"'") for m in
                    [re.match(r"^[ \t]*CAT\s*:\s*(.*)$", ln) for ln in text.splitlines()[:30]]
                    if m), "")
    preview_cat = args.cat or next_cat(src_cat, args.month)
    out = Path(args.out) if args.out else default_out(preview_cat, args.month)
    if not out.is_absolute():
        out = ROOT / out
    print(f"源配置 : {source}")
    print(f"目标文件: {out}")
    print(f"新 CAT : {preview_cat}")
    print(f"月份   : TRAFFIC/COMPETITOR_MONTH={args.month[:4]}-{args.month[4:]}  TREND_MONTH={args.month}")
    if args.dry_run:
        print("(dry-run, 未写盘)")
        return 0
    if out.exists() and not input(f"\n目标已存在, 覆盖 {out} ? [y/N]: ").strip().lower() in ("y", "yes"):
        print("已取消")
        return 1
    out, new_cat, traffic, _month = build(source, args.month, preview_cat, out)
    print(f"\n已生成: {out}\n  CAT={new_cat}\n  TRAFFIC_MONTH/COMPETITOR_MONTH={traffic}\n  TREND_MONTH={args.month}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
