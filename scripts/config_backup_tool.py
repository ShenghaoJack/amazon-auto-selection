# -*- coding: utf-8 -*-
"""配置备份治理工具(2026-09): list / restore / prune / arch。

备份目录: **主位置 data/<CAT>/config-backups/**(2026-09 起, 备份随类目) + 兜底 .webui/config-backups/
命名(新): <CAT清洗>-v<版本>.bak, 版本号按 CAT 字符串单调递增(旧命名 <stem>-<CAT>-<YYYYMMDD-HHMMSS>.bak 仍兼容)。
          文件头部带说明行: # 源文件 / # CAT / # 版本 / # 备份原因 / # 备份时间。
策略: 每个 CAT 保留最近 N 份(默认 10, 环境 WEBUI_KEEP_CONFIG_BACKUPS 可调) —— 超出的由 prune 删除。

用法:
  python scripts/config_backup_tool.py list [--cat 片段] [--source 片段]      # 含"版本缺号"巡检
  python scripts/config_backup_tool.py restore <备份文件名> [--to configs/xxx.yaml] [-y]
  python scripts/config_backup_tool.py prune [--keep N] [--dry-run]           # 清理并落日志
  python scripts/config_backup_tool.py arch list|show|edit|delete|use <快照>   # 归档快照里的 config.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # noqa: PGH003  (直接运行时保证 core 可导入)

from core.config_backup import (  # noqa: E402
    backup_paths,
    default_prune_logger,
    keep_count,
    list_backups,
    make_backup,
    prune,
    resolve_source,
    restore_backup,
)

ARCH_DIR = ROOT / "data" / "archive"


def _fmt(n: int) -> str:
    return f"{n / 1024:.1f}KB" if n >= 1024 else f"{n}B"


def cmd_list(args) -> int:
    rows = list_backups()
    if args.cat:
        rows = [r for r in rows if args.cat.lower() in (r["cat_raw"] or r["cat"]).lower()]
    if args.source:
        rows = [r for r in rows if args.source in (r.get("stem") or r["cat"])]
    if not rows:
        print("无匹配备份(扫 data/<CAT>/config-backups/ 与 .webui/config-backups/)")
        return 0
    cur = {}  # (cat, old) -> 最新 ts(标 *)
    for r in rows:
        key = (r["cat"], r["old"])
        if key not in cur or r["ts"] > cur[key]:
            cur[key] = r["ts"]
    print(f"{'文件':52s} {'CAT':36s} {'版本':6s} {'时间':18s} {'大小':8s} {'说明'}")
    for r in rows:
        key = (r["cat"], r["old"])
        star = " *" if cur.get(key) == r["ts"] else "  "
        ver = f"v{r['version']}" if r.get("version") else "-"
        cat_show = r.get("cat_raw") or r["cat"]
        src = resolve_source(r.get("stem") or r["cat"], r.get("cat_raw"))
        loc = " →当前存在" if src else " →当前无对应文件"
        print(f"{r['file']:52s} {cat_show[:36]:36s} {ver:6s} {r['ts']:18s} {_fmt(r['size']):8s} {r['reason'] or '-'}{star}{loc}")
    print("\n* = 该 (类目) 的最新一份; 恢复命令用文件名即可")
    # 缺号巡检(2026-09-13): 新命名备份按 CAT 递增, 缺号往往是"改过 CAT(旧名字的备份不在此序列)"
    # 或"被保留策略清掉了旧版本" —— 这里直接把缺口列出来, 便于判断历史是否完整
    miss = _missing_versions(rows)
    if miss:
        print("\n⚠ 版本缺号(同一 CAT 的 v1..vN 中缺失的编号):")
        for cat, gaps in miss:
            print(f"  {cat}: 缺 {', '.join('v' + str(g) for g in gaps)}"
                  f"(共缺 {len(gaps)} 个; 可能原因: 改过 CAT(旧名字下的备份不在此序列) / "
                  f"被 prune 按保留上限清理 —— 清理记录见 .webui/config-backups/prune.log)")
        print("  提示: 保留上限 = WEBUI_KEEP_CONFIG_BACKUPS(默认 10), prune 只删最旧的几份")
    return 0


def _missing_versions(rows: list[dict]) -> list[tuple[str, list[int]]]:
    """按 CAT 列出新命名备份里 v1..max 的缺号(仅看"新命名", 旧命名不参与计数)。"""
    vers: dict[str, list[int]] = {}
    for r in rows:
        if r.get("old") or not r.get("version"):
            continue
        vers.setdefault(r["cat"], []).append(int(r["version"]))
    out = []
    for cat, got in sorted(vers.items()):
        gap = [v for v in range(1, max(got) + 1) if v not in got]
        if gap:
            out.append((cat, gap))
    return out


def cmd_restore(args) -> int:
    bak = Path(args.backup)
    if not bak.is_absolute():
        # 2026-09-13 修复: 备份主位置已改为 data/<CAT>/config-backups/, 原来只查 .webui/config-backups/
        # 会导致"备份明明在 list 里却报不存在" —— 现在按文件名全量搜索所有备份目录
        cand = next((p for p in backup_paths() if p.name == bak.name), None)
        if cand is None:
            print(f"备份不存在: {bak.name}(可用 list 查看)")
            return 2
        bak = cand
    if not args.yes:
        ans = input(f"恢复 {bak.name} 会覆盖当前对应配置(恢复前会自动备份当前文件), 继续? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return 1
    to = Path(args.to) if args.to else None
    if to is not None and not to.is_absolute():
        to = ROOT / to
    try:
        target = restore_backup(bak, to=to)
    except ValueError as exc:
        print(f"恢复失败: {exc}")
        return 2
    print(f"已恢复 -> {target}(当前文件已自动备份)")
    return 0


def cmd_prune(args) -> int:
    # 2026-09-13 修复: 原来只把 keep 用来打印提示, 真正调用 prune() 时没传 —— `--keep N` 实际不生效
    keep = args.keep or keep_count()
    removed = prune(dry=args.dry_run, keep=keep,
                    log=None if args.dry_run else default_prune_logger())
    verb = "将删除" if args.dry_run else "已删除"
    print(f"{verb} {len(removed)} 份旧备份(每个类目保留最近 {keep} 份)")
    for p in removed[:20]:
        print(f"  {p.name}")
    if len(removed) > 20:
        print(f"  … 共 {len(removed)} 份")
    if removed and not args.dry_run:
        print("  清理记录已追加: .webui/config-backups/prune.log(及对应类目的 20_备份与监控/pipeline-progress.log)")
    return 0


# ==================== archive: 类目历史存档里的 config.yaml ====================
# 每个归档快照 data/archive/<CAT>-<时间戳>/config.yaml 是"该类目当时的配置",
# 支持 list / show(读取) / edit(改动) / delete(删除) / use(沿用为当前配置)。


def _arch_rows():
    rows = []
    if not ARCH_DIR.is_dir():
        return rows
    for snap in sorted(ARCH_DIR.iterdir()):
        cfg = snap / "config.yaml"
        if not snap.is_dir() or not cfg.is_file():
            continue
        cat = ""
        try:
            import yaml  # noqa: PLC0415
            cat = str((yaml.safe_load(cfg.read_text(encoding="utf-8-sig")) or {}).get("CAT", "") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        rows.append({"folder": snap.name, "cat": cat, "cfg": cfg,
                     "mtime": snap.stat().st_mtime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _resolve_arch_cfg(ref: str) -> Path | None:
    p = Path(ref)
    if p.is_file() and p.name == "config.yaml":
        return p
    if not p.is_absolute():
        cand = ARCH_DIR / ref
        if (cand / "config.yaml").is_file():
            return cand / "config.yaml"
        if cand.is_file() and cand.name == "config.yaml":
            return cand
    if p.is_dir() and (p / "config.yaml").is_file():
        return p / "config.yaml"
    return None


def _rewrite_keys(text: str, sets: list[str]) -> tuple[str, list[str]]:
    import re  # noqa: PLC0415
    lines = text.splitlines()
    changed = []
    for kv in sets:
        if "=" not in kv:
            raise ValueError(f"--set 需为 KEY=值 形式: {kv}")
        k, v = kv.split("=", 1)
        k = k.strip()
        hit = False
        for i, ln in enumerate(lines):
            if re.match(r"^[ \t]*" + re.escape(k) + r"\s*:", ln):
                lead = ln[: len(ln) - len(ln.lstrip())]
                lines[i] = f"{lead}{k}: \"{v}\""
                changed.append(k)
                hit = True
                break
        if not hit:
            lines.append(f"{k}: \"{v}\"")
            changed.append(k)
    return "\n".join(lines) + "\n", changed


def cmd_archive(args) -> int:
    rows = _arch_rows()
    if args.action in ("list", None):
        if args.cat:
            rows = [r for r in rows if args.cat.lower() in r["cat"].lower()]
        if not rows:
            print("无匹配的类目历史存档配置(data/archive/*/config.yaml)")
            return 0
        print(f"{'快照目录':52s} {'CAT':40s} {'时间':18s}")
        for r in rows:
            ts = __import__("datetime").datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{r['folder']:52s} {r['cat'][:40]:40s} {ts:18s}")
        print("\n子命令: show <快照目录> | edit <快照目录> --set K=V | delete <快照目录> | use <快照目录>")
        return 0
    if not args.ref:
        print("请指定快照目录(可用 list 查看)")
        return 2
    cfg = _resolve_arch_cfg(args.ref)
    if cfg is None:
        print(f"找不到该快照的 config.yaml: {args.ref}")
        return 2
    from datetime import datetime  # noqa: PLC0415
    if args.action == "show":
        print(f"=== {cfg} ===\n")
        print(cfg.read_text(encoding="utf-8-sig"))
        return 0
    if args.action == "edit":
        if not args.set:
            print("edit 需要 --set KEY=值(可多次), 例: --set TRAFFIC_MONTH=2026-09")
            return 2
        text = cfg.read_text(encoding="utf-8-sig")
        new, changed = _rewrite_keys(text, args.set)
        cfg.write_text(new, encoding="utf-8", newline="\n")
        print(f"[存档配置] 已修改: {cfg}")
        print(f"  变更项: {', '.join(changed)}  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return 0
    if args.action == "delete":
        if not args.yes and input(f"删除存档里的配置 {cfg} ?(快照数据目录保留, 仅删 config.yaml) [y/N]: ").strip().lower() not in ("y", "yes"):
            print("已取消")
            return 1
        cfg.unlink()
        print(f"[存档配置] 已删除: {cfg}  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return 0
    if args.action == "use":
        # 沿用该历史配置为"当前配置": 覆盖前自动版本备份当前(data/<CAT>/config.yaml)
        import yaml  # noqa: PLC0415
        content = cfg.read_text(encoding="utf-8-sig")
        cat = str((yaml.safe_load(content) or {}).get("CAT", "") or "").strip()
        if not cat:
            print("该存档配置缺少 CAT, 无法定位当前类目文件")
            return 2
        current = ROOT / "data" / cat / "config.yaml"
        current.parent.mkdir(parents=True, exist_ok=True)
        bak = make_backup(current, reason=f"沿用历史存档配置 {cfg.parent.name} 前自动备份",
                          log=lambda m: print(m), changed_to=content)
        current.write_text(content, encoding="utf-8", newline="\n")
        print(f"[存档配置] 已沿用为当前配置: {current}" + (f" (当前原配置已备份 v{bak.name})" if bak else ""))
        print("如需进一步改动, 请用 webui 配置中心编辑或 edit --set 修改存档后再 use。")
        return 0
    print(f"未知动作: {args.action}")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="配置备份治理(list/restore/prune)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("list", help="列出全部备份(标最新/当前对应)")
    p1.add_argument("--cat", default="", help="按 CAT 片段过滤")
    p1.add_argument("--source", default="", help="按源文件名 stem 片段过滤")
    p1.set_defaults(func=cmd_list)
    p2 = sub.add_parser("restore", help="恢复某份备份(恢复前自动备份当前)")
    p2.add_argument("backup", help="备份文件名(.webui/config-backups/ 下)")
    p2.add_argument("--to", default=None, help="恢复目标(缺省按 stem 自动定位 root/configs)")
    p2.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p2.set_defaults(func=cmd_restore)
    p3 = sub.add_parser("prune", help="按保留策略清理旧备份")
    p3.add_argument("--keep", type=int, default=None, help="每(源文件+CAT)保留份数(默认 10/环境)")
    p3.add_argument("--dry-run", action="store_true", help="只列出将删除, 不真删")
    p3.set_defaults(func=cmd_prune)
    p4 = sub.add_parser("archive", help="读取/改动/删除/沿用 类目历史存档里的 config.yaml")
    p4.add_argument("action", nargs="?", choices=["list", "show", "edit", "delete", "use"], default="list",
                    help="list=列出存档配置; show=读取打印; edit=改动(--set); delete=删除; use=沿用为当前")
    p4.add_argument("ref", nargs="?", default=None, help="快照目录名(或 config.yaml 路径), list 时省略")
    p4.add_argument("--cat", default="", help="list 时按 CAT 片段过滤")
    p4.add_argument("--set", action="append", default=[], help="edit 用: KEY=值, 可多次")
    p4.add_argument("-y", "--yes", action="store_true", help="delete 跳过确认")
    p4.set_defaults(func=cmd_archive)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
