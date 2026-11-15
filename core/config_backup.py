# -*- coding: utf-8 -*-
"""配置文件集中备份与治理(2026-09 终版规则)。

目录: <项目根>/.webui/config-backups/
命名(新): <类目名清洗>-v<版本号>.bak        (2026-09-25: 去掉日期/具体时间,
          版本号按类目单调递增已保证唯一; 日期与时间在文件头部与日志中记载)
规则:
  - **有改动才备份**: 调用方传"将写入的新内容"与当前文件比较; 一致则不生成
    (仍是原数据)并记日志; 不一致才生成新版本。
  - 头部说明: # 源文件 / # CAT / # 版本 / # 备份原因 / # 备份时间(日期时间落这里)
  - 每类目保留最近 N 份(默认 10, `WEBUI_KEEP_CONFIG_BACKUPS` 可调)
  - **清理留痕(2026-09-13)**: prune() 支持 log 回调, `default_prune_logger()` 会把
    "已删除 <文件名>(CAT/原因/原目录)" 追加到 `.webui/config-backups/prune.log` 与
    对应类目的 `20_备份与监控/pipeline-progress.log`; webui 自动清理也走它。
  - 事件日志: log 回调输出 [备份事件] 生成/跳过/失败
兼容: 旧命名 <stem>-<cat>-<YYYYMMDD>-<HHMMSS>.bak 仍可 list/prune/restore。
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = PROJECT_ROOT / ".webui" / "config-backups"   # 兼容/兜底目录(非类目文件与历史备份)

# 新/旧备份文件命名
# 新: <cat>-v<N>.bak
_NEW_RE = re.compile(r"^(?P<cat>.+?)-v(?P<v>\d+)\.bak$")
# 旧(兼容): <stem>-<cat>-<YYYYMMDD>-<HHMMSS>(-N).bak
_OLD_RE = re.compile(
    r"^(?P<stem>.+?)-(?P<cat>.+)-(?P<ts>\d{8}-\d{6})(?:-(?P<n>\d+))?\.bak$"
)


def backup_dirs() -> list[Path]:
    """所有备份目录: 类目各自 data/<CAT>/config-backups(2026-09 起主存放) + 集中兜底目录。"""
    dirs = [BACKUP_DIR]
    data_root = PROJECT_ROOT / "data"
    if data_root.is_dir():
        for d in sorted(data_root.iterdir()):
            if d.is_dir() and d.name != "archive" and not d.name.startswith("."):
                sub = d / "config-backups"
                if sub.is_dir():
                    dirs.append(sub)
    return dirs


def backup_paths() -> list[Path]:
    out: list[Path] = []
    for d in backup_dirs():
        if d.is_dir():
            out.extend(d.glob("*.bak"))
    return sorted(out, key=lambda p: p.name)


def keep_count() -> int:
    return max(3, int(os.environ.get("WEBUI_KEEP_CONFIG_BACKUPS", "10") or "10"))


def _safe_token(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", text).strip("-")
    return cleaned or "nocat"


def _yaml_cat(path: Path) -> str:
    try:
        import yaml  # noqa: PLC0415
        with open(path, "r", encoding="utf-8-sig") as fh:
            return str((yaml.safe_load(fh) or {}).get("CAT", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _header_value(path: Path, prefix: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(10):
                line = fh.readline()
                if not line:
                    break
                if line.startswith(prefix):
                    return line.strip().lstrip("# ").replace(prefix.replace("# ", "").strip(), "", 1).strip()
    except OSError:
        pass
    return ""


def _mtime_ts(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return ""


def parse_name(name: str) -> dict | None:
    """解析备份文件名(新/旧)。新: {cat, version, old:False}; 旧: {stem,cat,ts,old:True}。"""
    m = _NEW_RE.match(name)
    if m:
        return {"cat": m.group("cat"), "version": int(m.group("v")), "old": False}
    m = _OLD_RE.match(name)
    if m:
        return {"cat": m.group("cat"), "stem": m.group("stem"),
                "ts": m.group("ts"), "old": True}
    return None


def _next_version(cat_token: str) -> int:
    mx = 0
    for p in backup_paths():
        info = parse_name(p.name)
        if info and not info["old"] and info["cat"] == cat_token and info["version"]:
            mx = max(mx, info["version"])
    return mx + 1


def make_backup(path: Path, reason: str = "webui 保存前自动备份",
                log=None, changed_to: str | None = None) -> Path | None:
    """版本化备份(有改动才生成)。返回新备份路径; 无改动/失败返回 None。

    changed_to: 将写入文件的新内容(用于"内容无变化则跳过"); None=不比较(如恢复前备份)。
    log: log(msg) 回调, 用于记录备份事件(webui 控制台/CLI 终端)。
    """
    src = Path(path)
    if not src.is_file():
        if log:
            log(f"[备份事件] 跳过: 文件不存在 {src}")
        return None
    if ".webui" in src.parts and "runs" in src.parts:
        if log:
            log(f"[备份事件] 跳过: {src} 为运行快照, 本身即留档")
        return None
    if changed_to is not None:
        try:
            cur = src.read_text(encoding="utf-8-sig")
        except OSError:
            cur = ""
        if _norm(cur) == _norm(changed_to):
            if log:
                log(f"[备份事件] 配置无实际改动(内容与当前一致), 不生成新备份, 仍为原数据: {src.name}")
            return None
    cat = _yaml_cat(src)
    cat_token = _safe_token(cat) or "nocat"
    now = datetime.now()
    version = _next_version(cat_token)
    # 备份随类目放(2026-09): 类目配置(data/<CAT>/config.yaml) -> data/<CAT>/config-backups/;
    # 其它(模板/非类目文件)仍进集中兜底目录。
    # 2026-09-16 修: 原来直接比 `src.parent.parent == PROJECT_ROOT/"data"` —— 调用方传**相对路径**
    # (如 `data/<CAT>/config.yaml`, 脚本/CLI 常见)时两侧不相等, 类目配置会被误判成"非类目文件"
    # 落到集中兜底目录(实测: 批量补键时 12 个类目的备份全进了 .webui/config-backups/)。
    # 现两侧都 resolve 后比较, 与调用方传相对/绝对路径无关(restore 按头部 CAT 反查目标, 不受影响)。
    try:
        _is_cat_cfg = src.resolve().parent.parent == (PROJECT_ROOT / "data").resolve()
    except OSError:
        _is_cat_cfg = False
    if _is_cat_cfg:
        bdir = src.parent / "config-backups"
    else:
        bdir = BACKUP_DIR
    bak = bdir / f"{cat_token}-v{version}.bak"
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8-sig")
        header = (
            f"# 源文件: {src.name}\n"
            f"# CAT: {cat or '(未识别)'}\n"
            f"# 版本: v{version}\n"
            f"# 备份原因: {reason}\n"
            f"# 备份时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        bak.write_text(header + text, encoding="utf-8", newline="\n")
    except OSError as exc:
        if log:
            log(f"[备份事件] 失败: {exc}")
        return None
    if log:
        log(f"[备份事件] 已生成备份 v{version}: {bak}  (原因={reason}, CAT={cat or '?'}, "
            f"时间={now.strftime('%Y-%m-%d %H:%M:%S')})")
    return bak


def _norm(text: str) -> str:
    return "\n".join(x.strip() for x in text.splitlines() if x.strip()).rstrip()


def prune(dry: bool = False, keep: int | None = None, log=None) -> list[Path]:
    """按类目保留最近 keep 份(按文件修改时间), 删除更旧; 返回被删文件。

    log: 可选回调, 每清理一份调用一次, 收到 dict(name/cat/reason/dir/path/dry) ——
         用于把"谁被清理了"落盘(见 default_prune_logger)。dry=True 时不删除但**仍回调**
         (dict["dry"]=True), 便于 --dry-run 预览"将删除什么"。
    """
    if keep is None:
        keep = keep_count()
    removed: list[Path] = []
    groups: dict[str, list[Path]] = {}
    for p in backup_paths():
        info = parse_name(p.name)
        if not info:
            continue
        groups.setdefault(f"new:{info['cat']}" if not info["old"] else f"old:{info['stem']}|{info['cat']}", []).append(p)
    for paths in groups.values():
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in paths[keep:]:
            detail = {
                "name": old.name,
                "cat": _header_value(old, "# CAT:"),
                "reason": _header_value(old, "# 备份原因:"),
                "dir": old.parent.name,
                "path": str(old),
                "dry": bool(dry),
            }
            if not dry:
                try:
                    old.unlink()
                except OSError:
                    continue
            removed.append(old)
            if log:
                try:
                    log(detail)
                except Exception:  # noqa: BLE001 - 日志失败不影响清理
                    pass
    return removed


# 清理记录落盘位置: 必须**调用时**由当前 BACKUP_DIR 计算(不能在导入时冻结成常量 ——
# 否则测试/换根目录时会写到真实 .webui/config-backups/ 去)
def prune_log_path() -> Path:
    return BACKUP_DIR / "prune.log"


def _prune_log_targets(detail: dict) -> list[Path]:
    out = [prune_log_path()]
    cat = str(detail.get("cat") or "").strip()
    if cat:
        out.append(PROJECT_ROOT / "data" / cat / "20_备份与监控" / "pipeline-progress.log")
    return out


def default_prune_logger():
    """默认清理日志: 追加到 .webui/config-backups/prune.log 与对应类目的 pipeline-progress.log。

    为什么需要: 保留策略会**静默**删掉旧版本备份, 事后没人能回答"某个 vN 是什么时候没的"
    (实战: Air Purifiers 的 v1~v3、Cat Fountains 的 v1/v2 无声消失, 只能靠时间戳反推)。
    """
    def _log(detail: dict) -> None:
        if detail.get("dry"):
            return
        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [备份清理] 已删除 {detail.get('name')}"
            f"(CAT={detail.get('cat') or '?'}, 原因={detail.get('reason') or '?'}, "
            f"原目录={detail.get('dir') or '?'})\n"
        )
        for target in _prune_log_targets(detail):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
            except OSError:
                continue

    return _log


def list_backups() -> list[dict]:
    """全部备份(按时间倒序): file/cat/version/old/size/reason/cat_raw/mtime。"""
    out = []
    for p in backup_paths():
        info = parse_name(p.name) or {}
        out.append({
            "file": p.name,
            "path": str(p),
            "cat": info.get("cat", ""),
            "stem": info.get("stem", ""),
            "cat_raw": _header_value(p, "# CAT:"),
            "version": info.get("version"),
            "old": bool(info.get("old")),
            "size": p.stat().st_size,
            "reason": _header_value(p, "# 备份原因:"),
            "mtime": _mtime_ts(p),
            "ts": _header_value(p, "# 备份时间:") or info.get("ts", "") or _mtime_ts(p),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def resolve_source(cat_token_or_stem: str, cat_raw: str | None = None) -> Path | None:
    """按头部真实 CAT 或文件名 token 反查当前配置(data/<CAT>/config.yaml 优先)。"""
    if cat_raw:
        cand = PROJECT_ROOT / "data" / cat_raw / "config.yaml"
        if cand.is_file():
            return cand
    for cand in (PROJECT_ROOT / f"{cat_token_or_stem}.yaml",
                 PROJECT_ROOT / "configs" / f"{cat_token_or_stem}.yaml"):
        if cand.is_file():
            return cand
    return None


def restore_backup(backup: Path, to: Path | None = None, log=None) -> Path:
    """把备份恢复为配置文件; 恢复前先自动备份当前文件; 返回恢复目标路径。"""
    src = Path(backup)
    info = parse_name(src.name)
    if not info:
        raise ValueError(f"不是标准备份文件名: {src.name}")
    cat_raw = _header_value(src, "# CAT:")
    target = to or resolve_source(info.get("stem") or info["cat"], cat_raw)
    if target is None:
        raise ValueError(
            f"找不到该备份对应的当前配置文件(已查 data/<CAT>/config.yaml 等), 请用 --to 指定")
    make_backup(target, reason=f"恢复备份 {src.name} 前自动备份", log=log)
    try:
        lines = src.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    except UnicodeDecodeError:
        lines = None
    if lines is not None:
        while lines and lines[0].startswith("# "):
            lines.pop(0)
        target.write_text("".join(lines), encoding="utf-8", newline="\n")
    else:
        shutil.copy2(src, target)
    return target
