# -*- coding: utf-8 -*-
"""多类目并行启动器(不改动流水线任何代码, 仅做编排与隔离校验)。

用法:
  python scripts/parallel_cats.py config.A.yaml config.B.yaml [--dry-run] [--check-only] [--stagger N]

行为:
  1. 逐个加载配置文件, 校验"并行隔离三要素"(失败即拒绝启动, 打印原因):
       - CAT 不能为空 / 重复(同 CAT 并发会互写 step-status)
       - **实际生效的** AMZ_PROFILE / SS_PROFILE 两两不同
         (2026-09-15 修正: 走 core/profile_isolate 同一套解析 —— 真实类目 config 都写
          共享默认路径 data/profiles/amz-profile、ss-profile, 但运行期会按类目隔离成
          `*-<CAT>`; 旧版比字面值 → **任意两个真实类目都被误判"Profile 冲突"而拒绝启动**)
       - SS_CDP_URL 相同 -> **警告**(2026-09-15 起卖家精灵浏览器有跨进程全局锁
         core/ss_lock.py: 共用同一个 Chrome 时自动排队串行, 可以并行, 但步骤 0/11/12/13
         会互相等待)
       - SS_ACCOUNTS 账号重叠 -> 警告(共享 50 次/天配额, 并行会互抢; --strict 时视为错误)
     LIST_URLS 规范格式由流水线 step0 启动前预检把关, 这里不重复判定。
  2. 并发启动每个类目: python main.py run --config <X> (可加 --dry-run 透传),
     各自输出写入 logs/parallel/<时间戳>/N-<类目>.log(不经过管道, 便于 Windows 下稳定)。
  3. 全部结束后打印汇总(每类目 退出码/耗时/结果), 任一失败则整体退出码 1。

提示: 看板(webui)现已支持同时跑多个类目(2026-09-15) —— 习惯用面板的话不必用本脚本;
      本脚本用于命令行/批处理场景。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
# 直接用 `python scripts/parallel_cats.py` 时 sys.path[0] 是 **scripts/** 而不是项目根,
# `import core.*` 会失败 → 这里显式把项目根放进 sys.path(2026-09-15: 否则"实际生效
# profile"解析整段被 except 吞掉, 退回比字面值 → 又变成"任意两个类目都冲突")。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_AMZ = "data/amz-profile"
DEFAULT_SS = "data/ss-profile"
LOG_BASE = ROOT / "logs" / "parallel"


def _load_cfg(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("缺少 PyYAML, 请先 pip install pyyaml")
    with open(path, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f) or {}


def _cfg_str(cfg: dict, key: str, default: str = "") -> str:
    v = cfg.get(key)
    return str(v).strip() if v not in (None, "") else default


def _accounts_of(cfg: dict) -> list[str]:
    out = []
    for a in cfg.get("SS_ACCOUNTS") or []:
        if isinstance(a, dict) and str(a.get("account") or "").strip():
            out.append(str(a["account"]).strip())
    acct = _cfg_str(cfg, "SS_ACCOUNT")
    if acct:
        out.append(acct)
    return out


def _effective_profiles(cfg_path: Path) -> tuple[str, str]:
    """按**运行期真实规则**算出该类目实际用的 (亚马逊 profile, 卖家精灵 profile)。

    为什么不能直接比配置里的 AMZ_PROFILE/SS_PROFILE(2026-09-15 修正):
      真实类目 config 都显式写 `data/profiles/amz-profile`、`data/profiles/ss-profile`
      —— 看着"两两相同", 但流水线运行期会把**共享默认路径按类目隔离**成
      `amz-profile-<CAT>` / `ss-profile-<CAT>`(见 core/profile_isolate.py)。旧版启动器
      直接比字面值, 于是**任意两个真实类目都会被判成"Profile 冲突"而拒绝启动**。
      这里改成走同一套解析函数, 判的是"实际会不会撞"。
    """
    try:
        from core.config import Config
        from core.profile_isolate import resolve_amz_profile_dir, resolve_ss_profile_dir
    except Exception:  # pragma: no cover - 环境缺依赖时退回字面值
        return _cfg_str(_load_cfg(cfg_path), "AMZ_PROFILE", DEFAULT_AMZ), \
               _cfg_str(_load_cfg(cfg_path), "SS_PROFILE", DEFAULT_SS)
    cfg = Config.load(str(cfg_path))
    # profile 根目录与运行期同源(core.paths.Paths.profile_root): 配置的 PROFILE_DIR(默认
    # data/profiles), 相对路径按当前工作目录(项目根)解析
    root = Path(str(getattr(cfg, "profile_dir", "") or "data/profiles")).resolve()
    try:
        amz = str(resolve_amz_profile_dir(cfg, root))
    except Exception:  # noqa: BLE001
        amz = cfg.amz_profile_dir
    try:
        ss = str(resolve_ss_profile_dir(cfg, root))
    except Exception:  # noqa: BLE001
        ss = cfg.ss_profile_dir
    return amz, ss


def validate(configs: list[Path]) -> tuple[list[dict], list[str]]:
    """校验并行隔离; 返回 (每份配置的有效信息, 警告列表); 硬冲突抛 RuntimeError。

    硬冲突(拒绝启动): CAT 为空/模板/重复、缺 LIST_URLS、**实际生效的** profile 撞车。
    改为警告(2026-09-15): SS_CDP_URL 相同 —— 卖家精灵浏览器现在有**跨进程全局锁**
    (`core/ss_lock.py`), 多个类目共用同一个调试端口的 Chrome 时会自动排队串行, 不再
    互相踩; 账号重叠同样只是警告(共享导出配额, 串行后按配额顺序消耗)。
    """
    infos = []
    errors: list[str] = []
    warns: list[str] = []
    seen_cat: dict[str, str] = {}
    for p in configs:
        cfg = _load_cfg(p)
        cat = _cfg_str(cfg, "CAT")
        amz, ss = _effective_profiles(p)
        cdp = _cfg_str(cfg, "SS_CDP_URL")
        infos.append({"path": p, "cat": cat, "amz": amz, "ss": ss, "cdp": cdp,
                      "accounts": _accounts_of(cfg), "dry": bool(cfg.get("DRY_RUN", False))})
        if not cat:
            errors.append(f"{p.name}: 配置缺少 CAT(类目名)")
        elif "template" in cat.lower():
            errors.append(f"{p.name}: CAT 仍是模板占位值, 请复制模板后填写真实类目: {cat}")
        if not _cfg_str(cfg, "LIST_URLS"):
            errors.append(f"{p.name}: 缺少 LIST_URLS(规范 Best Sellers 短链接, step0 预检会阻断)")
    # 两两冲突(只对解析成功的配置)
    for i in range(len(infos)):
        for j in range(i + 1, len(infos)):
            a, b = infos[i], infos[j]
            if a["cat"] and a["cat"] == b["cat"]:
                errors.append(f"CAT 重复(会互写 step-status): {a['path'].name} 与 {b['path'].name} 都是 {a['cat']}")
            if a["amz"] == b["amz"]:
                errors.append(f"亚马逊 Profile 实际撞车(Chrome 互斥): {a['path'].name} 与 {b['path'].name} -> {a['amz']}")
            if a["ss"] == b["ss"]:
                errors.append(f"卖家精灵 Profile 实际撞车(Chrome 互斥): {a['path'].name} 与 {b['path'].name} -> {a['ss']}")
            if a["cdp"] and b["cdp"] and a["cdp"] == b["cdp"]:
                # 2026-09-15(用户"给不同类目配不同账号"): 同端口 + **不同账号** = 硬冲突 ——
                # 一个 Chrome 只能保持一份卖家精灵登录态, 后跑的会用到前一个账号的配额与
                # 数据(静默出错数据)。同端口同账号只是排队(由 core/ss_lock 的资源锁串行)。
                from core.ss_lock import accounts_of, browser_conflict, primary_account
                _ca, _cb = _load_cfg(a["path"]), _load_cfg(b["path"])
                why = browser_conflict(a["cdp"], primary_account(_ca), accounts_of(_ca),
                                       b["cdp"], primary_account(_cb), accounts_of(_cb))
                if why:
                    errors.append(f"{a['path'].name} 与 {b['path'].name}: {why}")
                else:
                    warns.append(f"SS_CDP_URL 相同({a['cdp']}, 且账号相同): 共用同一个卖家精灵 "
                                 f"Chrome —— 由资源锁**自动排队串行**(同一份登录态不能同时用), "
                                 f"步骤 0/11/12/13 会互相等待; 想真并行请给其中一个换端口")
    if errors:
        raise RuntimeError("并行隔离校验未通过:\n  - " + "\n  - ".join(errors))
    return infos, warns


def main() -> int:
    ap = argparse.ArgumentParser(description="多类目并行启动器(每类目独立 Profile/CDP/账号校验)")
    ap.add_argument("configs", nargs="+", help="要并行运行的配置文件(项目根, 如 config.A.yaml)")
    ap.add_argument("--dry-run", action="store_true", help="透传 --dry-run 给每个 main.py run")
    ap.add_argument("--check-only", action="store_true", help="只校验隔离性, 不真正启动")
    ap.add_argument("--strict", action="store_true", help="卖家精灵账号重叠也视为错误(默认仅警告)")
    ap.add_argument("--stagger", type=float, default=2.0, help="各任务启动间隔秒(错峰登录/请求), 默认 2")
    args = ap.parse_args()

    paths = [Path(c) for c in args.configs]
    missing = [str(p) for p in paths if not (ROOT / p).is_file()]
    if missing:
        print("配置文件不存在(需位于项目根): " + ", ".join(missing))
        return 2
    infos, val_warns = validate(paths)
    warn = list(val_warns)
    # 账号重叠警告
    for i in range(len(infos)):
        for j in range(i + 1, len(infos)):
            ov = sorted(set(infos[i]["accounts"]) & set(infos[j]["accounts"]))
            if ov:
                msg = f"卖家精灵账号重叠(共享 50 次/天配额): {infos[i]['path']} 与 {infos[j]['path']} -> {', '.join(ov)}"
                warn.append(msg)
    if warn and args.strict:
        print("并行隔离校验未通过(--strict):\n  - " + "\n  - ".join(warn))
        return 2
    print("=== 并行任务清单 ===")
    for it in infos:
        print(f"  {it['path'].name:28s} CAT={it['cat']}")
        print(f"      AMZ_PROFILE={it['amz']}  SS_PROFILE={it['ss']}  CDP={it['cdp'] or '(自动有头)'}"
              f"  账号数={len(it['accounts'])}  dry={'DRY' if (it['dry'] or args.dry_run) else 'REAL'}")
    for w in warn:
        print("  [warn] " + w)
    if args.check_only:
        print("校验通过(未启动)。")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rundir = LOG_BASE / f"run-{ts}"
    rundir.mkdir(parents=True, exist_ok=True)
    procs = []
    for i, it in enumerate(infos):
        logf = rundir / f"{i + 1:02d}-{it['cat'].replace(' ', '_')}.log"
        # ⚠️ 必须传**相对项目根的完整路径**(如 data/<CAT>/config.yaml) —— 旧版传
        # `path.name`(只有 "config.yaml"), 子进程 cwd=项目根 → main.py 找不到文件,
        # 每个任务 1 秒内以退出码 2 失败(实测踩到并修正, 2026-09-15)。
        cfg_arg = it["path"]
        try:
            cfg_arg = it["path"].resolve().relative_to(ROOT)
        except Exception:  # noqa: BLE001
            pass
        cmd = [sys.executable, "-u", "-X", "utf8", "main.py", "run", "--config", str(cfg_arg)]
        if args.dry_run:
            cmd.append("--dry-run")
        print(f"[启动] {it['cat']}: {' '.join(cmd)}\n        日志: {logf}")
        with open(logf, "wb") as fh:
            procs.append((it, logf, subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT), time.monotonic()))
        if i < len(infos) - 1 and args.stagger > 0:
            time.sleep(args.stagger)
    print("全部已启动, 等待完成 ...")
    results = []
    bad = 0
    for it, logf, proc, t0 in procs:
        rc = proc.wait()
        el = time.monotonic() - t0   # 从该任务实际启动时刻计(不是开始等待的时刻)
        ok = rc == 0
        bad += 0 if ok else 1
        results.append((it["cat"], rc, el, ok, logf))
    print("\n=== 汇总 ===")
    for cat, rc, el, ok, logf in results:
        print(f"  [{'OK ' if ok else 'FAIL'}] {cat:32s} 退出码={rc}  耗时={el:.0f}s  日志={logf.name}")
    print("全部成功。" if bad == 0 else f"{bad} 个任务失败(查看上方日志)。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
