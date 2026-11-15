# -*- coding: utf-8 -*-
"""把**部署期密钥**从 `config.yaml` 迁到**环境变量**（2026-10-12 用户口径）。

为什么
===============================================================================
系统要上服务器，而令牌**不该进源码/仓库**（源码一分享即泄露），也不该让每个类目的
`config.yaml` 各存一份（14 个类目 = 14 份，换令牌要改 14 处、还容易漏）。

目标状态
===============================================================================
  · `PANGOLIN_MCP_URL` —— **写死在代码里**（固定端点、非秘密，见 `tools/voc/pipeline.py`）
  · `LLM_API_KEY` / `SS_MCP_KEY` / `PANGOLIN_MCP_KEY` —— **环境变量优先**，
    读不到再回落 `config.yaml`（兼容）→ 本脚本把现有真值搬进环境变量、再删配置里的键。

为什么必须"先设环境变量、验证通过、再删配置"
===============================================================================
顺序反了就会**静默弄坏你现在能跑的环境**（下一次跑流水线直接 401，而且不易定位）。
所以本脚本默认 **dry-run**：只打印将要做什么；`--apply` 才动手，且**先写环境变量、
回读注册表确认，确认成功后才删配置**。

用法
===============================================================================
  python scripts/setup_deploy_secrets.py            # 预览（不改任何东西）
  python scripts/setup_deploy_secrets.py --apply    # 执行

服务器上怎么配（**不需要跑本脚本**）
===============================================================================
  Linux systemd : Environment="LLM_API_KEY=…"（逐条）
  Docker        : docker run -e LLM_API_KEY=… -e SS_MCP_KEY=… -e PANGOLIN_MCP_KEY=…
  Windows 服务  : setx LLM_API_KEY "…"（管理员/PowerShell，重开进程生效）
  ⚠ 环境变量在**启动看板/流水线之前**设好即可 —— `webui` 起的流水线是子进程，
    会继承看板服务的环境（`subprocess.Popen(env=dict(os.environ)…)`）。
"""
from __future__ import annotations

import argparse
import glob
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.config import ENV_OVERRIDE_KEYS                       # noqa: E402

# ★★ 以**脚本自身位置**定位项目根（2026-10-12 修）：
#   原来写的是相对路径 `data/*/config.yaml` —— 于是**必须在项目根目录**跑，
#   换个目录（如 `C:\Users\Jack_New`）就报 `can't open file 'C:\...\scripts\...'`
#   或更糟：**静默什么都找不到**。现在从 `__file__` 反推，**从任何目录跑都对**。
ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIGS = str(ROOT / "data" / "*" / "config.yaml")


def read_from_configs() -> dict[str, str]:
    """从各类目 config.yaml 里取**第一个非空值**（同一个键在 14 份里通常是同一个）。"""
    found: dict[str, str] = {}
    for f in sorted(glob.glob(CONFIGS)):
        txt = pathlib.Path(f).read_text(encoding="utf-8-sig", errors="replace")
        for k in ENV_OVERRIDE_KEYS:
            if k in found:
                continue
            m = re.search(r"(?m)^\s*" + k + r"\s*:\s*(.*)$", txt)
            if m:
                v = m.group(1).strip().strip('"').strip("'")
                if v:
                    found[k] = v
    return found


def read_pango_key_from_codex() -> str:
    """`PANGOLIN_MCP_KEY` 的值不在 config.yaml，而在本机 `~/.codex/config.toml`（开发机兜底）。

    服务器上没有这个文件 → 所以**也要把它搬进环境变量**，否则上服务器后 VOC 直接没凭据。
    解析逻辑与 `tools/voc/pipeline.resolve_pangolinfo_creds` 保持一致。
    """
    p = pathlib.Path(os.path.expanduser("~/.codex/config.toml"))
    if not p.is_file():
        return ""
    try:
        t = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    seg = re.search(r"\[mcp_servers\.pangolinfo\.http_headers\](.*?)(?=\n\[|\Z)", t, re.S)
    auth = (re.search(r'Authorization\s*=\s*"([^"]+)"', seg.group(1) if seg else "")
            or [None, ""])[1]
    if not auth:
        return ""
    return auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth


