# -*- coding: utf-8 -*-
"""配置体检: 校对所有类目配置与备份配置的**格式**与**参数(键)数量一致性**(只读, 不修改任何文件)。

检查项:
  ① 类目配置 data/<CAT>/config.yaml
     - YAML 能否解析 / 能否被 Config.load 读取(含 BOM 容忍路径)
     - 是否带 UTF-8 BOM(容忍但不规范) / 顶层键是否重复 / 是否用到制表符缩进
     - CAT 是否等于所在目录名(2026-09-13 防呆口径)
     - DATA_ROOT 是否为 data; 月份格式是否合规
     - SS_ACCOUNTS 结构(列表 + 每项有非空 account)
     - 关键键是否为空(LLM/MCP/账号/COOKIE) —— DRY_RUN 模拟配置放宽
     - 键类型是否与 core/config.py DEFAULTS 的默认类型一致(整型/浮点/布尔)
     - **键集合**与"全体类目并集"对比 → 列出缺失/多余的键(参数数量是否一致)
  ② 备份配置 data/*/config-backups/*.bak 与 .webui/config-backups/*.bak
     - 新命名(带 5 行头部) / 旧命名(无头部) 分类
     - 跳过头部后 YAML 能否解析; **键数量**与当前类目配置对比(缺/多哪些键)
  ③ 汇总: 按严重度列出问题 + "参数数量是否都一样"的结论

用法:
  python scripts/check_configs.py                 # 全部
  python scripts/check_configs.py --cat 片段      # 只看某类目(含其备份)
  python scripts/check_configs.py --quiet         # 只打印问题与汇总
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.category_config import check_cat_dir_consistency  # noqa: E402
from core.config import Config                              # noqa: E402

HEADER_RE = re.compile(r"^# (源文件|CAT|版本|备份原因|备份时间):")
TOPKEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
TREND_RE = re.compile(r"^\d{6}$")
_SEP = "=" * 100

# 关键键: 空值即风险(DRY_RUN 模拟配置只提示不报错)
CRITICAL = ["LLM_API_KEY", "LLM_BASE_URL", "SS_MCP_URL", "SS_MCP_KEY",
            "SS_ACCOUNT", "SS_PASSWORD", "COOKIE"]

TYPES = {k: type(v) for k, v in Config.DEFAULTS.items() if isinstance(v, (int, float, bool))}


def read_text(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig", errors="replace"), bom


def strip_header(text: str) -> tuple[str, int]:
    lines = text.splitlines()
    n = 0
    while n < len(lines) and HEADER_RE.match(lines[n]):
        n += 1
    return "\n".join(lines[n:]), n


def top_keys(text: str) -> tuple[list[str], list[str]]:
    """返回 (顶层键列表, 重复键列表)。"""
    keys = [m.group(1) for ln in text.splitlines() if (m := TOPKEY_RE.match(ln))]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    return keys, dup


def parse_yaml(text: str) -> tuple[dict | None, str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"YAML 解析失败: {str(exc).splitlines()[0]}"
    if data is None:
        return None, "解析结果为空"
    if not isinstance(data, dict):
        return None, f"顶层不是映射(而是 {type(data).__name__})"
    return data, ""


def audit_current(cfg: Path) -> dict:
    text, bom = read_text(cfg)
    data, err = parse_yaml(text)
    keys, dup = top_keys(text)
    out = {"path": cfg, "bom": bom, "error": err, "dup": dup, "keys": keys, "data": data or {},
           "issues": [], "warns": []}
    if err:
        out["issues"].append(err)
        return out
    cat = str(data.get("CAT", "") or "").strip()
    if data.get("DATA_ROOT") and str(data["DATA_ROOT"]).strip() != "data":
        out["warns"].append(f"DATA_ROOT={data['DATA_ROOT']}(非常规值)")
    mismatch = check_cat_dir_consistency(cfg, cat, str(data.get("DATA_ROOT") or "data"))
    if mismatch:
        out["issues"].append("CAT 与所在目录名不一致")
    if dup:
        out["issues"].append("顶层键重复: " + ", ".join(dup))
    if "\t" in text:
        out["warns"].append("含制表符(Tab)缩进")
    if bom:
        out["warns"].append("带 UTF-8 BOM(可读但不规范)")
    for k in ("TRAFFIC_MONTH", "COMPETITOR_MONTH"):
        v = str(data.get(k) or "").strip()
        if v and v != "最近30天" and not MONTH_RE.match(v):
            out["issues"].append(f"{k} 格式异常: {v}")
    v = str(data.get("TREND_MONTH") or "").strip()
    if v and not TREND_RE.match(v):
        out["issues"].append(f"TREND_MONTH 格式异常: {v}")
    accts = data.get("SS_ACCOUNTS")
    if accts is not None and not isinstance(accts, list):
        out["issues"].append("SS_ACCOUNTS 不是列表")
    elif isinstance(accts, list):
        bad = [i for i, it in enumerate(accts) if not isinstance(it, dict)
               or not str(it.get("account", "") or "").strip()]
        if bad:
            out["issues"].append(f"SS_ACCOUNTS 第 {bad} 项缺 account")
    dry = bool(data.get("DRY_RUN")) or str(data.get("DRY_RUN", "")).lower() in ("true", "1", "yes")
    for k in CRITICAL:
        if k in data and not str(data.get(k) or "").strip():
            (out["warns"] if dry else out["issues"]).append(f"{k} 为空{' (模拟配置, 仅提示)' if dry else ''}")
    bad_type = []
    for k, want in TYPES.items():
        if k not in data or data[k] is None:
            continue
        got = data[k]
        if isinstance(got, str):
            bad_type.append(f"{k}=字符串{want.__name__}")
        elif want is bool and not isinstance(got, bool):
            bad_type.append(f"{k}={type(got).__name__}")
        elif want is int and (not isinstance(got, int) or isinstance(got, bool)):
            bad_type.append(f"{k}={type(got).__name__}")
        elif want is float and not isinstance(got, (int, float)):
            bad_type.append(f"{k}={type(got).__name__}")
    if bad_type:
        out["issues"].append("类型异常: " + ", ".join(bad_type))
    try:
        Config.load(cfg)
    except Exception as exc:  # noqa: BLE001
        out["issues"].append(f"Config.load 失败: {exc}")
    return out


def audit_backup(path: Path, cur_keys: set[str] | None) -> dict:
    text, bom = read_text(path)
    body, hdr = strip_header(text)
    data, err = parse_yaml(body)
    keys, dup = top_keys(body)
    out = {"path": path, "hdr": hdr, "bom": bom, "error": err, "dup": dup,
           "keys": set(keys), "issues": [], "data": data or {}}
    if err:
        out["issues"].append(err)
    if dup:
        out["issues"].append("顶层键重复: " + ", ".join(dup))
    if hdr == 0:
        out["issues"].append("无头部(旧命名格式, 属历史留档)")
    elif hdr < 5:
        out["issues"].append(f"头部不完整({hdr}/5 行)")
    if cur_keys is not None and not err:
        miss = sorted(cur_keys - set(keys))
        extra = sorted(set(keys) - cur_keys)
        out["miss"], out["extra"] = miss, extra
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="配置体检(只读)")
    ap.add_argument("--cat", default="", help="只看含该片段的类目")
    ap.add_argument("--quiet", action="store_true", help="只打印问题与汇总")
    args = ap.parse_args()

    cat_dirs = sorted([d for d in (ROOT / "data").iterdir()
                       if d.is_dir() and d.name != "archive" and (d / "config.yaml").is_file()])
    if args.cat:
        cat_dirs = [d for d in cat_dirs if args.cat.lower() in d.name.lower()]

    currents = []
    for d in cat_dirs:
        r = audit_current(d / "config.yaml")
        r["cat"] = str(r["data"].get("CAT") or d.name)
        r["dir"] = d.name
        dr = r["data"].get("DRY_RUN")
        r["sim"] = dr is True or str(dr or "").lower() in ("true", "1", "yes")
        currents.append(r)

    real = [r for r in currents if not r["sim"] and not r["error"]]
    sims = [r for r in currents if r["sim"]]
    keymap = {r["cat"]: set(r["keys"]) for r in currents if not r["error"]}
    r_union = set().union(*[set(r["keys"]) for r in real]) if real else set()
    r_inter = set.intersection(*[set(r["keys"]) for r in real]) if real else set()

    print(_SEP)
    print("① 类目配置检查(格式 + 键数量)")
    print(_SEP)
    print(f"{'类目':<48}{'键数':>5}  {'类型':<6}{'YAML':<6}{'BOM':<5}{'重复':<5}问题")
    for r in currents:
        yaml_ok = "OK" if not r["error"] else "FAIL"
        kind = "模拟" if r["sim"] else "真实"
        print(f"{r['dir'][:46]:<48}{len(r['keys']):>5}  {kind:<6}{yaml_ok:<6}"
              f"{('是' if r['bom'] else '否'):<5}{('是' if r['dup'] else '否'):<5}"
              f"{'; '.join(r['issues']) or '-'}")
        for w in r["warns"]:
            print(f"{'':<48}{'':>5}  ⚠ {w}")
    print(f"\n  【真实类目 {len(real)} 个】键集合: 并集 {len(r_union)} / 交集 {len(r_inter)}")
    if real and len(r_inter) == len(r_union):
        print(f"  ✅ 真实类目参数数量完全一致(均 {len(r_inter)} 个键)")
    else:
        for r in real:
            miss = sorted(r_union - set(r["keys"]))
            extra = sorted(set(r["keys"]) - r_union)
            if miss or extra:
                print(f"  · {r['dir']}: 缺 {miss or '无'} / 多 {extra or '无'}")
    for r in sims:
        print(f"  · {r['dir']}: {len(r['keys'])} 键(dry-run 模拟配置, 设计上的最小集, 不参与一致性判定)")

    print()
    print(_SEP)
    print("② 备份配置检查(格式 + 键数量 vs 当前配置)")
    print(_SEP)
    bak_dirs = [ROOT / ".webui" / "config-backups"] + [d / "config-backups" for d in cat_dirs]
    bak_dirs += [p for p in (ROOT / "data").glob("*/config-backups") if p not in bak_dirs]
    rows, bad, hist = [], [], []
    for bdir in sorted(set(bak_dirs)):
        if not bdir.is_dir():
            continue
        for bak in sorted(bdir.glob("*.bak")):
            # 归属: 同目录若为 data/<CAT>/config-backups 则对应类目; 否则按头部 CAT 找
            head_cat = ""
            text, _ = read_text(bak)
            for ln in text.splitlines()[:6]:
                if ln.startswith("# CAT:"):
                    head_cat = ln.split(":", 1)[1].strip()
            owner = keymap.get(head_cat) or keymap.get(bdir.parent.parent.name) or None
            r = audit_backup(bak, owner)
            r["owner"] = head_cat or bdir.parent.parent.name
            rows.append(r)
            if r["issues"]:
                # "无头部" = 2026-09 版本化之前的历史命名(预期); 其余(头部不全/YAML坏/重复键)才算问题
                (hist if all("无头部" in i for i in r["issues"]) else bad).append(r)

    print(f"{'备份文件':<58}{'键数':>5}  {'头部':<5}说明")
    for r in rows:
        print(f"{r['path'].name[:56]:<58}{len(r['keys']):>5}  {str(r['hdr']) + '/5':<5}"
              f"{'; '.join(r['issues']) or '-'}")
        if r.get("miss"):
            print(f"{'':<58}{'':>5}  缺(相对当前配置 {len(r['miss'])} 个): {r['miss'][:8]}"
                  f"{' …' if len(r['miss']) > 8 else ''}")
        if r.get("extra"):
            print(f"{'':<58}{'':>5}  多: {r['extra'][:8]}")

    print()
    print(_SEP)
    print("③ 汇总")
    print(_SEP)
    n_issue = sum(1 for r in currents if r["issues"]) + len(bad)
    print(f"  类目配置 {len(currents)} 个(真实 {len(real)} + 模拟 {len(sims)}): "
          f"有问题的 {sum(1 for r in currents if r['issues'])} 个")
    print(f"  备份配置 {len(rows)} 个: 有问题的 {len(bad)} 个; "
          f"旧命名无头部(历史留档, 预期) {len(hist)} 个")
    print(f"  真实类目参数数量一致性: "
          f"{'一致' if real and len(r_inter) == len(r_union) else f'不一致(并集 {len(r_union)} / 交集 {len(r_inter)})'}")
    print(f"  结论: {'全部通过' if n_issue == 0 else f'{n_issue} 项需人工确认(见上)'}")
    return 0 if n_issue == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
