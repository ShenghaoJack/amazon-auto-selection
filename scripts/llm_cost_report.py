"""跨类目 LLM 用量/费用报表。

用法:
  python scripts/llm_cost_report.py                      # 扫全部类目的实时账本
  python scripts/llm_cost_report.py --cat "Cutting Boards-US-202608"
  python scripts/llm_cost_report.py --archive            # 额外纳入归档快照里的账本
  python scripts/llm_cost_report.py --top 3              # 每轮只列费用前 3 的步骤
  python scripts/llm_cost_report.py --csv llm-cost.csv   # 顺带导出明细 CSV

数据源: `data/<CAT>/20_备份与监控/llm-usage.jsonl`(含轮转分卷 `llm-usage-*.jsonl`)。
口径:
  - 按 **run_id** 累加 step-summary → **同一轮被强杀后分多段跑完也自动合并成一本**
    (run_id = 归档快照目录名, 见 `PipelineRunner.snapshot_dir`)。
  - **按通道(文本/图片)拆开**: 项目有两个模型、两个 key —— 文本用 `LLM_MODEL`,
    图片用 `VISION_MODEL`; 而且图片 token 是服务端按尺寸折算后并进 `prompt_tokens` 的,
    **只看合计永远看不出图片花了多少**。表格与 `--csv` 都按 通道×模型 分行。
  - 费用是本地按价表**估算**(usage 为官方口径; 单价按调用时刻的高峰/空闲档),
    **不等于平台账单** —— 平台后台按 API key 看的是账户级总数, 拆不到类目/步骤。
  - `--archive` 会优先用实时账本, 只把"实时里没有的 run"从归档补进来,
    避免同一轮被实时+归档各算一次。

纯离线、零新增依赖、不联网。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.usage import channel_label, ledger_paths, load_records, summarize_runs  # noqa: E402

# 长表: 每 (类目 × 轮 × 步骤 × 通道 × 模型) 一行 —— 文本与图片分开,
# 合并成"每步一行"会把"图片花了多少"永久丢掉(图片 token 是服务端按尺寸折算进 prompt 的)。
CSV_HEADER = ["类目", "轮", "步骤", "步骤名", "通道", "模型", "调用数", "命中token",
              "未命中token", "输出token", "费用估算元", "占本轮比例"]


def _channel_suffix(mapping: dict) -> str:
    """双通道时的 `[文本 N次/X元 + 视觉 M次/Y元]` 后缀; 单通道返回空串。"""
    live = {c: v for c, v in (mapping or {}).items()
            if v.get("calls") or v.get("failed_attempts")}
    if len(live) < 2:
        return ""
    return "[" + " + ".join(
        f"{channel_label(c)} {v['calls']}次/{v['cost_cny']:.4f}元"
        for c, v in sorted(live.items())) + "]"


def _step_titles() -> dict[int, str]:
    """步骤号 → 标题。取不到就算了(注册表会连带导入 playwright 等重依赖)。"""
    try:
        from steps.registry import get_steps
        return {s.number: s.title for s in get_steps()}
    except Exception:                                    # noqa: BLE001
        return {}


def _monitor_dirs(data_root: Path, include_archive: bool) -> tuple[list[Path], list[Path]]:
    """返回 (实时监控目录列表, 归档监控目录列表)。"""
    live: list[Path] = []
    for cfg in sorted(data_root.glob("*/config.yaml")):
        mdir = cfg.parent / "20_备份与监控"
        if mdir.is_dir():
            live.append(mdir)
    archive: list[Path] = []
    if include_archive:
        for snap in sorted((data_root / "archive").glob("*/20_备份与监控")):
            if snap.is_dir():
                archive.append(snap)
    return live, archive


def _collect(live: list[Path], archive: list[Path]) -> tuple[list[dict], int]:
    """汇总账本记录: 实时优先, 归档只补"实时里没有的 run"。"""
    records: list[dict] = []
    for mdir in live:
        for path in ledger_paths(str(mdir)):
            records.extend(load_records(path))
    seen = {(r.get("cat"), r.get("run_id")) for r in records}
    added = 0
    for mdir in archive:
        recs: list[dict] = []
        for path in ledger_paths(str(mdir)):
            recs.extend(load_records(path))
        if not recs:
            continue
        keys = {(r.get("cat"), r.get("run_id")) for r in recs} - seen
        if not keys:
            continue
        records.extend(r for r in recs if (r.get("cat"), r.get("run_id")) in keys)
        seen |= keys
        added += len(keys)
    return records, added


def _fmt_int(n) -> str:
    return f"{int(n):,}"


def main() -> int:
    ap = argparse.ArgumentParser(description="跨类目 LLM 用量/费用报表")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--cat", default="", help="只看名字含该片段的类目(与 check_configs.py 同口径)")
    ap.add_argument("--archive", action="store_true", help="额外纳入归档快照的账本")
    ap.add_argument("--top", type=int, default=5, help="每轮列出费用前 N 个步骤(0=全部)")
    ap.add_argument("--csv", default="", help="导出明细 CSV 路径")
    args = ap.parse_args()

    root = Path(args.data_root)
    if not root.is_dir():
        print(f"数据根不存在: {root}")
        return 2

    live, archive = _monitor_dirs(root, args.archive)
    if not live and not archive:
        print(f"{root} 下没有找到任何 20_备份与监控 目录")
        return 1

    records, added_runs = _collect(live, archive)
    if args.cat:
        # **片段匹配**(不是精确相等): 与 scripts/check_configs.py --cat 同口径,
        # 类目名很长(带站点/月份后缀), 每次敲全名不现实。
        needle = args.cat.strip().lower()
        records = [r for r in records if needle in str(r.get("cat") or "").lower()]
    if not records:
        print("没有账本记录。可能是: 尚未跑过带 LLM 的步骤, 或 LLM_USAGE_LOG=false。")
        return 1

    runs = summarize_runs(records)
    titles = _step_titles()
    files = sum(len(ledger_paths(str(d))) for d in live + archive)

    print(f"数据根: {root} | 账本文件 {files} 个"
          + (f" | 归档补充 {added_runs} 轮" if args.archive else ""))
    print(f"记录 {_fmt_int(len(records))} 条 | 轮次 {len(runs)} 个"
          + (f" | 过滤类目: {args.cat}" if args.cat else ""))
    print()

    grand_cost = 0.0
    grand_tokens = 0
    grand_calls = 0
    grand_ch: dict[str, dict] = {}
    csv_rows: list[list] = []

    for idx, run in enumerate(runs, start=1):
        t = run["totals"]
        grand_cost += t["cost_cny"]
        grand_tokens += t["total_tokens"]
        grand_calls += t["calls"]
        for ch, v in (run.get("totals_by_channel") or {}).items():
            acc = grand_ch.setdefault(ch, {"calls": 0, "cost_cny": 0.0, "total_tokens": 0})
            acc["calls"] += v.get("calls", 0)
            acc["cost_cny"] += v.get("cost_cny", 0.0)
            acc["total_tokens"] += v.get("total_tokens", 0)
        print(f"[{idx}] {run['cat'] or '(未知类目)'}  ·  {run['run_id'] or '(未知轮)'}")
        print(f"    调用 {_fmt_int(t['calls'])} | tokens {_fmt_int(t['total_tokens'])}"
              f" (命中 {_fmt_int(t['in_hit'])}/未命中 {_fmt_int(t['in_miss'])}"
              f"/输出 {_fmt_int(t['out'])})"
              f" | 费用估算 {t['cost_cny']:.4f} 元"
              + _channel_suffix(run.get("totals_by_channel")))
        notes = []
        if run["price_source"]:
            notes.append(f"价格来源={run['price_source']}")
        if t["unpriced_calls"]:
            notes.append(f"未定价 {t['unpriced_calls']} 次")
        if t["failed_attempts"]:
            notes.append(f"失败尝试 {t['failed_attempts']} 次(未计费)")
        if notes:
            print("    " + "; ".join(notes))

        steps = sorted(run["steps"].items(),
                       key=lambda kv: kv[1]["cost_cny"], reverse=True)
        visible = [(n, row) for n, row in steps
                   if row["calls"] or row["failed_attempts"]]
        # CSV 始终导出**全部**步骤(--top 只控制控制台显示, 不该悄悄截断导出)
        for num, row in visible:
            name = titles.get(int(num), "")
            by_model = row.get("by_model") or {}
            if not by_model:
                # 老账本没有 by_model → 合并口径补一行; 模型留空(确实不知道),
                # 通道标 unknown(与 summarize_run 的通道兜底口径一致)
                by_model = {"unknown/": row}
            for key in sorted(by_model, key=lambda k: -by_model[k]["cost_cny"]):
                channel, _, model = key.partition("/")
                g = by_model[key]
                share = (g["cost_cny"] / t["cost_cny"] * 100) if t["cost_cny"] else 0.0
                csv_rows.append([run["cat"], run["run_id"], f"{int(num):02d}", name,
                                 channel, model, g["calls"], g["in_hit"], g["in_miss"],
                                 g["out"], f"{g['cost_cny']:.6f}", f"{share:.1f}"])
        shown = visible if args.top <= 0 else visible[:args.top]
        for num, row in shown:
            share = (row["cost_cny"] / t["cost_cny"] * 100) if t["cost_cny"] else 0.0
            name = titles.get(int(num), "")
            print(f"      步骤{int(num):02d} {name:<14} {row['calls']:>6} 次"
                  f"  {_fmt_int(row['total_tokens']):>14} tok"
                  f"  {row['cost_cny']:>9.4f} 元  {share:>5.1f}%")
            # 双通道的步骤才再打一行通道拆分(这才是"图片花了多少"的答案);
            # 单通道时打出来只会是"└ 文本 100.0%", 纯噪音。
            live_ch = {c: g for c, g in (row.get("by_channel") or {}).items() if g.get("calls")}
            if len(live_ch) > 1:
                for ch, g in sorted(live_ch.items()):
                    ch_share = (g["cost_cny"] / row["cost_cny"] * 100) if row["cost_cny"] else 0.0
                    print(f"          └ {channel_label(ch):<4} {g['calls']:>6} 次"
                          f"  {_fmt_int(g['total_tokens']):>14} tok"
                          f"  {g['cost_cny']:>9.4f} 元  {ch_share:>5.1f}%")
        if 0 < args.top < len(visible):
            print(f"      … 另有 {len(visible) - args.top} 个步骤未列出(用 --top 0 看全)")
        print()

    print("=" * 72)
    print(f"合计: {len(runs)} 轮 | 调用 {_fmt_int(grand_calls)} 次 | "
          f"tokens {_fmt_int(grand_tokens)} | 费用估算 {grand_cost:.4f} 元"
          + _channel_suffix(grand_ch))
    print("口径提示: tokens 以模型返回的 usage 为准; 费用为本地按价表估算, 非平台账单。")
    print("          文本/图片是两个模型(两个 key), 上表已按通道拆开 —— 图片 token 由服务端按尺寸折算。")
    print("=" * 72)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            writer.writerows(csv_rows)
        print(f"\n明细已导出: {args.csv}({len(csv_rows)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