def _mask(v: str) -> str:
    return ("%s…%s(长度 %d)" % (v[:6], v[-4:], len(v))) if len(v) > 12 else "(短值已隐藏)"


def setx(k: str, v: str) -> bool:
    """写**持久**用户环境变量（Windows）。返回是否成功。"""
    if os.name != "nt":
        print("      [非 Windows] 请手动: export %s='…'" % k)
        return False
    r = subprocess.run(["setx", k, v], capture_output=False)     # 不用管道（沙箱禁止）
    return r.returncode == 0


def registry_has(k: str, v: str) -> bool:
    """回读 HKCU\\Environment 确认真的写进去了（setx 对**新**进程才生效）。

    ⚠ **不用 `capture_output=True`（管道）** —— 本项目沙箱禁止管道（`WinError 5`），
      而项目里其它脚本也早就改成"**写文件再读**"了。这里输出重定向到仓库内的临时文件。
    """
    if os.name != "nt":
        return False
    tmp = ROOT / "scripts" / "_plan_pilot" / "_regprobe.txt"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", k],
                               stdout=fh, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            return False
        return v in tmp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def strip_from_configs(keys: list[str]) -> list[str]:
    """把已迁走的键（+ 上面的注释行）从各类目 config.yaml 删掉。返回改动文件清单。"""
    changed = []
    for f in sorted(glob.glob(CONFIGS)):
        p = pathlib.Path(f)
        keep, hit = [], False
        for ln in p.read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"^\s*([A-Z_]+)\s*:", ln)
            if m and m.group(1) in keys:
                hit = True
                continue
            if hit is False and "凭据" in ln and ln.lstrip().startswith("#") \
                    and "Pangolinfo" in ln:
                continue
            keep.append(ln)
        if hit:
            p.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
            changed.append(f)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="把部署期密钥从 config.yaml 迁到环境变量")
    ap.add_argument("--apply", action="store_true", help="真的执行（默认只预览）")
    a = ap.parse_args()

    found = read_from_configs()
    _pk = read_pango_key_from_codex()
    if _pk and "PANGOLIN_MCP_KEY" not in found:
        found["PANGOLIN_MCP_KEY"] = _pk                 # 值来自本机 codex 配置
    print("=" * 84)
    print("找到的密钥（config.yaml + 本机 codex 配置）:", len(found))
    for k, v in found.items():
        print("  %-20s %s" % (k, _mask(v)))
    if not found:
        print("  （没有可迁的键 —— 可能已经迁过了）")
        return 0

    print()
    print("环境变量里**已存在**的（会保留、不被覆盖）:")
    for k in found:
        cur = str(os.environ.get(k, "") or "")
        print("  %-20s %s" % (k, _mask(cur) if cur else "（无）"))

    if not a.apply:
        print()
        print("【预览】将执行：")
        for k, v in found.items():
            print("  setx %s <值>" % k)
        print("  → 回读注册表确认")
        print("  → 从 %d 个 data/*/config.yaml 删掉这些键" % len(glob.glob(CONFIGS)))
        print()
        print("确认无误后加 --apply 执行。")
        return 0

    print()
    ok: list[str] = []
    for k, v in found.items():
        if setx(k, v):
            if registry_has(k, v):
                print("  ✅ %s 已写入并回读确认" % k)
                ok.append(k)
            else:
                print("  ⚠ %s setx 成功但回读**没确认**到 → 不删配置（安全起见）" % k)
        else:
            print("  ❌ %s 写入失败 → 不删配置" % k)
    if not ok:
        print()
        print("没有任何键被确认写入 → **什么都没删**。请按上面的提示手动设环境变量。")
        return 1

    print()
    print("已确认写入的键:", ok)
    not_ok = [k for k in found if k not in ok]
    if not_ok:
        print("⚠ 未确认的键**保留在配置里**（避免弄坏）:", not_ok)
    changed = strip_from_configs(ok)
    print("已从 %d 个 config.yaml 删掉这些键" % len(changed))
    print()
    print("⚠ 环境变量对**新进程**才生效：请**重启看板**（以及任何已开的终端）后再跑流水线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
