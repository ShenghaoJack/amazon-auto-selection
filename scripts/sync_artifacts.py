# -*- coding: utf-8 -*-
"""分端部署的**交接层**(2026-10-08, 阶段 1): 产物指纹 manifest + 校验 + 推送/拉取。

## 为什么需要它

设想是"**要爬数据的步骤在本地跑**(0/1/9/10/11/12/13), **LLM/数据处理/MCP 放服务器跑**
(2–8、14–19)"。流水线 20 步之间**只靠 `data/<CAT>/` 里的文件衔接**, 所以拆分能否成立取决于两件事:

  ① 两端看到的产物**完整且一致** —— 半同步的大文件绝不能被当成"这步跑好了";
  ② 同一时刻**只有一端在写** —— `step-status.txt` / `.run.lock` 都是**本地文件**, 跨机无效。

本工具解决 ①: 每步跑完后把"这步产出了哪些文件 / 多大 / 什么 hash / 谁在哪台机器上跑的"写成
`data/<CAT>/20_备份与监控/step-manifests/stepNN.json`; 另一端**在读之前校验**这些指纹。
② 靠使用约定 + 本工具的 `extra` 提示(见 `SPLIT_DEPLOYMENT_GUIDE.md`)。

## 命令

    write   产出端跑完后写/刷新 manifest
    verify  读取端开跑前校验(缺文件/内容被改 → 退出码 2)
    status  一屏看 0..19: 哪步、谁跑的、什么时候、现在还一致吗
    push    把类目产物 + 步骤状态 + manifest 推到目标目录(NAS/SMB/网盘挂载点)
    pull    反向拉取

## 机器本地配置

"我是哪一端"是**机器属性**, 不是类目属性 —— 所以**不写进 `config.yaml`**(否则同步来同步去
会把 role 也带过去)。优先级: 命令行 > 环境变量 > 仓库根 `sync.local.yaml` > 默认:

    role    local|server      --role / AWP_SYNC_ROLE / sync.local.yaml: role      (默认 local)
    target  <目录>            --target / AWP_SYNC_TARGET / sync.local.yaml: target (默认空)

示例见仓库根 `sync.local.example.yaml`。

## 退出码

    0  一致(或有非致命的提示)
    2  校验发现问题(缺文件 / 内容不一致 / 尚无 manifest)
    1  用法或 IO 错误
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

# 与 scripts/ 下其它脚本同款引导: 直接 `python scripts/sync_artifacts.py` 时把仓库根加入 sys.path,
# 否则 `import core` / `steps` 会 ModuleNotFoundError(脚本目录才是 sys.path[0])。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCHEMA = 1
MANIFEST_SUBDIR = "step-manifests"
STEP_STATUS_NAME = "step-status.txt"
# 全量 sha256 的体积上限; 超过则用"头+尾各 4MB + 体积"的部分哈希(够用于"是否被改过"的判定,
# 且避免每次把几十上百 MB 的下载件/备份重新读一遍)。
HASH_FULL_MAX = 64 * 1024 * 1024
HASH_PART = 4 * 1024 * 1024
# 这些目录名/文件名不参与指纹(备份、归档、浏览器 Profile、缓存等"非步骤产物")
# `step-manifests` 必须排除: manifest 自己就写在步骤19 的目录里, 不排除就会**把自己算进指纹**,
# 于是每次 write 之后 verify(19) 必定报"内容已变"(自指)。
EXCLUDE_DIRS = {"config-backups", "backups", "archive", "profiles", "__pycache__",
                ".webui", ".git", "tmp", "pytest_ws", ".pytest-tmp", "node_modules",
                "step-manifests"}
EXCLUDE_SUFFIX = (".tmp", ".part", ".crdownload", ".lock")
EXCLUDE_PREFIX = ("~$", ".~")
# **追加型/易变**的状态文件: 会随另一端跑步骤而变(step-status 每步都会写, 日志一直在追加),
# 所以它们**只做同步、不做 hash 判定** —— 否则每次交接都会误报"内容不一致", 分端就没法用了。
VOLATILE_RELS = {
    "20_备份与监控/step-status.txt",
    "20_备份与监控/pipeline-progress.log",
    "20_备份与监控/monitor-alerts.txt",
}
# 传输时**始终**一并带上的"状态类"文件(另一端据此知道进度)
ALWAYS_SYNC = ("20_备份与监控/step-status.txt",)
ROLE_ENV = "AWP_SYNC_ROLE"
TARGET_ENV = "AWP_SYNC_TARGET"
LOCAL_CFG_NAME = "sync.local.yaml"


# --------------------------------------------------------------------------- 基础工具

def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha256(path: Path, *, start: int = 0, length: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        if start:
            fh.seek(start)
        left = length
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n <= 0:
                break
            chunk = fh.read(n)
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def file_sig(path: Path, *, full_max: int = HASH_FULL_MAX) -> dict:
    """单文件指纹。mtime 只作参考 —— **同步工具会改 mtime**, 判定一致性只认 size+hash。"""
    st = path.stat()
    size = int(st.st_size)
    if size <= full_max:
        return {"size": size, "sha256": _sha256(path), "hash_mode": "full",
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    head = _sha256(path, length=HASH_PART)
    tail = _sha256(path, start=max(0, size - HASH_PART), length=HASH_PART)
    return {"size": size, "sha256": f"{head}-{tail}", "hash_mode": "partial",
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


def _excluded(p: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return True
    n = p.name
    return n.endswith(EXCLUDE_SUFFIX) or n.startswith(EXCLUDE_PREFIX)


def step_declarations(cat: str = "") -> dict[int, list[str]]:
    """各步骤**声明的产物**(步骤类的 `artifacts` + `extra_archive_files`), 相对类目根。

    ⚠️ 与 webui 的 `_step_declarations(cat)` 同款做法: 这些属性按 `self._cat` 拼类目名 ——
    **必须先 `s._cat = cat` 再读**, 否则属性内部抛 AttributeError(被 getattr 默认值吞掉)或拼出
    `None-xxx.xlsx` 这种假名字(实测踩过: 不设 `_cat` 时 0/1/8/10 步声明全空 → manifest 里 0 个文件)。
    """
    out: dict[int, list[str]] = {}
    from steps.registry import get_steps

    for s in get_steps():
        try:
            num = int(getattr(s, "number", -1))
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        if cat:
            try:
                s._cat = cat            # noqa: SLF001 — 与 webui 同款注入方式
            except Exception:           # noqa: BLE001
                pass
        rels: list[str] = []
        for attr in ("artifacts", "extra_archive_files"):
            try:
                val = list(getattr(s, attr) or [])
            except Exception:           # noqa: BLE001 — 该步声明拿不到就当没有
                val = []
            for rel in val:
                rel = str(rel).replace("\\", "/").strip()
                if cat:
                    rel = rel.replace("CAT", cat)     # 与 webui `_expand_artifact` 同口径
                if rel and rel not in rels:
                    rels.append(rel)
        out[num] = rels
    return out


# --------------------------------------------------------------------------- 机器本地配置

def local_config(root: Path | None = None) -> dict:
    """读仓库根 `sync.local.yaml`(机器本地配置; 不存在则空)。"""
    p = (root or repo_root()) / LOCAL_CFG_NAME
    if not p.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 本地配置坏了不该让命令挂掉
        return {}


def resolve_role(cli_role: str | None = None, root: Path | None = None) -> str:
    """命令行 > 环境变量 > sync.local.yaml > 默认 local。"""
    for cand in (cli_role, os.environ.get(ROLE_ENV), local_config(root).get("role")):
        if cand:
            return str(cand).strip().lower()
    return "local"


def resolve_target(cli_target: str | None = None, root: Path | None = None) -> str:
    for cand in (cli_target, os.environ.get(TARGET_ENV), local_config(root).get("target")):
        if cand:
            return str(cand).strip()
    return ""


# --------------------------------------------------------------------------- 文件集合

def step_files(cat_root: Path, step: int, decls: dict[int, list[str]] | None = None) -> list[Path]:
    """某步骤的产物文件集合。

    规则(与 webui 的"点击步骤卡片"同口径, 但排除备份/归档等噪音):
      * 该步**声明**的产物(相对类目根), 存在才算;
      * 有独立实时子目录的步骤(11/12/13/14/19)再并入该子目录下的全部文件(递归)。
    平铺在类目根的步骤(0..10、15..18)只认声明产物 —— 否则会把别的步骤的产物也扫进来。
    """
    from core.paths import REALTIME_SUBDIRS, STEP_DIR_NAMES

    decls = decls if decls is not None else {}
    out: list[Path] = []
    for rel in decls.get(int(step), []):
        p = cat_root / rel
        if p.is_file() and not _excluded(p.relative_to(cat_root)):
            out.append(p)
    if int(step) in REALTIME_SUBDIRS:
        d = cat_root / STEP_DIR_NAMES[int(step)]
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file() and not _excluded(p.relative_to(cat_root)):
                    out.append(p)
    # 去重 + 保持稳定顺序(相对路径排序, 便于 diff)
    uniq: dict[str, Path] = {}
    for p in out:
        uniq[str(p.relative_to(cat_root)).replace("\\", "/")] = p
    return [uniq[k] for k in sorted(uniq)]


def manifest_dir(cat_root: Path) -> Path:
    from core.paths import STEP_DIR_NAMES

    return cat_root / STEP_DIR_NAMES[20] / MANIFEST_SUBDIR


def manifest_path(cat_root: Path, step: int) -> Path:
    return manifest_dir(cat_root) / f"step{int(step):02d}.json"


def read_step_status(cat_root: Path, step: int) -> dict:
    """从 `step-status.txt` 取该步那一行(状态/起止/失败数); 缺失则空。"""
    from core.paths import STEP_DIR_NAMES

    p = cat_root / STEP_DIR_NAMES[20] / STEP_STATUS_NAME
    if not p.is_file():
        return {}
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = ln.split("|")
            if len(parts) >= 4 and parts[0].strip() == str(int(step)):
                return {"state": parts[1].strip(), "started_at": parts[2].strip(),
                        "finished_at": parts[3].strip(),
                        "fails": int(parts[6]) if len(parts) > 6 and parts[6].strip().isdigit() else 0,
                        "interrupts": int(parts[7]) if len(parts) > 7 and parts[7].strip().isdigit() else 0}
    except OSError:
        return {}
    return {}


# --------------------------------------------------------------------------- write

def build_manifest(cat_root: Path, step: int, *, role: str, decls: dict[int, list[str]] | None = None,
                   cat: str = "", data_root: str = "", full_max: int = HASH_FULL_MAX) -> dict:
    files = step_files(cat_root, step, decls)
    sigs = []
    for p in files:
        rel = str(p.relative_to(cat_root)).replace("\\", "/")
        decl = bool(decls and rel in (decls.get(int(step)) or []))
        sig = file_sig(p, full_max=full_max)
        sig.update({"rel": rel, "declared": decl})
        sigs.append(sig)
    declared = list((decls or {}).get(int(step)) or [])
    have = {s["rel"] for s in sigs}
    status = read_step_status(cat_root, step)
    from core.paths import STEP_DIR_NAMES

    man = {
        "schema": SCHEMA,
        "cat": cat or cat_root.name,
        "data_root": data_root,
        "step": int(step),
        "step_dir": STEP_DIR_NAMES.get(int(step), ""),
        "role": role,
        "machine": socket.gethostname(),
        "written_at": _now(),
        "file_count": len(sigs),
        "total_size": sum(s["size"] for s in sigs),
        "files": sigs,
        "missing_declared": [rel for rel in declared if rel not in have],
    }
    man.update({k: status[k] for k in ("state", "started_at", "finished_at", "fails", "interrupts")
                if k in status})
    return man


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_manifests(cat_root: Path, steps: list[int], *, role: str, cat: str = "",
                    data_root: str = "", decls: dict[int, list[str]] | None = None,
                    full_max: int = HASH_FULL_MAX) -> list[dict]:
    """写/刷新若干步骤的 manifest, 并更新 `index.json`。返回写出的 manifest 列表。"""
    if decls is None:
        decls = step_declarations(cat)
    written = []
    for step in steps:
        man = build_manifest(cat_root, step, role=role, decls=decls, cat=cat,
                             data_root=data_root, full_max=full_max)
        _atomic_write_json(manifest_path(cat_root, step), man)
        written.append(man)
    _update_index(cat_root)
    return written


def _update_index(cat_root: Path) -> dict:
    """汇总类目下所有 manifest → index.json(便于 status 一屏看)。"""
    idx = {"schema": SCHEMA, "cat": cat_root.name, "updated_at": _now(), "steps": {}}
    for p in sorted(manifest_dir(cat_root).glob("step*.json")):
        try:
            man = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        step = int(man.get("step", -1))
        if step < 0:
            continue
        idx["steps"][str(step)] = {
            "role": man.get("role"), "machine": man.get("machine"),
            "state": man.get("state"), "written_at": man.get("written_at"),
            "file_count": man.get("file_count"), "total_size": man.get("total_size"),
        }
    _atomic_write_json(manifest_dir(cat_root) / "index.json", idx)
    return idx


def load_manifest(cat_root: Path, step: int) -> dict | None:
    p = manifest_path(cat_root, step)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- verify

def verify_step(cat_root: Path, step: int, *, check_hash: bool = True,
                full_max: int = HASH_FULL_MAX, with_extra: bool = True,
                decls: dict[int, list[str]] | None = None) -> dict:
    """把"现在磁盘上的产物"与 manifest 比对。

    判定只认 **size + hash**(mtime 变化记为 `touched`, 不算冲突 —— 网盘/SMB 复制必然改 mtime)。
    `extra`: 存在于产物集合里、但 manifest 没记录的文件 → **警告**(可能是另一端刚写了还没写 manifest,
    也可能是"两端同时在写"的征兆), 由调用方决定是否算错误(`--strict` 时算)。
    ⚠️ `decls` 必须传(或由调用方注入): 平铺步骤的产物集合 = 声明产物, 没有声明表就扫描不到 extra。
    """
    man = load_manifest(cat_root, step)
    if man is None:
        return {"step": int(step), "status": "no_manifest", "missing": [], "changed": [],
                "extra": [], "touched": [], "live": [], "manifest": None}
    res = {"step": int(step), "status": "ok", "missing": [], "changed": [], "extra": [],
           "touched": [], "live": [],
           "manifest": {"role": man.get("role"), "machine": man.get("machine"),
                        "written_at": man.get("written_at"),
                        "state": man.get("state")}}
    recorded: dict[str, dict] = {}
    for sig in man.get("files") or []:
        rel = str(sig.get("rel", "")).replace("\\", "/")
        recorded[rel] = sig
        volatile = rel in VOLATILE_RELS          # 追加型状态文件: 只同步, 不判定
        p = cat_root / rel
        if not p.is_file():
            (res["live"] if volatile else res["missing"]).append(rel)
            continue
        now_size = p.stat().st_size
        if int(sig.get("size", -1)) != now_size:
            if volatile:
                res["live"].append(f"{rel}(大小已变)")
            else:
                res["changed"].append(f"{rel}(大小 {sig.get('size')}→{now_size})")
            continue
        if check_hash:
            cur = file_sig(p, full_max=full_max)
            if cur["sha256"] != sig.get("sha256") or cur["hash_mode"] != sig.get("hash_mode"):
                if volatile:
                    res["live"].append(f"{rel}(内容已变)")
                else:
                    res["changed"].append(f"{rel}(内容已变)")
            elif cur["mtime"] != sig.get("mtime") and not volatile:
                res["touched"].append(rel)
        elif sig.get("mtime") and not volatile and datetime.fromtimestamp(
                p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") != sig["mtime"]:
            res["touched"].append(rel)
    if with_extra:
        for p in step_files(cat_root, step, decls):
            rel = str(p.relative_to(cat_root)).replace("\\", "/")
            if rel not in recorded:
                res["extra"].append(rel)
    if res["missing"] or res["changed"]:
        res["status"] = "changed" if res["changed"] else "missing"
    elif res["extra"]:
        res["status"] = "extra"
    return res


def verify_many(cat_root: Path, steps: list[int], *, check_hash: bool = True,
                full_max: int = HASH_FULL_MAX, strict: bool = False,
                decls: dict[int, list[str]] | None = None) -> list[dict]:
    return [verify_step(cat_root, s, check_hash=check_hash, full_max=full_max, decls=decls)
            for s in steps]


def steps_to_check(cat_root: Path, only: list[int] | None = None) -> list[int]:
    """默认: 有 manifest 的步骤; `only` 指定时用指定的。"""
    if only:
        return sorted({int(x) for x in only})
    steps = []
    for p in sorted(manifest_dir(cat_root).glob("step*.json")):
        try:
            steps.append(int(json.loads(p.read_text(encoding="utf-8")).get("step")))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(set(steps))


# --------------------------------------------------------------------------- 传输

def transfer(cat_root: Path, target: Path, *, direction: str, steps: list[int] | None = None,
             decls: dict[int, list[str]] | None = None, dry_run: bool = False,
             cat: str = "") -> dict:
    """把类目产物 + 步骤状态 + manifest 在 `cat_root` 与 `target` 之间复制。

    * **不删除**目标端的任何文件(绝不用 `--delete` 语义, 避免误删另一端的东西);
    * 只传"选定步骤的产物 + 全部 manifest + step-status.txt";
    * 大文件建议配合 `verify`(`transfer` 不做原子替换, 中断会留下半份文件 —— 这正是 `verify` 要拦的)。
    """
    if decls is None:
        decls = step_declarations(cat)
    from core.paths import STEP_DIR_NAMES

    src_root, dst_root = (cat_root, target) if direction == "push" else (target, cat_root)
    if not src_root.is_dir():
        raise FileNotFoundError(f"源目录不存在: {src_root}")
    files: list[Path] = []
    if steps:
        for s in steps:
            files.extend(step_files(src_root, s, decls))
    else:
        # 未指定步骤: 全类目(排除备份/归档等)
        for p in sorted(src_root.rglob("*")):
            if p.is_file() and not _excluded(p.relative_to(src_root)):
                files.append(p)
    for rel in ALWAYS_SYNC:
        p = src_root / rel
        if p.is_file():
            files.append(p)
    for p in sorted((src_root / STEP_DIR_NAMES[20] / MANIFEST_SUBDIR).glob("*")):
        if p.is_file():
            files.append(p)
    uniq = {str(p.relative_to(src_root)).replace("\\", "/"): p for p in files}
    copied, total = 0, 0
    for rel in sorted(uniq):
        src = uniq[rel]
        dst = dst_root / rel
        size = src.stat().st_size
        total += size
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied += 1
    return {"direction": direction, "from": str(src_root), "to": str(dst_root),
            "files": copied, "bytes": total, "steps": steps or "all", "dry_run": bool(dry_run)}


# --------------------------------------------------------------------------- CLI

def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _resolve_cat_root(args) -> tuple[Path, str, str]:
    """从 --config 解析出类目根目录 / 类目名 / data_root。"""
    from core.config import Config
    from core.paths import Paths

    cfg = Config.load(args.config)
    paths = Paths(cfg)
    return paths.cat_root, str(cfg.cat), str(cfg.get("DATA_ROOT", "data"))


def _status_rows(cat_root: Path, *, check_hash: bool, decls: dict[int, list[str]] | None = None) -> list[dict]:
    rows = []
    for step in range(21):        # 0–20(20=VOC 评论分析, 2026-10-09 新增)
        man = load_manifest(cat_root, step)
        if man is None:
            rows.append({"step": step, "manifest": None})
            continue
        v = verify_step(cat_root, step, check_hash=check_hash, decls=decls)
        rows.append({"step": step, "manifest": man, "verify": v})
    return rows


def cmd_write(args) -> int:
    cat_root, cat, data_root = _resolve_cat_root(args)
    steps = [int(x) for x in args.steps] if args.steps else list(range(21))   # 0–20
    role = resolve_role(args.role)
    out = write_manifests(cat_root, steps, role=role, cat=cat, data_root=data_root)
    if args.json:
        _print_json({"cat": cat, "role": role, "written": [
            {"step": m["step"], "files": m["file_count"], "size": m["total_size"],
             "state": m.get("state"), "missing_declared": m["missing_declared"]} for m in out]})
        return 0
    print(f"类目 {cat} | 本机角色 {role} | 机器 {socket.gethostname()}")
    print(f"写入 manifest: {len(out)} 个 → {manifest_dir(cat_root)}")
    for m in out:
        flag = "  ⚠ 声明了但没产出: " + ", ".join(m["missing_declared"]) if m["missing_declared"] else ""
        print(f"  步骤{m['step']:>2}  {m.get('state', '?'):<10} {m['file_count']:>3} 个文件 "
              f"{m['total_size'] / 1024:>9.1f} KB{flag}")
    print("下一步: 用 push(或你自己的网盘/rsync)把产物同步到另一端, 在另一端跑 verify。")
    return 0


def cmd_verify(args) -> int:
    cat_root, cat, _ = _resolve_cat_root(args)
    only = [int(x) for x in args.steps] if args.steps else None
    steps = steps_to_check(cat_root, only)
    if not steps:
        print(f"类目 {cat}: 还没有任何 manifest —— 请先在**产出端**跑 "
              f"`sync_artifacts.py write --config ...`, 再同步过来。")
        return 2
    results = verify_many(cat_root, steps, check_hash=not args.no_hash, strict=args.strict,
                          decls=step_declarations(cat))
    bad = [r for r in results if r["status"] in ("missing", "changed", "no_manifest")]
    warn = [r for r in results if r["status"] == "extra"]
    if args.json:
        _print_json({"cat": cat, "results": results,
                     "ok": not bad and not (args.strict and warn)})
    else:
        print(f"类目 {cat} | 校验 {len(results)} 个步骤"
              f"{'(size-only)' if args.no_hash else '(size+sha256)'}")
        for r in results:
            m = r["manifest"] or {}
            line = (f"  步骤{r['step']:>2}  {r['status']:<10} "
                    f"{(m.get('role') or '-'):<7} {m.get('machine') or '-':<16} "
                    f"{m.get('written_at') or '-'}")
            print(line)
            for rel in r["missing"]:
                print(f"        ✗ 缺文件: {rel}")
            for rel in r["changed"]:
                print(f"        ✗ 不一致: {rel}")
            for rel in r["extra"]:
                print(f"        ⚠ manifest 未记录(可能是另一端刚写入/尚未写 manifest): {rel}")
            for rel in r["live"]:
                print(f"        · 追加型状态文件已变(只同步不判定): {rel}")
            for rel in r["touched"]:
                print(f"        · mtime 变了但内容一致(同步工具所致, 无害): {rel}")
    if bad:
        print(f"\n✗ 校验未通过: {len(bad)} 个步骤有问题 —— 不要继续跑下游步骤(先补齐同步/重跑产出步骤)。")
        return 2
    if warn and args.strict:
        print(f"\n✗ --strict 下 extra({len(warn)} 步)视为问题。")
        return 2
    print("\n✓ 校验通过: 产物与 manifest 一致。")
    return 0


def cmd_status(args) -> int:
    cat_root, cat, _ = _resolve_cat_root(args)
    rows = _status_rows(cat_root, check_hash=args.hash, decls=step_declarations(cat))
    lc = local_config()
    if args.json:
        _print_json({"cat": cat, "local": {"role": resolve_role(), "target": resolve_target(),
                                           "config_file": bool(lc)},
                     "rows": [{"step": r["step"], "manifest": r["manifest"],
                               "verify": r["verify"]["status"] if r["verify"] else None}
                              for r in rows]})
        return 0
    print(f"类目 {cat} | 本机角色 {resolve_role()} | 目标目录 {resolve_target() or '(未配置)'} "
          f"| sync.local.yaml {'已配置' if lc else '未配置'}")
    print(f"{'步骤':<6}{'状态':<12}{'角色':<8}{'机器':<18}{'写于':<21}{'文件':>6}{'大小':>12}  一致性")
    for r in rows:
        if not r["manifest"]:
            print(f"{r['step']:<6}{'— 无 manifest':<12}")
            continue
        m, v = r["manifest"], r["verify"]
        mark = {"ok": "✓ 一致", "extra": "✓ 一致(+extra)", "missing": "✗ 缺文件",
                "changed": "✗ 内容不一致", "no_manifest": "—"}.get(v["status"], v["status"])
        print(f"{r['step']:<6}{str(m.get('state') or '?'):<12}{str(m.get('role') or '-'):<8}"
              f"{str(m.get('machine') or '-'):<18}{str(m.get('written_at') or '-'):<21}"
              f"{m.get('file_count', 0):>6}{m.get('total_size', 0) / 1024:>11.1f}K  {mark}")
    print("\n提示: 默认只比 size(快); 要验内容跑 `status --hash`(慢但能发现同体积被改)。")
    return 0


def cmd_transfer(args) -> int:
    cat_root, cat, _ = _resolve_cat_root(args)
    target = resolve_target(args.target)
    if not target:
        print("未配置目标目录: 用 --target 或环境变量 AWP_SYNC_TARGET, 或写进 sync.local.yaml。\n"
              "（若你用网盘/rsync 自行同步, 可以只用 write/verify/status, 不需要 push/pull。）")
        return 1
    steps = [int(x) for x in args.steps] if args.steps else None
    out = transfer(cat_root, Path(target), direction=args.cmd, steps=steps,
                   dry_run=args.dry_run, cat=cat)
    if args.json:
        _print_json(out)
        return 0
    verb = "预演" if args.dry_run else "完成"
    print(f"{verb}: {out['direction']} {cat}  {out['from']} → {out['to']}")
    print(f"  {out['files']} 个文件, {out['bytes'] / 1024 / 1024:.2f} MB, 步骤={out['steps']}")
    print("  提醒: 传输中断会留下半份文件 —— 另一端开跑前务必 `sync_artifacts.py verify`。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 公共选项用 `default=SUPPRESS` + 父子解析器共享: 这样 `--config X write` 与 `write --config X`
    # **两种写法都行**(子解析器若带自己的默认值, 会把主解析器已经解析到的值覆盖掉 —— argparse 的老坑)。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="类目配置文件(默认 config.yaml)")
    common.add_argument("--role", default=argparse.SUPPRESS,
                        help="本机角色 local|server(默认读环境/本地配置, 兜底 local)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="机器可读输出")

    p = argparse.ArgumentParser(
        prog="sync_artifacts.py", parents=[common],
        description="分端部署交接层: 产物 manifest(写/校验) + 推送拉取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  # 本地跑完爬取步骤(0/1/9/10/11/12/13)后\n"
               "  python scripts/sync_artifacts.py write  --config \"data/Dog Dryer-US-202608/config.yaml\" "
               "--steps 0 1 9 10 11 12 13 --role local\n"
               "  python scripts/sync_artifacts.py push   --config ... --target \\\\NAS\\awp\n"
               "  # 服务器上开跑前\n"
               "  python scripts/sync_artifacts.py pull   --config ... --target \\\\NAS\\awp\n"
               "  python scripts/sync_artifacts.py verify --config ... --role server\n",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", parents=[common], help="写/刷新 manifest(产出端跑完这步后)")
    w.add_argument("--steps", nargs="*", default=None, help="只写这些步骤(默认 0..19)")
    w.set_defaults(func=cmd_write)

    v = sub.add_parser("verify", parents=[common], help="校验产物与 manifest 是否一致(读取端开跑前)")
    v.add_argument("--steps", nargs="*", default=None, help="只校验这些步骤(默认所有已写 manifest 的)")
    v.add_argument("--no-hash", action="store_true", help="只比体积(快, 但同体积被改发现不了)")
    v.add_argument("--strict", action="store_true", help="把 extra(manifest 未记录的文件)也算问题")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("status", parents=[common], help="一屏看 0..19 的 manifest 与一致性")
    s.add_argument("--hash", action="store_true", help="连内容一起校验(慢)")
    s.set_defaults(func=cmd_status)

    for name, helptext in (("push", "推到目标目录"), ("pull", "从目标目录拉取")):
        t = sub.add_parser(name, parents=[common], help=helptext)
        t.add_argument("--target", default=None, help="目标目录(NAS/SMB/网盘挂载点)")
        t.add_argument("--steps", nargs="*", default=None, help="只传这些步骤的产物")
        t.add_argument("--dry-run", action="store_true", help="只列出将复制什么")
        t.set_defaults(func=cmd_transfer)
    return p


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认 GBK, 中文说明会乱码
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    args = build_parser().parse_args(argv)
    for name, val in (("config", "config.yaml"), ("role", None), ("json", False)):
        if not hasattr(args, name):
            setattr(args, name, val)
    args.role = resolve_role(args.role)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI 不该抛栈
        print(f"✗ {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
