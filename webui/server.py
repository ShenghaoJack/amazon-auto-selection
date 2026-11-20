# -*- coding: utf-8 -*-
"""Amazon 选品流水线 Web 控制台 - 本地服务(纯 Python 标准库, 无新增依赖)。

功能:
  - 页面(webui/index.html)通过本服务的 HTTP API 触发流水线子进程:
      运行/断点续跑/从某步开始/单步执行/重置某步及下游/手动快照
  - 实时状态: 步骤状态表(step-status.txt)、运行输出流(子进程 stdout)、
    pipeline-progress.log 尾部、monitor-alerts.txt、health-report.json
  - 配置文件可视化编辑与保存(自动备份, 行级改写保留注释)

用法:
  python webui/server.py [--port 8790] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import json
import os
import hmac
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import yaml
from collections import deque
from functools import lru_cache
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

# 项目根加入 sys.path(2026-09-08): 以 `python webui/server.py` 启动时 sys.path[0]=webui,
# 处理器内惰性 `import core.*`(类目扫描/新建/同步等)会失败 → 配置列表只剩根模板、
# 新建类目 500。任何启动方式都保证 core 可导入。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 启动后台预热和接收请求前，先加载类目配置模块，避免并发首次导入。
import core.category_config  # noqa: E402,F401

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = Path(__file__).resolve().parent
HTML_FILE = WEBUI_DIR / "index.html"
RUN_DIR = PROJECT_ROOT / ".webui" / "runs"          # 临时运行配置
LOG_DIR = PROJECT_ROOT / ".webui" / "logs"          # 历史运行日志(可留档)
CONFIG_BACKUP_DIR = PROJECT_ROOT / ".webui" / "config-backups"   # 配置文件备份(集中存放, 文件名带 CAT+时间)
RING_MAX = 4000
LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")

STEP_COUNT = 20      # 0–19，末步为备份与最终体检

# 配置清单扫描缓存(2026-10-09 性能): 键值指纹见 _scan_configs(); 短 TTL 只为兜住"目录改名"这类
# 不体现在 config.yaml mtime 上的变化(改配置本身会立刻使指纹失效)。
_SCAN_CONFIGS_CACHE: dict = {}
_RUN_START_LOCK = threading.Lock()
_SCAN_CONFIGS_TTL = 60.0     # 只用于兜住『目录改名』这类不体现在 config.yaml mtime 上的变化;
                             # 配置内容一改 mtime_ns 即失效, 写配置入口还会显式清缓存

# WebUI 构建版本: 页面头部展示 + 启动日志打印, 用于确认服务是否为最新代码
# (旧服务进程不具备 /api/files、累计计数、下载白名单收窄等能力, 必须重启到同版本)
# 2026-09-08: 快照命名剥链式前缀 + 新建类目 core 导入修复后 bump, 便于确认重启生效
# 2026-10-08: 费用改「类目累计」取数 + 配置保存改「先校验后写盘/失败回滚」+ 关键词清单面板,
#             故 bump —— 页面会拿 `WUI_EXPECT`(index.html) 对比本值, 不符即提示"请重启服务"。
WEBUI_VERSION = "2026-11-12.1"

# 产物下载白名单 —— 仅保留关键交付报表(2026-09 收窄):
#   步骤8 关键词报告 / 步骤10 父项变体明细+过滤审计 / 步骤14 流量拆分需人工补充
#   步骤15 初步类目分析报告 / 步骤16 属性集规格 / 步骤18 标准化交付('CAT' 运行时替换为类目名)
ARTIFACT_TABLE: list[tuple[int, str]] = [
    (8, "CAT-keyword-report.xlsx"),
    (10, "CAT-父项变体明细.xlsx"),
    (10, "CAT-过滤审计.csv"),
    (14, "14_流量拆分/流量拆分数据-需人工补充.xlsx"),
    (15, "CAT-初步类目分析报告.xlsx"),
    (16, "CAT-attribute-spec.json"),
    (18, "18_标准化/CAT-属性标注-标准化交付.xlsx"),
]
# 单文件下载大小上限(超出拒绝, 防止误拖超大文件)
MAX_DOWNLOAD_BYTES = 1_000_000_000

# 属性集规格(步骤16 产物)状态 —— 与 tools/attr_stability/spec.py 保持一致(此处不 import 该包,
# 避免 webui 启动时连带导入工具链; spec 格式简单, 直接以 stdlib json 读写)
ATTR_SPEC_PENDING = "pending_confirm"
ATTR_SPEC_CONFIRMED = "confirmed"

# 已知键类型(用于配置文件缺失该键时的值转换, 与 core/config.py DEFAULTS 对齐)
KNOWN_BOOL_KEYS = {"LLM_NO_THINK", "REVIEW_AUTO", "SS_FORCE_RESUBMIT", "DRY_RUN"}
KNOWN_INT_KEYS = {
    "GATE_MIN_QUALIFIED", "GATE_NEW_SELLERS", "MONTHLY_SALES_MIN", "FETCH_WORKERS", "FETCH_ROUND_PAUSE",
    "FETCH_STALL_ROUNDS", "FETCH_ASIN_TRIES", "FETCH_LONG_EVERY", "FETCH_LONG_MIN", "FETCH_LONG_MAX",
    "BROWSER_FALLBACK_ROUNDS", "BROWSER_ROUND_PAUSE",
    "REVIEW_LIMIT", "REVIEW_MIN_RATE", "EXPORT_STATUS_WAIT", "EXPORT_STATUS_INTERVAL",
    "REQUEST_RETRY", "BATCH_FILTER", "BATCH_PARENT", "BATCH_VARIANT", "CANDIDATE_LIMIT",
    "FILTER_JUDGE_TRIES", "QUERY_WAIT_TIMEOUT", "LONGTAIL_PER_DIM",
    "CORE_KEYWORD_COUNT", "LONGTAIL_COUNT", "CORE_SEARCH_PAGES", "LONGTAIL_SEARCH_PAGES", "DOWNLOAD_TIMEOUT", "SCREENSHOT_EVERY",
    "STUCK_MINUTES", "CONSECUTIVE_FAIL_WARN", "HTTP_TIMEOUT",
}
KNOWN_FLOAT_KEYS = {"REQUEST_MIN_INTERVAL", "REQUEST_MAX_INTERVAL", "MOM_YOY_MIN",
                    "GOOGLE_YOY_MIN", "CPC_MAX", "RECENT_SUCCESS_RATIO"}


# ---------------------------------------------------------------- 工具
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _meter_legacy_vocab_client(client, root, step):
    """Give legacy vocabulary endpoints an isolated, immediately written ledger."""
    from webui.step18_api import _metered_client
    from tools.attr_stability.step18_workspace import Workspace, token
    client, meter = _metered_client(Workspace(root), {}, token("J"), lambda _: client)
    if meter is not None and step != 18:
        meter.begin_step(step)
    return client


def ts_for_log() -> str:
    return datetime.now().strftime("%H:%M:%S")


def fmt_yaml_value(v):
    """把 Python 值格式化为 YAML 字面量(引号策略与项目 config.yaml 一致)。"""
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if '"' in s and "'" not in s:
        return "'" + s.replace("'", "''") + "'"
    if '"' in s:
        return "'" + s.replace("'", "''") + "'"
    if "'" in s:
        return '"' + s.replace('"', '\\"') + '"'
    # 含冒号/井号/前后空白才必须引号
    if re.search(r"[:#]|^\s|\s$", s):
        return '"' + s + '"'
    if re.match(r"^(true|false|null|yes|no|on|off|\d)", s, re.I):
        return '"' + s + '"'
    return s


def coerce_by_existing(key: str, raw: str, existing: dict):
    """按配置文件中该键的现有类型转换用户输入的字符串(缺失时用已知键类型表)。"""
    base = existing.get(key)
    if not isinstance(base, (bool, int, float)):
        if key in KNOWN_BOOL_KEYS:
            base = True
        elif key in KNOWN_INT_KEYS:
            base = 0
        elif key in KNOWN_FLOAT_KEYS:
            base = 0.0
    if isinstance(base, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "真", "是")
    if isinstance(base, int):
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return str(raw).strip()
    if isinstance(base, float):
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return str(raw).strip()
    return str(raw)


def patch_yaml_lines(lines, values: dict) -> list:
    """行级改写: 只替换顶层 `KEY:` 的值部分, 保留行内注释与其余所有行。"""
    pat = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
    out = []
    replaced = set()
    for ln in lines:
        m = pat.match(ln)
        if m and m.group(1) in values:
            key = m.group(1)
            new_line = f"{key}: {fmt_yaml_value(values[key])}"
            cm = re.search(r"(\s*#.*)$", ln)
            if cm:
                new_line += cm.group(1)
            out.append(new_line)
            replaced.add(key)
        else:
            out.append(ln)
    for key in values:
        if key not in replaced:
            out.append(f"{key}: {fmt_yaml_value(values[key])}")
    return out


# ---------- 列表型配置键(需要整块编辑, 如 SS_ACCOUNTS) ----------
def parse_ss_accounts(raw) -> list | None:
    """把 yaml 中的 SS_ACCOUNTS 原始值规范化为 [{account,password,note,is_main}];
    键缺失返回 None; 兼容字符串/列表/字典项。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for it in raw or []:
        if isinstance(it, str):
            parts = it.split(":", 1)
            out.append({"account": parts[0].strip(),
                        "password": parts[1].strip() if len(parts) > 1 else "",
                        "note": "", "is_main": False})
        elif isinstance(it, dict):
            out.append({"account": str(it.get("account", "") or "").strip(),
                        "password": str(it.get("password", "") or "").strip(),
                        "note": str(it.get("note", "") or "").strip(),
                        "is_main": bool(it.get("is_main", False))})
    return out


def render_ss_accounts(items: list | None) -> list[str]:
    """渲染 SS_ACCOUNTS 块(与 config.yaml 风格一致: 2 空格缩进列表项)。"""
    if not items:
        return ["SS_ACCOUNTS: []"]
    lines = ["SS_ACCOUNTS:"]
    for it in items:
        lines.append(f"  - account: {fmt_yaml_value(str(it.get('account', '') or ''))}")
        lines.append(f"    password: {fmt_yaml_value(str(it.get('password', '') or ''))}")
        lines.append(f"    note: {fmt_yaml_value(str(it.get('note', '') or ''))}")
        lines.append(f"    is_main: {'true' if it.get('is_main') else 'false'}")
    return lines


def replace_yaml_block(lines: list, key: str, block_lines: list) -> list:
    """整体替换顶层键的整块(键行起, 到下一个顶层键行前为止, 其间注释一并替换);
    键不存在则追加到文件尾。返回新行列表。"""
    pat = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
    out = []
    i, n = 0, len(lines)
    replaced = False
    while i < n:
        ln = lines[i]
        if not replaced and (ln == key + ":" or ln.startswith(key + ": ")):
            i += 1
            while i < n and not pat.match(lines[i]):
                i += 1  # 跳过块内行(含其间的注释/空行)
            if out and out[-1].strip():
                out.append("")
            out.extend(block_lines)
            replaced = True
            continue
        out.append(ln)
        i += 1
    if not replaced:  # 键不存在 → 文件尾追加
        if out and out[-1].strip():
            out.append("")
        out.extend(block_lines)
    return out


def load_yaml_file(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


_PLACEHOLDER_CAT_RE = re.compile(r"(xxx|template|示例|样例|placeholder|YYYYMM|your-category)", re.I)


def _is_placeholder_cat_fallback(cat: str) -> bool:
    """与 core.category_config.is_placeholder_cat 保持一致的降级判断。"""
    return not cat or bool(_PLACEHOLDER_CAT_RE.search(cat))


def invalidate_config_cache() -> None:
    """配置清单缓存显式失效(2026-10-09): 保存配置/新建类目后立即调用,
    这样 TTL 放宽到 60 秒也不会让用户看到过期的下拉清单。"""
    _SCAN_CONFIGS_CACHE.clear()
    _HIST_STATUS_CACHE.clear()


# 类目中心 /api/history 的类目级缓存: cat -> (step-status mtime_ns, (last, parsed))
_HIST_STATUS_CACHE: dict = {}
# 配置清单扫描的**单飞锁**(2026-10-09): 重启后预热线程与浏览器请求会同时进来,
# 没有锁时每个请求各自做一遍 ~1.8s 的全量扫描(实测 /api/history 因此 1088ms);
# 加锁后并发请求排队复用第一份结果。
_SCAN_LOCK = threading.Lock()


def _hist_status_snapshot(mdir) -> tuple[str, dict]:
    """读某类目的 `step-status.txt`: 返回(最晚时间戳, 解析后的状态表)。

    入参是 **monitor 目录**(即 `<DATA_ROOT>/<CAT>/20_备份与监控`), 不是类目根目录。
    缓存键 = 完整路径 + 文件 mtime_ns(写状态必改 mtime) → 内容变了立刻重读, 不变则 0 磁盘 IO。
    """
    mdir = Path(mdir)
    stp = mdir / "step-status.txt"
    key = str(stp)                           # 用完整路径做键: 单测 tmp 根里同名类目不会串
    try:
        mtime = stp.stat().st_mtime_ns
    except OSError:
        _HIST_STATUS_CACHE.pop(key, None)
        return "", {}
    hit = _HIST_STATUS_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    last = ""
    try:
        hits = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                          stp.read_text(encoding="utf-8-sig", errors="replace"))
        if hits:
            last = max(hits)
    except Exception:  # noqa: BLE001
        pass
    try:
        parsed = parse_step_status(stp)
    except Exception:  # noqa: BLE001
        parsed = {}
    if len(_HIST_STATUS_CACHE) > 128:      # 简单上界, 避免长期运行无限增长
        _HIST_STATUS_CACHE.clear()
    _HIST_STATUS_CACHE[key] = (mtime, (last, parsed))
    return last, parsed


def _warm_startup_caches() -> None:
    """预热首屏重扫描(2026-10-09 性能): 配置清单扫描冷启动实测 ~1.3s。

    在后台线程跑, 不阻塞端口监听; 之后 /api/bootstrap、/api/configs、/api/history
    的同类扫描直接命中 `_SCAN_CONFIGS_CACHE`(热态 ~0.3ms)。失败只记日志, 不影响服务。
    """
    try:
        h = Handler.__new__(Handler)          # 只借方法(内部只用模块级状态, 不碰 socket)
        t0 = time.perf_counter()
        items = h._scan_configs()
        print(f"[webui] 已预热配置清单缓存: {len(items)} 个配置, {(time.perf_counter()-t0)*1000:.0f} ms")
    except Exception as exc:  # noqa: BLE001
        print(f"[webui] 预热缓存失败(不影响使用): {type(exc).__name__}: {exc}")


# 必须**单行**的配置键(2026-09-29): 值里一旦带换行, 轻则 YAML 解析崩(粘成两行),
# 重则被引号包起来"存进去"但后续请求全废(COOKIE 里夹了换行)。保存前统一拦掉。
SINGLE_LINE_KEYS = ("COOKIE", "LIST_URLS", "SS_PASSWORD", "LLM_API_KEY",
                    "VISION_API_KEY", "SS_MCP_KEY", "SS_CDP_URL")


def multiline_value_error(values: dict) -> str | None:
    """入参层面的拦截(比"改写完文本再校验"更早、提示更准): 返回错误文案, None = 通过。"""
    for key in SINGLE_LINE_KEYS:
        val = (values or {}).get(key)
        for one in (val if isinstance(val, list) else [val]):
            if isinstance(one, str) and ("\n" in one or "\r" in one):
                return (f"配置未保存: `{key}` 的值里有换行(粘贴时被分成了两行?) —— "
                        f"请把整条值粘成**一行**(不要带 `Cookie: ` 前缀、不要带首尾引号、不要分段); "
                        f"跨行会让该值失效")
    return None


def _bare_scalar_line(lines: list[str]) -> int:
    """找"看起来是某个值、却没有 `键:` 前缀"的**顶格裸行**(0-based; -1 = 没找到)。

    这类行的唯一现实成因就是"**上一个字段的值里带了换行**"(实测: COOKIE 被粘成两行 —— 第 118 行
    `COOKIE: '…'` 完整, 第 119 行又多出一段裸值)。它会让 YAML 直接报 `could not find expected ':'`,
    但报的**位置往往落在下一行**, 用户照着报错行看会一脸懵 —— 所以这里主动把它找出来。

    只在"顶格 + 形如 `key=value`/含 `;`"的行上判定, 避免误伤 `KEY: |` 块标量的缩进内容(那些是缩进的)。
    """
    for i, ln in enumerate(lines):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln[:1] in (" ", "\t") or ln.lstrip().startswith("- "):
            continue
        if ":" in ln:
            continue
        if "=" in ln or ";" in ln:
            return i
    return -1


def yaml_save_guard(text: str) -> str | None:
    """保存前的**内容校验 + 可操作提示**(返回 None = 通过; 否则返回给用户看的错误文案)。

    为什么必须"先校验再写盘"(2026-09-29 真事故): 原实现是"先备份 → **先写盘** → 再校验",
    于是校验抛 `ScannerError` 报"保存失败"时**文件其实已经被写坏了**(只有备份是好的, 用户并不知道)。
    实测成因: `COOKIE` 值被**粘成两行**(第 118 行 `COOKIE: '…'` 完整, 第 119 行又多出一段裸值、
    没有 `KEY:`) → PyYAML 报 `while scanning a simple key ... could not find expected ':'`。

    这里做两层:
      ① `yaml.safe_load` 解析不通过 → 报错, 并**指出真正的裸行**(而不是 YAML 报的那一行)+ 改法;
      ② 解析通过但**语义明显不对**也要拦: `COOKIE`/`LIST_URLS` 这类"必须单行"的值里带了换行
         (双引号写法会保留 `\\n`) → 直接拒绝并给出改法。
    """
    import yaml
    lines = text.splitlines()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = ""
        if mark is not None:
            where = f"，第 {mark.line + 1} 行第 {mark.column + 1} 列"
        problem = getattr(exc, "problem", None) or str(exc)
        msg = f"配置未保存(YAML 语法错误{where}): {problem}"
        suspect = _bare_scalar_line(lines)
        if suspect >= 0:
            msg += (f"。**第 {suspect + 1} 行看起来不是 `键: 值` 形式** —— 最常见的原因是"
                    f"**上一个字段的值里带了换行**(例如 COOKIE / LIST_URLS 被粘贴成两行): "
                    f"请把该值合并成**一行**再保存"
                    f"\n该行内容: {lines[suspect][:200]}")
            if mark is not None and mark.line != suspect:
                msg += f"\n(YAML 报的位置是第 {mark.line + 1} 行, 那是它放弃继续扫描的地方, 不是病根)"
        else:
            bad = lines[mark.line] if (mark is not None and 0 <= mark.line < len(lines)) else ""
            msg += "。请检查该行(以及上一行)的缩进与冒号是否完整"
            if bad:
                msg += f"\n出错行内容: {bad[:200]}"
        return msg
    if not isinstance(data, dict):
        return "配置未保存: 顶层不是 `键: 值` 结构(检查是否漏了冒号或整体缩进错了)"
    for key in SINGLE_LINE_KEYS:
        vals = data.get(key)
        for val in (vals if isinstance(vals, list) else [vals]):
            if isinstance(val, str) and ("\n" in val or "\r" in val):
                return (f"配置未保存: `{key}` 的值里有换行 —— 请把整条值粘成**一行**"
                        f"(不要带 `Cookie: ` 前缀、不要带首尾引号、不要分段); 该值跨行会让后续请求全部失效")
    return None


def safe_config_path(raw: str) -> Path:
    """解析配置文件路径: 优先项目根 *.yaml, 其次 .webui/runs/ 历史运行快照(2026-09-04)。

    历史恢复面板的 config_file 是 run-*.yaml(位于 .webui/runs/), 不在项目根——
    仅按项目根解析会失败导致 CAT 读空、步骤看板空白。两处都找, 仍无则报错。
    """
    p = (PROJECT_ROOT / raw).resolve()
    if p.is_file() and p.suffix.lower() in (".yaml", ".yml"):
        return p
    p2 = (RUN_DIR / raw).resolve()
    if p2.is_file() and p2.suffix.lower() in (".yaml", ".yml"):
        return p2
    if p.suffix.lower() not in (".yaml", ".yml"):
        raise ValueError(f"不是 YAML 配置文件: {raw}")
    raise ValueError(f"配置文件不存在: {p}")


# ---------------- 配置文件备份(集中存放 .webui/config-backups/, 文件名带 CAT+时间) ----------------
def _yaml_cat(path: Path) -> str:
    """从 YAML 配置读取 CAT 作备份文件名用; 缺失/解析失败返回 'nocat'。"""
    try:
        return str(load_yaml_file(path).get("CAT", "") or "").strip() or "nocat"
    except Exception:
        return "nocat"


def _safe_backup_token(text: str) -> str:
    """CAT/文件名片段清洗为安全字符(字母数字 - _ .), 空则 'nocat'。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", text).strip("-")
    return cleaned or "nocat"


def make_config_backup(path: Path) -> Path | None:
    """保存配置前把当前文件复制为集中备份(2026-09 版本化)。

    命名: <类目名清洗>-<YYYYMMDD>-v<N>-<HHMMSS>.bak(类目名+日期+版本+时间),
    头部带说明行; 有改动才生成(由调用方传 changed_to 判断)。失败/无改动返回 None。
    """
    from core.config_backup import make_backup as _mk_backup

    return _mk_backup(path, reason="webui 保存前自动备份")


def migrate_legacy_config_backups() -> dict:
    """把项目根目录散落的 *.bak-<时间戳> 老备份迁入集中目录(重命名为带 CAT 格式)。

    幂等: 只迁移项目根下形如 <stem>.bak-<ts> 的文件, 成功后删除原位副本。
    返回 {"migrated": [旧名→新名], "kept": [...]}.
    """
    if not PROJECT_ROOT.exists():
        return {"migrated": [], "kept": []}
    migrated: list[dict] = []
    kept: list[str] = []
    pat = re.compile(r"^(?P<stem>.+?)(\.yaml|\.yml)?\.bak-(?P<ts>\d{8}-\d{6})$")
    for p in sorted(PROJECT_ROOT.glob("*.bak-*")):
        if not p.is_file():
            continue
        m = pat.match(p.name)
        if not m:
            kept.append(p.name)
            continue
        cfg_candidate = PROJECT_ROOT / (m.group("stem") + (m.group(2) or ".yaml"))
        cat = _safe_backup_token(_yaml_cat(cfg_candidate)) if cfg_candidate.exists() else "nocat"
        target = CONFIG_BACKUP_DIR / f"{m.group('stem')}-{cat}-{m.group('ts')}.bak"
        try:
            CONFIG_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(p, target)
            p.unlink()  # 迁移成功删除原位散落副本
            migrated.append({"from": p.name, "to": target.name})
        except OSError:
            kept.append(p.name)
    return {"migrated": migrated, "kept": kept}


# ---------------------------------------------------------------- 客户端提前断开(静默处理)
# 背景(2026-09-13): 浏览器在服务端写完响应前关闭连接时, wfile.write 会抛
#   ConnectionAbortedError(WinError 10053) / BrokenPipeError / ConnectionResetError。
#   原实现让异常冒到 socketserver: 每次刷新页面都打**两大段** traceback(第一段=写响应体中断,
#   第二段=兜底 500 分支又写一次), 把真正的报错淹没; 服务本身不受影响(每请求一线程), 但很吓人。
# 现做法: 这三类异常统一吞掉 + 记一行(同一路径 5 秒内合并计数), 兜底分支也不再二次抛。
CLIENT_GONE = (ConnectionAbortedError, BrokenPipeError, ConnectionResetError)
_ABORT_STATE = {"key": "", "ts": 0.0, "n": 0}


def is_client_gone(exc: BaseException) -> bool:
    """是否属于"客户端已断开"类异常(与业务错误区分)。"""
    return isinstance(exc, CLIENT_GONE)


def log_client_abort(where: str, exc: BaseException) -> None:
    """连接中断只记一行(同一位置 5 秒内合并计数), 不打印 traceback。"""
    now = time.time()
    if _ABORT_STATE["key"] == where and now - _ABORT_STATE["ts"] < 5.0:
        _ABORT_STATE["n"] += 1
        return
    if _ABORT_STATE["key"] == where and _ABORT_STATE["n"] > 1:
        merged = f" (近 5 秒同类中断 {_ABORT_STATE['n']} 次)"
    else:
        merged = ""
    _ABORT_STATE.update({"key": where, "ts": now, "n": 1})
    print(f"[连接中断] {where} 客户端已提前断开(页面刷新/关闭或取消下载), 已忽略{merged}: "
          f"{type(exc).__name__}", flush=True)



# ---------------------------------------------------------------- 日志环形缓冲
class RingLog:
    def __init__(self, maxlen: int = RING_MAX):
        self._buf = deque(maxlen=maxlen)
        self._id = 0
        self._lock = threading.Lock()
        # 多类目并行(2026-09-15): 服务器级事件(保存配置/告警/中断请求)同时写进这些
        # **在跑类目各自的缓冲**, 这样"看哪个类目就看到那个类目的完整控制台"。
        # 子进程输出只进本类目缓冲(不 mirror), 避免把 A 类目的日志串到 B 类目控制台。
        self.mirrors: list["RingLog"] = []

    def push(self, text: str, level: str = "INFO", ts: str | None = None, mirror: bool = True):
        with self._lock:
            self._id += 1
            entry = {"id": self._id, "ts": ts or ts_for_log(), "level": level, "text": text}
            self._buf.append(entry)
        if mirror:
            for m in list(self.mirrors):
                if m is self:
                    continue
                try:
                    m.push(text, level=level, ts=entry["ts"], mirror=False)
                except Exception:  # noqa: BLE001
                    pass
        return entry

    def since(self, after: int, limit: int = 400) -> list:
        with self._lock:
            items = list(self._buf)
        if after is None:
            after = -1
        out = [e for e in items if e["id"] > after]
        if len(out) > limit:
            out = out[-limit:]
        return out

    def tail(self, n: int = 200) -> list:
        with self._lock:
            items = list(self._buf)
        return items[-n:]

    def watermark(self) -> int:
        with self._lock:
            return self._id

    def clear(self) -> None:
        """清空缓冲(2026-09-11: 新运行开始时调用)。

        只清空已缓存条目, **不重置 `_id`** —— 保证 id 单调递增, 前端已持有的
        游标(`lastLogId`/`stream_after`)不会因清空而回退; 旧行已持久化在各自
        `data/<CAT>/20_备份与监控/pipeline-progress.log`, 清空不影响回溯。
        """
        with self._lock:
            self._buf.clear()


RING = RingLog()


def cat_ring(cat: str):
    """按类目取实时日志缓冲(有该类目的运行记录才有; 否则 None)。"""
    cat = str(cat or "").strip()
    if not cat:
        return None
    try:
        return RUNNER.ring_for(cat)
    except Exception:  # noqa: BLE001
        return None


def push_cat(cat: str, text: str, level: str = "INFO"):
    """**类目级**消息只写该类目的缓冲(没有该类目缓冲时退回服务器级缓冲), 且**不广播**。

    2026-09-16 修(用户报障「UK 的怎么又混到了 FR 的实时日志中」): 维护命令(重置步骤N-19/
    归档/reset)与配置保存这类**明确属于某个类目**的消息, 原先走全局 `RING.push()` ——
    而 `RingLog.push` 默认 `mirror=True`, 会把服务器级消息**广播进所有在跑类目的缓冲**
    (`RING.mirrors` = 各在跑类目的 ring)。实测: 用户在 FR 的运行台看到 **18 行 UK 的
    「重置步骤10-19」输出**(含被删的 15 个 `data/Chopping Boards-UK-202608/...` 路径)。
    流水线子进程输出一直是"只进本类目缓冲"(`push_console_line(..., ring=rec["ring"])`),
    唯独维护/配置这条路径漏了 → 现统一走本函数。
    返回实际写入的缓冲(便于测试与排查)。
    """
    target = cat_ring(cat) or RING
    target.push(text, level=level, mirror=False)
    return target


def push_console_line(text: str, ring: "RingLog | None" = None):
    """把子进程输出行解析成带级别日志条目(ring: 写到该类目的缓冲; 缺省=全局缓冲)。"""
    target = ring or RING
    if not text:
        return
    text = text.rstrip("\r\n")
    if not text:
        return
    m = LOG_LINE_RE.match(text)
    if m:
        target.push(m.group(3), level=m.group(2), ts=m.group(1))
    elif text.startswith("===") or "Traceback" in text:
        target.push(text, level="ERROR" if "Traceback" in text else "INFO")
    elif re.search(r"\[(CRIT|ERROR|WARN|INFO)\]", text):
        lv = re.search(r"\[(CRIT|ERROR|WARN|INFO)\]", text).group(1)
        target.push(text, level=lv)
    else:
        target.push(text, level="INFO")


# ---------------------------------------------------------------- 子进程管理器
def prune_run_snapshots(keep: int | None = None, protect: set | None = None) -> list[str]:
    """限量清理临时运行快照, 返回被删文件名。

    **多类目并行(2026-09-15)**: 清理时必须**跳过在跑运行的快照** —— 否则 A 类目跑完触发清理,
    可能把 B 类目**正在用**的临时配置删掉(子进程若还没读完/后续按该路径回读就会出问题)。
    protect: 需要保护的绝对路径集合(在跑运行的 effective_conf)。
    """
    if keep is None:
        keep = max(6, int(os.environ.get("WEBUI_KEEP_RUN_SNAPSHOTS", "12") or "12"))
    guard = {str(Path(p)) for p in (protect or set()) if p}
    removed: list[str] = []
    try:
        snaps = sorted(RUN_DIR.glob("run-*.yaml"))
        for old in snaps[:-keep]:
            if str(old) in guard:
                continue
            try:
                old.unlink()
                removed.append(old.name)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return removed


def _cat_config_values(cat: str, data_root: str) -> dict:
    """读某类目的配置(并行冲突判定用; 读不到返回 {})。"""
    try:
        p = PROJECT_ROOT / str(data_root or "data") / str(cat) / "config.yaml"
        if p.is_file():
            return load_yaml_file(p) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


class UserFacingError(RuntimeError):
    """**用户可读的拒绝**(不是服务端故障): 同类目重复启动/与在跑类目冲突/维护命令撞车等。

    2026-09-15 修: 这些原先是裸 `RuntimeError`, 经 do_POST/do_GET 兜底变成 **HTTP 500 +
    "RuntimeError: …"**, 前端 `api()` 会把这条原文弹给用户 —— 看着像服务端崩了, 实际只是
    "请先中断再启动"。现单独成类型并映射为 **409 Conflict**(消息原文返回, 不带类型前缀)。
    """


class Forbidden(UserFacingError):
    """已登录但**无权访问该类目**（类目白名单）；映射为 403。

    2026-11-12 新增: 只有 admin 能看到所有类目；operator/viewer 只能看自己被授权的类目。
    """


# ★ 2026-11-12: 操作日志的动作名（接口路径 → 人类可读动作）
OP_ACTIONS = {
    "/api/run": "启动流水线",
    "/api/stop": "中断流水线",
    "/api/config/save": "保存配置",
    "/api/reset-step": "重置步骤",
    "/api/archive": "归档快照",
    "/api/integrated-report/build": "生成整合报告",
    "/api/keyword-lists/save": "保存关键词清单",
    "/api/keyword-lists/stop": "暂停/恢复关键词抓取",
    "/api/keyword-lists/confirm": "确认关键词并续跑",
    "/api/category/create": "新建类目",
    "/api/month-fallback-confirm": "确认月份回退",
    "/api/attr-spec/save": "保存属性集",
    "/api/attr-spec/confirm": "确认属性集",
    "/api/attr-exclude": "排除属性",
    "/api/attr-candidate": "加回候选属性",
    "/api/std-vocab/regenerate": "重生成词表",
    "/api/std-vocab/chat": "词表对话",
    "/api/std-vocab/save": "保存标准词表",
    "/api/std-vocab/chat-history": "词表对话存档",
    "/api/std-vocab/reannotate": "词表重判",
    "/api/std-vocab/prompts": "查看词表提示词",
    "/api/std-vocab/rollback": "词表回滚",
}
# 步骤18 是 `/api/std-workspace/<action>`，按后缀映射
STD_ACTIONS = {
    "preview": "步骤18 预览",
    "apply": "步骤18 确认应用",
    "export": "步骤18 导出交付",
    "audit": "步骤18 二审",
    "refresh-input": "步骤18 重新导入",
    "restore-preview": "步骤18 回滚",
    "retry-job": "步骤18 重试任务",
    "cancel-job": "步骤18 取消任务",
    "delete-row": "步骤18 删除商品行",
    "delete-attribute": "步骤18 删除属性列",
    "delete-value-rows": "步骤18 按值删除商品",
    "value-rows-info": "步骤18 预览删除影响",
    "row-info": "步骤18 查看商品行",
    "preview-page": "步骤18 翻页预览",
}


def suggest_free_cdp_port(ignore_cat: str = "") -> int:
    """给"需要换端口"的类目挑一个**当前没被任何类目占用**的 CDP 端口(默认从 9223 起)。

    用于冲突报错里的可操作建议(2026-09-15): 用户按提示换端口时, 不该让他自己去猜哪个端口空着。
    """
    used: set[int] = set()
    try:
        from core.category_config import list_category_configs
        from core.ss_lock import normalize_cdp_url

        for cfg in list_category_configs(PROJECT_ROOT / "data"):
            vals = load_yaml_file(cfg) or {}
            if ignore_cat and str(vals.get("CAT") or "").strip() == str(ignore_cat).strip():
                continue
            hostport = normalize_cdp_url(str(vals.get("SS_CDP_URL") or ""))
            if hostport:
                try:
                    used.add(int(hostport.rsplit(":", 1)[1]))
                except (IndexError, ValueError):
                    pass
    except Exception:  # noqa: BLE001
        pass
    for port in range(9223, 9300):
        if port not in used:
            return port
    return 9223


def _run_config_values(run: dict) -> dict:
    """运行中的实际配置优先；自动端口只存在于运行快照中。"""
    effective = str(run.get("effective_conf") or "").strip()
    if effective:
        try:
            values = load_yaml_file(Path(effective))
            if not isinstance(values, dict):
                raise ValueError("运行配置不是映射")
            return values
        except Exception as exc:
            raise UserFacingError("无法读取正在运行任务的实际配置，不能安全判断浏览器端口冲突") from exc
    return _cat_config_values(str(run.get("cat") or ""), str(run.get("data_root") or "data"))


def run_start_guard(cat: str, data_root: str, runner=None, effective_config=None) -> list[dict]:
    """启动前的**并行守门**(2026-09-15, 用户需求: 看板里同时跑多个类目)。

    规则:
      * **同一个类目只允许一条流水线**(双跑会互写 step-status/产物, 且
        `data/<CAT>/.run.lock` 也会拦); **不同类目放行** —— 这正是并行要的;
      * **同一个卖家精灵浏览器 + 不同账号** = 硬冲突(一个 Chrome 只能保持一份登录态,
        后跑的会用到前一个账号的配额与数据) → 拦下并提示改用不同 CDP 端口
        (2026-09-15 用户"会给不同类目配不同账号"后新增, 见 `core.ss_lock.browser_conflict`)。
    返回"还在跑的其它类目"清单(调用方据此提示共享资源风险)。

    data_root: 该类目的数据根(定位 .run.lock 与类目配置); runner: 便于单测注入。
    """
    runner = runner if runner is not None else RUNNER
    key = str(cat or "").strip()
    from tools.attr_stability.export_state import export_status, BUSY_MESSAGE
    export = export_status(cat_root_dir(key, str(data_root or "data")))
    if export.get("status") in ("running", "unknown"):
        raise UserFacingError(BUSY_MESSAGE if export['status'] == 'running' else export['message'])
    if key and runner.active_for(key):
        raise UserFacingError(f"类目「{key}」已有流水线在本服务运行中, 请先中断再启动")
    lock_alive, lock_started, _ = _lock_live_started(key, str(data_root or "data"))
    if lock_alive:
        raise UserFacingError(
            f"类目「{key}」正被另一个进程运行(检测到 .run.lock"
            + (f", 开始于 {lock_started}" if lock_started else "")
            + "): 可能是命令行窗口里在跑; 请先停掉它, 或换一个类目"
        )
    others = [r for r in runner.snapshots() if r.get("active")]
    if not others:
        return others
    try:
        from core.ss_lock import accounts_of, browser_conflict, primary_account

        mine = effective_config if effective_config is not None else _cat_config_values(key, data_root)
        for r in others:
            ocat = str(r.get("cat") or "")
            if not ocat:
                continue
            ocfg = _run_config_values(r)
            why = browser_conflict(str(mine.get("SS_CDP_URL", "") or ""), primary_account(mine),
                                   accounts_of(mine),
                                   str(ocfg.get("SS_CDP_URL", "") or ""), primary_account(ocfg),
                                   accounts_of(ocfg))
            if why:
                # 报错要**直接可操作**(2026-09-15): 点名"本次要启动的类目", 给出一个当前**真的空着**
                # 的端口, 以及拉起该端口 Chrome 的命令 —— 否则用户只能回来问"那我该改成几号?"
                port = suggest_free_cdp_port(ignore_cat=key)
                rel = f"data/{key}/config.yaml"
                raise UserFacingError(
                    f"与正在运行的类目「{ocat}」冲突: {why}。"
                    f"→ 改本类目「{key}」即可: 把 {rel} 里的 SS_CDP_URL 改成 "
                    f"http://127.0.0.1:{port}, 然后执行 "
                    f"python scripts/start_ss_chrome.py --config \"{rel}\" --port {port} 并在该窗口登录"
                    f"本类目账号(首次也可以直接启动运行: 系统会自动拉起 {port} 端口的 Chrome 并自动登录); "
                    f"两个类目各用一个端口即可**同时**跑, 步骤 0/11/12/13 不再互相等待"
                )
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001
        pass
    return others


def ss_parallel_notes(cat: str, data_root: str, others: list[dict], effective_config=None) -> list[str]:
    """并行提示(不阻断): 与在跑类目共用同一个卖家精灵浏览器 → 步骤 0/11/12/13 会排队。"""
    notes: list[str] = []
    if not others:
        return notes
    try:
        from core.ss_lock import normalize_cdp_url, resource_key

        mine = effective_config if effective_config is not None else _cat_config_values(cat, data_root)
        my_cdp = str(mine.get("SS_CDP_URL", "") or "")
        for r in others:
            ocat = str(r.get("cat") or "")
            if not ocat:
                continue
            ocfg = _run_config_values(r)
            o_cdp = str(ocfg.get("SS_CDP_URL", "") or "")
            if resource_key(my_cdp) == resource_key(o_cdp):
                where = normalize_cdp_url(my_cdp) or "同一 profile"
                notes.append(f"与「{ocat}」共用同一个卖家精灵浏览器({where}): "
                             f"步骤 0/11/12/13 会**排队串行**(同一份登录态/配额不能同时用); "
                             f"想真并行请给本类目换一个 SS_CDP_URL 端口")
    except Exception:  # noqa: BLE001
        pass
    return notes


class RunManager:
    """**多类目并行**(2026-09-15, 用户需求: "看板里同时跑多个类目")。

    与旧版(同一时间只允许一个流水线子进程)的差别:
      * 运行记录按**类目**归档(`self._runs[cat]`), 每个类目一条: 各自一个子进程、
        一份运行信息、**一个独立日志环形缓冲**(控制台只看当前类目的输出);
      * 同一类目**仍然只允许一条**(重复启动会被拒 —— 同一个类目双跑会互写
        step-status/产物, 且 `data/<CAT>/.run.lock` 也会拦);
      * 不同类目可以同时在跑, 停止/状态/日志都按类目各归各。
    共享资源的互斥不在这一层(而是运行期): 亚马逊 profile 按类目隔离
    (`core/profile_isolate.py`), 卖家精灵浏览器用**跨进程全局锁**串行
    (`core/ss_lock.py`)。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}     # key(类目) -> {"proc","info","ring"}
        self.history: deque = deque(maxlen=24)

    # ---------------- 查询
    @staticmethod
    def key_of(cat: str, fallback: str = "") -> str:
        return str(cat or "").strip() or str(fallback or "").strip() or "pipeline"

    def _records(self) -> list[dict]:
        with self._lock:
            return list(self._runs.values())

    def _is_alive(self, rec: dict) -> bool:
        try:
            return rec["proc"].poll() is None
        except Exception:  # noqa: BLE001
            return False

    @property
    def active(self) -> bool:
        """是否有**任一**类目在跑(兼容旧调用: 关机/升级前的"运行中"判断)。"""
        return any(self._is_alive(r) for r in self._records())

    def active_cats(self) -> list[str]:
        """正在跑的类目名列表(类目中心用它逐个类目纠偏 RUNNING 显示)。"""
        out = []
        for rec in self._records():
            if not self._is_alive(rec):
                continue
            info = rec["info"]
            name = str(info.get("cat") or info.get("key") or "").strip()
            if name:
                out.append(name)
        return sorted(set(out))

    def active_for(self, cat: str) -> bool:
        rec = self.record_for(cat)
        return bool(rec) and self._is_alive(rec)

    def record_for(self, cat: str) -> dict | None:
        with self._lock:
            return self._runs.get(self.key_of(cat))

    def ring_for(self, cat: str):
        """该类目的实时日志环形缓冲(无该类目运行记录时返回 None)。"""
        rec = self.record_for(cat)
        return rec["ring"] if rec else None

    def snapshot(self, cat: str | None = None) -> dict:
        """cat 给定 → 该类目的运行信息; 不给 → 最近一条有记录的运行(旧行为)。"""
        if cat:
            rec = self.record_for(cat)
            if rec is None:
                return {}
            with self._lock:
                info = dict(rec["info"])
            info["active"] = self._is_alive(rec)
            return info
        recs = self._records()
        if not recs:
            return {}
        # 不给 cat 时优先"在跑的那条"(诊断/监控更关心当前活动), 否则最近一条
        live = [r for r in recs if self._is_alive(r)]
        rec = live[-1] if live else recs[-1]
        with self._lock:
            info = dict(rec["info"])
        info["active"] = self._is_alive(rec)
        return info

    def snapshots(self) -> list[dict]:
        """全部运行记录(含已结束的最近一次), 供前端展示"并行运行中"清单。"""
        out = []
        for rec in self._records():
            with self._lock:
                info = dict(rec["info"])
            info["active"] = self._is_alive(rec)
            out.append(info)
        return out

    def _effective(self) -> dict:
        """当前运行使用的有效配置快照(CAT/DATA_ROOT 等)。"""
        return self.snapshot()

    def start(self, cmd: list, label: str, base_config: str,
              effective_conf: Path, cat: str, data_root: str,
              mode: str, from_step: int | None, overrides: dict,
              extra_env: dict | None = None, user: str = "") -> dict:
        key = self.key_of(cat, label)
        ring = RingLog()
        with self._lock:
            rec = self._runs.get(key)
            if rec is not None and self._is_alive(rec):
                raise UserFacingError(f"类目「{key}」已有流水线在运行中, 请先中断再启动")
            env = dict(os.environ)
            env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            if extra_env:
                env.update(extra_env)
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            info = {
                "active": True,
                "key": key,
                "pid": proc.pid,
                "started_at": now_str(),
                "ended_at": "",
                "cmd": " ".join(cmd),
                "label": label,
                "mode": mode,
                "from_step": from_step,
                "base_config": base_config,
                "effective_conf": str(effective_conf),
                "cat": cat,
                "data_root": data_root,
                "exit_code": None,
                "killed": False,
                # ★ 2026-11-12: 记录**操作人**（哪个账号启动的），供 admin 在运行台/类目中心看到"谁在跑"
                "user": str(user or ""),
            }
            self._runs[key] = {"proc": proc, "info": info, "ring": ring}
        # 该类目的控制台缓冲: 新运行开始时**只清自己的**(2026-09-15) —— 旧版清的是全局缓冲,
        # 并行跑时会把另一个类目的现场日志抹掉。旧行仍持久化在各自
        # data/<CAT>/20_备份与监控/pipeline-progress.log, 清空不影响回溯。
        ring.push(f"===== 新运行: {key}  (mode={mode}, label={label}) =====")
        ring.push(f"▶ [{label}] 已启动 pid={proc.pid}  操作人={user or '-'}  命令: {' '.join(cmd)}")
        ring.push(f"◆ 并行提示: 同一时刻可跑多个类目; 卖家精灵浏览器全局串行(别类目在用时会等待)")
        RING.push(f"▶ [{key}] 已启动 pid={proc.pid} (mode={mode})", mirror=False)
        # 服务器级事件(保存配置/告警等)也进该类目日志, 保持"一个类目一个控制台"的旧观感
        RING.mirrors.append(ring)
        threading.Thread(target=self._reader, args=(proc, ring), daemon=True).start()
        threading.Thread(target=self._waiter, args=(proc, key), daemon=True).start()
        return dict(info)

    def _reader(self, proc: subprocess.Popen, ring: "RingLog"):
        try:
            for line in proc.stdout:
                push_console_line(line, ring=ring)
        except Exception as exc:  # pragma: no cover
            ring.push(f"[读取子进程输出异常] {exc}", level="ERROR")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def _waiter(self, proc: subprocess.Popen, key: str):
        code = proc.wait()
        cat = key
        with self._lock:
            rec = self._runs.get(key)
            if rec is not None and rec["info"].get("pid") == proc.pid:
                cat = rec["info"].get("cat") or key
                rec["info"]["exit_code"] = code
                rec["info"]["ended_at"] = now_str()
                rec["info"]["active"] = False
                self.history.append(dict(rec["info"]))
        rec = self._runs.get(key)
        ring = rec["ring"] if rec else RING
        try:
            RING.mirrors.remove(ring)        # 结束后本类目日志不再接收服务器级事件
        except ValueError:
            pass
        except Exception:  # noqa: BLE001
            pass
        killed = bool(rec["info"].get("killed")) if rec else False
        tag = "已手动中断" if killed else f"进程退出 code={code}"
        ring.push(f"■ [{tag}] pid={proc.pid} 结束")
        RING.push(f"■ [{cat}] {tag} pid={proc.pid}", mirror=False)
        # 运行结束清理临时配置: 保留最近 N 份供"⑦历史运行与恢复"查看/续跑(2026-09-04:
        # 不再正常结束即删——历史面板靠这些快照展示各类目进度; 限量清理最旧的)。
        # 多类目并行(2026-09-15): **在跑运行的快照不删**(否则可能删掉别类目正在用的那份)。
        if not killed:
            try:
                protect = {r["info"].get("effective_conf") for r in self._records()
                           if self._is_alive(r) and r["info"].get("effective_conf")}
                prune_run_snapshots(protect=protect)
            except Exception:
                pass

    def stop(self, cat: str | None = None) -> dict:
        """中断指定类目的运行; 不给 cat 时: 只有一个在跑就停它, 多个在跑则全停(旧兼容)。

        **2026-09-16(用户确认)**: 注册表里没有该类目的记录时, 不再直接回"没有运行中的进程",
        而是**回退按 `.run.lock` 里的 pid 强杀**(见 `_stop_orphan`) —— 看板重启后注册表清空,
        上一实例启动的子进程仍在跑(孤儿运行), 原实现下页面的「中断」对它是**空操作**。
        """
        targets: list[str] = []
        if cat:
            targets = [self.key_of(cat)] if self.active_for(cat) else []
            if not targets:
                return self._stop_orphan(cat)
        else:
            actives = [r for r in self._records() if self._is_alive(r)]
            if not actives:
                return {"ok": True, "note": "没有运行中的进程"}
            targets = [self.key_of(r["info"].get("cat"), r["info"].get("label")) for r in actives]
        results = []
        for key in targets:
            results.append(self._stop_one(key))
        if len(results) == 1:
            return results[0]
        killed = [r for r in results if r.get("pid")]
        return {"ok": True, "stopped": killed,
                "note": f"已中断 {len(killed)} 个类目的运行" if killed else "没有运行中的进程"}

    def _stop_orphan(self, cat: str) -> dict:
        """停掉**不是本服务启动的**存活运行(看板重启后留下的孤儿进程; 2026-09-16 用户确认)。

        背景: `RUNNER._runs` 是**内存注册表** —— 看板重启(尤其非优雅退出: 关窗口/任务管理器/
        `taskkill /F` 不带 `/T`)后它清空, 而上一实例启动的流水线子进程还在跑; 新实例既不认识它
        (不显示为受管运行), 页面「中断」也点不动(旧实现回"该类目没有运行中的进程")。
        现在回退到 `.run.lock`: 用 `probe_lock` 确认存活 → 读锁内 pid → `taskkill /PID <pid> /T /F`
        → 再用 `probe_lock` 复查(仍存活则二次强杀)→ 写"页面手动中断"告警(与停自己子进程同款文案,
        事件统计口径一致)。
        """
        droot = _cat_data_root(cat)
        root = cat_root_dir(cat, droot)
        # **先看有没有锁文件**: 绝大多数"确实没在跑"的调用在这里就返回, 连 `core.*` 都不导入
        # (理由见 `_cat_data_root` 的注释: Popen 被测试替身替换时 core 导入会炸且污染会话)。
        if not (root / ".run.lock").is_file():
            return {"ok": True, "note": "该类目没有运行中的进程"}
        from core.run_lock import probe_lock
        try:
            alive, started, _unknown = probe_lock(root, droot)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "note": f"探测该类目进程失败: {exc}"}
        if not alive:
            return {"ok": True, "note": "该类目没有运行中的进程"}
        pid = _lock_pid(cat, droot)
        if not pid:
            return {"ok": False,
                    "note": (f"该类目有存活进程(开始于 {started or '?'}), 但 `.run.lock` 里读不到 pid; "
                             f"请手动结束(任务管理器 / taskkill)")}
        ring = self.ring_for(cat) or RING
        msg = (f"■ [{cat}] 检测到**外部/孤儿运行**(非本服务启动, 多为看板重启前的实例遗留): "
               f"按 .run.lock 强杀进程树 pid={pid} …")
        ring.push(msg, level="WARN")
        if ring is not RING:
            RING.push(msg, level="WARN", mirror=False)
        killed = False
        for attempt in (1, 2):
            try:
                _kill_pid_tree(pid)
            except Exception as exc:  # noqa: BLE001
                ring.push(f"强杀 pid={pid} 异常: {exc}", level="ERROR")
            # 复查: 进程一死, 它的 OS 独占锁立刻释放 → probe_lock 判"没人持有"
            for _ in range(30):
                time.sleep(0.5)
                try:
                    still, _s, _u = probe_lock(root, droot)
                except Exception:  # noqa: BLE001
                    still = False
                if not still:
                    killed = True
                    break
            if killed:
                break
            ring.push(f"pid={pid} 第 {attempt} 次强杀 15s 后仍存活, 继续重试…", level="WARN")
        # 页面"中断"记录(与 `_stop_one` 同文案: ④监控与告警 + 事件统计都认这一行)
        try:
            alert_path = monitor_dir(cat, droot) / "monitor-alerts.txt"
            alert_path.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(alert_path, "a", encoding="utf-8") as fh:
                fh.write(f"{now} [WARN] 流水线被页面手动中断(进程树强杀 pid={pid}), "
                         f"进行中步骤将于下次运行标 INTERRUPTED 并自动重跑\n")
        except OSError:
            pass
        if not killed:
            ring.push(f"⚠ pid={pid} 强杀后仍存活(疑似冻结在原生调用); 请手动执行: "
                      f"taskkill /PID {pid} /T /F", level="ERROR")
            return {"ok": False, "pid": pid, "cat": cat, "orphan": True,
                    "note": f"孤儿运行 pid={pid} 强杀后仍存活, 请手动 taskkill /PID {pid} /T /F"}
        done = f"已强杀外部/孤儿运行(pid={pid}, 非本服务启动), 进行中的步骤将于下次运行重跑"
        ring.push(f"■ [{cat}] {done}", level="WARN")
        return {"ok": True, "pid": pid, "cat": cat, "orphan": True, "note": done}

    def _stop_one(self, key: str) -> dict:
        with self._lock:
            rec = self._runs.get(key)
            proc = rec["proc"] if rec else None
            pid = proc.pid if proc else None
            snap_cat = rec["info"].get("cat", "") if rec else ""
            snap_root = rec["info"].get("data_root", "data") if rec else "data"
        if proc is None or proc.poll() is not None:
            return {"ok": True, "note": "没有运行中的进程"}
        rec["ring"].push(f"■ [{key}] 收到手动中断请求, 正在终止进程树…", level="WARN")
        RING.push(f"■ [{key}] 收到手动中断请求, 正在终止进程树…", level="WARN", mirror=False)
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
        except Exception as exc:
            rec["ring"].push(f"中断失败(尝试直接结束): {exc}", level="ERROR")
            try:
                proc.kill()
            except Exception:
                pass
        with self._lock:
            rec["info"]["killed"] = True
        # 强杀后校验真死(2026-09-08, 方案B): 冻结在原生调用(Playwright/CDP)的进程
        # taskkill 可能未立即生效 → 复查, 仍在则二次强杀; 仍存活则明确告警
        # (防"页面显示已中止但进程还活着"成为孤儿)
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
        if proc.poll() is None:
            rec["ring"].push(f"pid={pid} 首次强杀 30s 后仍在, 二次强杀…", level="WARN")
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    proc.kill()
            except Exception as exc:
                rec["ring"].push(f"二次强杀异常: {exc}", level="ERROR")
            try:
                proc.wait(timeout=15)
            except Exception:
                pass
        if proc.poll() is None:
            rec["ring"].push(
                f"⚠ pid={pid} 强杀后仍存活(疑似冻结的原生调用); "
                f"请手动执行: taskkill /PID {pid} /T /F",
                level="ERROR",
            )
        # 页面"中断"(进程树强杀)写入监控与告警, 供 ④ 监控与告警 展示
        try:
            if snap_cat:
                alert_path = monitor_dir(snap_cat, snap_root) / "monitor-alerts.txt"
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                alert_path.parent.mkdir(parents=True, exist_ok=True)
                with open(alert_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{now} [WARN] 流水线被页面手动中断(进程树强杀 pid={pid}), "
                             f"进行中步骤将于下次运行标 INTERRUPTED 并自动重跑\n")
        except OSError:
            pass
        return {"ok": True, "pid": pid, "cat": snap_cat}


RUNNER = RunManager()


# ---------------------------------------------------------------- 监控文件读取
def monitor_dir(cat: str, data_root: str = "data") -> Path:
    """data_root 相对项目根; 返回 <root>/<cat>/20_备份与监控"""
    root = Path(data_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root / cat / "20_备份与监控"


def cat_root_dir(cat: str, data_root: str = "data") -> Path:
    """类目数据根目录 <root>/<cat>。"""
    root = Path(data_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root / cat


def _expand_artifact(step: int, template: str, cat: str) -> str:
    """把产物模板展开为类目内相对路径(CAT -> 类目名)。"""
    return template.replace("CAT", cat)


# ---------------------------------------------------------------- 整合市场报告(HTML) 一键生成
# 2026-10-09 用户要求: "三份报告缺一个不可, 缺其中一个都无法生成整合报告"; 入口放**类目中心**。
# 门槛与 scripts/build_integrated_report_html.py 完全一致(那边是唯一实现, 这里只做状态展示与触发)。
INTEGRATED_REPORT_SUBDIR = Path("20_整合市场报告") / "html"
INTEGRATED_REPORT_SCRIPT = Path("scripts") / "build_integrated_report_html.py"
INTEGRATED_BUILD_TIMEOUT = 900          # 秒: 2000+ 变体实测约 1~3 秒, 留足余量(含大表读取)


def safe_cat_name(cat: str) -> str:
    """类目名白名单校验(用于拼路径的接口, 防目录穿越: `../`、盘符、分隔符一律拒)。"""
    c = str(cat or "").strip()
    if not c or c in (".", ".."):
        raise UserFacingError("缺少类目名(cat)")
    if any(ch in c for ch in "\\/:*?\"<>|"):
        raise UserFacingError(f"非法类目名(含路径分隔符): {cat!r}")
    return c


def integrated_report_status(cat: str, data_root: str = "data") -> dict:
    """整合报告(HTML)状态: 三份输入齐不齐 + 成品在不在。

    输入(与脚本门槛同口径, 缺一不可):
      步骤8  `<CAT>-keyword-report.xlsx`
      步骤18 `18_标准化/<CAT>-属性标注-标准化交付.xlsx`
      外部提供 `19_VOC报告/<CAT>-VOC报告.xlsx`
    """
    cat_dir = cat_root_dir(cat, data_root)
    inputs = (
        (8, "关键词报告", cat_dir / f"{cat}-keyword-report.xlsx"),
        (18, "属性标注-标准化交付", cat_dir / "18_标准化" / f"{cat}-属性标注-标准化交付.xlsx"),
        (None, "VOC报告", cat_dir / "19_VOC报告" / f"{cat}-VOC报告.xlsx"),
    )
    missing = [{"step": s, "name": n, "path": str(p)} for s, n, p in inputs if not p.is_file()]
    out_dir = cat_dir / INTEGRATED_REPORT_SUBDIR
    html = out_dir / "index.html"
    html_info = None
    if html.is_file():
        st = html.stat()
        html_info = {"path": str(html), "size": st.st_size,
                     "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    return {"cat": cat, "inputs_ok": not missing, "missing": missing, "html": html_info,
            "step15_pdf": (cat_dir / f"{cat}-初步类目分析报告.xlsx").is_file(),
            "json": (out_dir / "report.json").is_file(),
            "log": (out_dir / "构建日志.txt").is_file(),
            "dir": str(out_dir), "progress": integrated_progress(cat, data_root)}


INTEGRATED_PROGRESS_NAME = ".build-progress.json"
INTEGRATED_PROGRESS_STALE_SEC = 180      # 进度文件多久没更新就算"已不在生成"
# 类目中心「⬇ 下载」的四项: kind → (相对类目根的源文件, 下载时的文件名)
INTEGRATED_DOWNLOADS = {
    "integrated": ("20_整合市场报告/html/index.html", "{cat} 市场调研整合报告.html"),
    "keyword": ("{cat}-keyword-report.xlsx", "{cat}-关键词报告.xlsx"),
    "product": ("18_标准化/{cat}-属性标注-标准化交付.xlsx", "{cat}-属性标注-标准化交付.xlsx"),
    "voc": ("19_VOC报告/{cat}-VOC报告.xlsx", "{cat}-VOC报告.xlsx"),
}


def integrated_download_path(cat: str, data_root: str, kind: str) -> Path | None:
    """`kind` → 实际文件路径(白名单之外返回 None)。供下载接口与测试复用。"""
    item = INTEGRATED_DOWNLOADS.get(str(kind or "").strip().lower())
    if item is None:
        return None
    rel, _name = item
    return cat_root_dir(cat, data_root) / rel.format(cat=cat)


def integrated_progress(cat: str, data_root: str = "data") -> dict:
    """读生成进度(由 `scripts/build_integrated_report_html.py` 写, 供类目中心按钮显示)。

    `running` 的判定 = 文件存在 + 未标 done + 时间戳未过期(进程被杀/看板重启留下的僵尸进度
    不该让按钮永远卡在"生成中")。
    """
    p = cat_root_dir(cat, data_root) / INTEGRATED_REPORT_SUBDIR.parent / INTEGRATED_PROGRESS_NAME
    if not p.is_file():
        return {"running": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    if not isinstance(data, dict):
        return {"running": False}
    age = None
    try:
        age = int(time.time() - p.stat().st_mtime)
    except OSError:
        pass
    stale = bool(age is not None and age > INTEGRATED_PROGRESS_STALE_SEC)
    data["age_sec"] = age
    data["stale"] = stale
    data["running"] = bool(not data.get("done")) and not stale
    return data


@lru_cache(maxsize=16)
def _step_declarations_cached(cat: str) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """`_step_declarations()` 的带缓存版本: 声明是静态的(只随 CAT 变), 但构建它要导入并实例化
    19 个步骤(冷启动含 pandas/openpyxl 等重依赖, 实测首次 ~1s+) —— 弹窗每次打开都算一遍太浪费。
    """
    return tuple((k, tuple(v)) for k, v in _step_declarations(cat).items())


def _step_declarations_fast(cat: str) -> dict[int, list[str]]:
    return {k: list(v) for k, v in _step_declarations_cached(cat)}


def _step_declarations(cat: str) -> dict[int, list[str]]:
    """每个步骤**声明**的产物(类目内相对路径) —— 来自步骤类自身的 `artifacts` +
    `extra_archive_files`(与流水线归档同步用的是同一份声明, 因此最权威)。

    为什么不用 webui 的白名单表: `ARTIFACT_TABLE` 只收"关键交付报表"(6 个), 而点击步骤卡片
    要展示的是**该步骤实际产出的文件**(含中间产物/审计件), 故改读步骤类声明; 读不到时退回白名单。
    """
    out: dict[int, list[str]] = {}
    try:
        from steps.registry import get_steps

        for s in get_steps():
            try:
                num = int(getattr(s, "number", -1))
            except (TypeError, ValueError):
                continue
            if num < 0:
                continue
            try:
                s._cat = cat            # artifacts 属性按 self._cat 拼类目名
            except Exception:  # noqa: BLE001
                pass
            try:
                decl = list(s.artifacts or []) + list(getattr(s, "extra_archive_files", []) or [])
            except Exception:  # noqa: BLE001
                decl = []
            out[num] = [str(x).replace("CAT", cat).replace("\\", "/") for x in decl]
    except Exception:  # noqa: BLE001
        out = {}
    for step, tpl in ARTIFACT_TABLE:                 # 兜底/补白名单里独有的项
        rel = _expand_artifact(step, tpl, cat).replace("\\", "/")
        if rel not in out.setdefault(step, []):
            out[step].append(rel)
    return out


def _claimed_flat_files(decls: dict[int, list[str]]) -> set[str]:
    """被任意步骤声明过的**类目根平铺文件**名(用于找"未归属产物")。"""
    out: set[str] = set()
    for rels in (decls or {}).values():
        for rel in rels:
            if "/" not in rel:
                out.add(rel)
    return out


def pick_snapshot(cat: str, data_root: str, name: str = "") -> Path | None:
    """选该类目的归档快照目录(2026-09-16: 步骤卡片改为"读归档"后的唯一数据源)。

    - `name` 指定时: **按精确模式校验** `<CAT>-<YYYYMMDD-HHMMSS>[-N]` 且必须是目录, 否则返回 None
      (绝不接受任意路径 —— 下载校验也复用它, 防目录穿越);
    - 缺省: 取该类目**最新一份**快照(与 `Paths.latest_snapshot` 同口径: 名字=时间戳升序取最后);
    - **不做任何创建**(`Paths.archive_step_dir` 会 mkdir, 这里只读, 不能凭空造目录)。
    """
    root = cat_root_dir(cat, data_root)
    arch = root.parent / "archive"
    if not cat or not arch.is_dir():
        return None
    pat = re.compile(rf"^{re.escape(str(cat))}-\d{{8}}-\d{{6}}(?:-\d+)?$")
    if name:
        cand = arch / str(name)
        try:
            if pat.match(str(name)) and cand.is_dir():
                return cand
        except OSError:
            pass
        return None
    dirs = sorted(
        (p for p in arch.iterdir() if p.is_dir() and pat.match(p.name)),
        key=lambda p: p.name,
    )
    return dirs[-1] if dirs else None


def _archive_step_files(snap: Path, step: int) -> tuple[str, list[dict]]:
    """快照内某步骤目录下的文件清单(name = **相对快照根**的路径, 供下载接口用)。"""
    try:
        from core.paths import STEP_DIR_NAMES
    except Exception:  # noqa: BLE001
        return "", []
    dname = STEP_DIR_NAMES.get(int(step), "")
    if not dname:
        return "", []
    d = snap / dname
    if not d.is_dir():
        return dname, []
    rows: list[dict] = []
    try:
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            rows.append({
                "name": str(p.relative_to(snap)).replace("\\", "/"),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "declared": True,      # 归档里的都算"该步骤的产物"(前端只分表用)
                "archive": True,
            })
    except OSError:
        pass
    return dname, rows


def step_files(cat: str, data_root: str, step: int,
               source: str = "live", snapshot: str = "") -> dict:
    """步骤卡片的内容(2026-09-16: 新增 `source="archive"` —— **读归档快照**, 界面的默认)。

    用户定调(2026-09-16): "改读取方式: 读取每个类目 archive 中每个步骤里的数据" →
    卡片不再按"步骤声明 + 实时目录扫描"拼, 而是**直接列该类目归档快照里该步骤目录的内容**:

      * 数据源 = `data/archive/<CAT>-<时间戳>/<NN_步骤名>/`(缺省取最新一份快照, 可用 `snapshot=` 指定);
      * 好处: 结构就是归档的真实结构(00~17 步骤目录), 不再有"未归属产物"这种桶;
      * 代价(已知): 归档是**每步跑完时**同步的副本 → 可能滞后于实时数据; 且**未被任何步骤声明
        的文件从来不会进归档**, 卡片里看不到它们(2026-09-16 用户明确要求: **连计数提示也不要**,
        卡片只呈现归档内容本身)。
      * `source="live"` 保留旧行为(声明产物 + 该步骤实时目录 + 未归属列表), 供排查/对比使用。
    """
    if str(source or "").strip().lower() != "archive":
        data = step_files_live(cat, data_root, step)
        data["source"] = "live"
        return data

    titled = ""
    desc = ""
    try:
        from steps.registry import get_steps

        for s in get_steps():
            if int(getattr(s, "number", -1)) == int(step):
                titled = str(getattr(s, "title", "") or "")
                desc = str(getattr(s, "description", "") or "")
                break
    except Exception:  # noqa: BLE001
        pass

    snap = pick_snapshot(cat, data_root, snapshot)
    snap_name = snap.name if snap else ""
    # 快照**创建时间**从目录名解析(`<CAT>-<YYYYMMDD>-<HHMMSS>[-N]`); **最近同步**取目录 mtime
    # (每步跑完都会往里同步文件 → mtime 即"最后一次归档写入时刻", 便于判断归档新旧)
    snap_created = ""
    snap_synced = ""
    if snap is not None:
        m = re.search(r"-(\d{8})-(\d{6})(?:-\d+)?$", snap_name)
        if m:
            snap_created = (f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} "
                            f"{m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}")
        try:
            snap_synced = datetime.fromtimestamp(snap.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            snap_synced = ""
    dname, rows = _archive_step_files(snap, step) if snap is not None else ("", [])
    # 该类目共有几份快照(便于判断"归档是不是旧的")
    snap_n = 0
    try:
        arch = cat_root_dir(cat, data_root).parent / "archive"
        pat = re.compile(rf"^{re.escape(str(cat))}-\d{{8}}-\d{{6}}(?:-\d+)?$")
        if arch.is_dir():
            snap_n = sum(1 for p in arch.iterdir() if p.is_dir() and pat.match(p.name))
    except OSError:
        snap_n = 0
    return {
        "step": int(step),
        "cat": cat,
        "title": titled,
        "desc": desc,
        "source": "archive",
        "dir": dname,
        "snapshot": snap_name,
        "snapshot_created": snap_created,
        "snapshot_synced": snap_synced,
        "snapshot_count": snap_n,
        "exists": bool(rows),
        "files": rows,
        "declared_count": len(rows),
        "extra_count": 0,
        "unclaimed": [],          # 归档模式不再有"未归属"分节(2026-09-16 用户要求: 连计数提示也不要)
    }


def step_files_live(cat: str, data_root: str, step: int) -> dict:
    """某步骤的产物清单(2026-09-15: 供"点击步骤卡片"弹窗使用)。

    合并两类来源(去重):
      ① **声明产物**: `_step_declarations()`(步骤类 artifacts + extra_archive_files);
      ② **该步骤实时目录**: `data/<CAT>/<STEP_DIR_NAMES[step]>/` 下的全部文件(递归) ——
         步骤 11/12/13/14/17 的下载件、日志、截图等"实际产出但不在声明里"的文件都在这里。
    另附 `unclaimed`: 类目根下带 `<CAT>` 前缀却没有任何步骤声明的文件(正常应为空, 防空漏展示)。
    """
    decls = _step_declarations_fast(cat)
    titled = ""
    desc = ""
    try:
        from steps.registry import get_steps

        for s in get_steps():
            if int(getattr(s, "number", -1)) == int(step):
                titled = str(getattr(s, "title", "") or "")
                desc = str(getattr(s, "description", "") or "")
                break
    except Exception:  # noqa: BLE001
        pass
    base = cat_root_dir(cat, data_root)
    dname = ""
    try:
        from core.paths import STEP_DIR_NAMES

        dname = STEP_DIR_NAMES.get(int(step), "")
    except Exception:  # noqa: BLE001
        dname = ""
    rows: list[dict] = []
    seen: set[str] = set()

    def _add(rel: str, declared: bool) -> None:
        rel = str(rel).replace("\\", "/")
        if not rel or rel in seen:
            return
        p = base / rel
        try:
            if not p.is_file():
                return
            st = p.stat()
        except OSError:
            return
        seen.add(rel)
        rows.append({
            "name": rel,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "declared": declared,
        })

    for rel in decls.get(int(step), []):
        _add(rel, True)
    if dname:
        d = base / dname
        if d.is_dir():
            try:
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        try:
                            _add(str(p.relative_to(base)), False)
                        except ValueError:
                            continue
            except OSError:
                pass
    unclaimed: list[dict] = []
    try:
        if cat and base.is_dir():
            claimed = _claimed_flat_files(decls)
            for p in sorted(base.iterdir()):
                if not p.is_file() or not p.name.startswith(cat) or p.name in claimed:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                # 未归属文件也要带**真实大小/时间**(2026-09-15: 之前只回文件名, 前端只能显示 "—",
                # 会被当成"坏行"; 用户就是看到 "—" 才来问的)
                unclaimed.append({
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "declared": False,
                })
    except OSError:
        pass
    declared_n = sum(1 for r in rows if r["declared"])
    return {
        "step": int(step),
        "cat": cat,
        "title": titled,
        "desc": desc,
        "dir": dname,
        "files": rows,
        "declared_count": declared_n,
        "extra_count": len(rows) - declared_n,
        "unclaimed": unclaimed,
    }


def list_artifacts(cat: str, data_root: str = "data") -> list[dict]:
    """扫描类目数据目录, 返回当前已存在的可下载产物列表。"""
    if not cat:
        return []
    base = cat_root_dir(cat, data_root)
    out: list[dict] = []
    seen: set = set()
    for step, template in ARTIFACT_TABLE:
        rel = _expand_artifact(step, template, cat)
        p = base / rel
        if not p.is_file():
            continue
        if rel in seen:
            continue
        seen.add(rel)
        try:
            size = p.stat().st_size
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            continue
        out.append({"step": step, "name": rel, "size": size, "mtime": mtime})
    return out


def _artifact_abs(cat: str, data_root: str, step: int, name: str,
                  source: str = "live", snapshot: str = "") -> Path | None:
    """校验下载请求: 必须是**类目数据目录内**(或**该类目归档快照内**)已存在的文件(防目录穿越)。

    2026-09-15: 原实现要求 `name` 精确命中 `ARTIFACT_TABLE` 白名单, 于是"点击步骤卡片"弹窗里
    能列出却下不了非关键交付件(截图/日志/中间产物)。现改为**只认"落在允许根目录内"**这条硬约束
    (真正的安全边界在这里), 白名单只用于列表归类。
    2026-09-16: 卡片改为"读归档"后新增 `source="archive"` —— 允许根变成**该类目的归档快照目录**
    (由 `pick_snapshot` 按精确模式解析, 绝不接受任意路径), 仍做包含性校验。
    """
    try:
        step = int(step)
    except (TypeError, ValueError):
        return None
    if not 0 <= step < STEP_COUNT:
        return None
    if str(source or "").strip().lower() == "archive":
        snap = pick_snapshot(cat, data_root, snapshot)
        if snap is None:
            return None
        base = snap
    else:
        base = cat_root_dir(cat, data_root)
    p = (base / name).resolve()
    base_resolved = base.resolve()
    try:
        inside = p.is_relative_to(base_resolved)
    except AttributeError:  # py3.8 兼容
        inside = str(p).startswith(str(base_resolved) + os.sep)
    if not inside:
        return None
    return p if p.is_file() else None


_CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
}


# ---------------- 关键词候选池(供「关键词清单」面板: 候选/搜索量/FR-07 判定) ----------------
# 2026-10-06: 原「步骤10 注入词」面板已删除 —— 步骤10 的判定参考词改为与
# 「关键词清单」面板的**核心大词列**同步(在该列勾选/手填即等于选注入词), 不再单独人工选一遍。
# 本函数只保留候选池 + 自动达标核心词的计算, 供 keyword_lists_panel() 复用。
def keyword_candidates(cat: str, data_root: str = "data") -> dict:
    """组装候选词池数据: {candidates:[{word, volume, verdict, qualified}], auto_qualified, site_scope}。

    auto_qualified = **自动态核心大词**(与步骤9 搜索收集 / 步骤10 判定参考词同口径):
      US 站   = `<CAT>-screen-results.json` 中 verdict=达标 的词全集(保持 screen 顺序);
      非 US 站 = 站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个候选词(缺搜索量的排除)。
    """
    if not cat:
        return {"ok": False, "error": "配置缺少 CAT(类目名)"}
    base = cat_root_dir(cat, data_root)
    # 1) 候选词文件(可能缺失: 步骤4 未跑)
    cand_path = base / f"{cat}-keywords-candidates.txt"
    candidates: list[str] = []
    if cand_path.is_file():
        candidates = [ln.strip() for ln in cand_path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]
    # 2) 挖词指标: word -> volume
    volume: dict[str, float | None] = {}
    cpc: dict[str, float | None] = {}
    kw_path = base / f"{cat}-amazon-keyword-data.json"
    if kw_path.is_file():
        try:
            rows = json.loads(kw_path.read_text(encoding="utf-8-sig", errors="replace"))
            for r in rows if isinstance(rows, list) else []:
                if not isinstance(r, dict):
                    continue
                w = str(r.get("keyword") or "").strip().lower()
                try:
                    volume[w] = float(r.get("monthly_search_volume"))
                except (TypeError, ValueError):
                    volume[w] = None
                try:
                    cpc[w] = float(r.get("cpc")) if r.get("cpc") is not None else None
                except (TypeError, ValueError):
                    cpc[w] = None
        except Exception:
            pass
    # 3) FR-07 判定: word -> verdict
    verdict: dict[str, str] = {}
    screen_path = base / f"{cat}-screen-results.json"
    if screen_path.is_file():
        try:
            rows = json.loads(screen_path.read_text(encoding="utf-8-sig", errors="replace"))
            for r in rows if isinstance(rows, list) else []:
                if not isinstance(r, dict):
                    continue
                verdict[str(r.get("keyword") or "").strip().lower()] = str(r.get("verdict") or "").strip()
        except Exception:
            pass
    # 4) 自动达标核心词(2026-09-15 起按站点分流, 与步骤9/10 同口径):
    #    US 站 = FR-07 全部达标词(screen 判定, 保持 screen 顺序);
    #    非 US 站 = **站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个**候选词(缺搜索量的排除)
    #    —— 与"步骤9 达标核心 / 步骤10 判定参考词"完全同一批, 避免三处口径不一致。
    auto_qualified: list[str] = []
    us_site = True
    try:
        from core.category_config import (
            NON_US_QUALIFIED_TOP as _TOP,
            is_us_site as _is_us,
            pick_top_volume_candidates as _pick,
        )
        _vals = load_yaml_file(base / "config.yaml")
        us_site = _is_us(site=str(_vals.get("SITE", "") or ""),
                         market=str(_vals.get("MARKET", "") or ""), cat=cat)
        if not us_site:
            _rows = []
            if kw_path.is_file():
                _rows = json.loads(kw_path.read_text(encoding="utf-8-sig", errors="replace"))
            auto_qualified = [w.lower() for w in _pick(_rows, _TOP)]
    except Exception:
        us_site, auto_qualified = True, []
    if us_site and screen_path.is_file():
        try:
            rows = json.loads(screen_path.read_text(encoding="utf-8-sig", errors="replace"))
            for r in rows if isinstance(rows, list) else []:
                if isinstance(r, dict) and str(r.get("verdict") or "").strip() == "达标":
                    w = str(r.get("keyword") or "").strip().lower()
                    if w and w not in auto_qualified:
                        auto_qualified.append(w)
        except Exception:
            pass
    # 候选词面板行(合并候选词文件; 若 screen 有候选文件外的达标词也补上)
    seen: set[str] = set()
    rows_out: list[dict] = []
    for w in candidates + [w for w in auto_qualified if w not in candidates]:
        wl = w.strip().lower()
        if not wl or wl in seen:
            continue
        seen.add(wl)
        rows_out.append({
            "word": wl,
            "volume": volume.get(wl),
            "cpc": cpc.get(wl),
            "verdict": verdict.get(wl, ""),
            "qualified": (wl in set(auto_qualified)) if not us_site else (verdict.get(wl, "") == "达标"),
        })
    return {
        "ok": True,
        "cat": cat,
        "candidates": rows_out,
        "auto_qualified": auto_qualified,
        "site_scope": "us" if us_site else "non_us",
    }


# ---------------- 关键词清单面板(三态: 自动 / 人工清单 / 人工停止, 2026-09-29) ----------------
# 核心大词与长尾词原先只有 FR-04 自动产物, 人工无法干预。本面板给核心大词/长尾词
# 三态语义(见 core/keyword_lists.py; 人工清单文件存在即生效, 删除=回自动, 另有"人工停止")。
# **2026-10-06**: 本面板「核心大词」列的生效词同时就是**步骤10 的判定参考词源**
# (原「步骤10 注入词」面板已删除), 因此"面板 / 步骤9 搜索 / 步骤10 判定"三处永远是同一批词。
def _auto_crawl_longtail(base: Path, cat: str, qualified_core: list[str],
                         auto_tail: list[str]) -> list[str]:
    """**自动态实际会抓**的长尾词（与步骤9 `_build_collect_list` 非人工分支**同口径**）。

    ★★ 为什么要有它（2026-10-12 用户口径 A："预填要给实际在用的那一批"）：
      面板里的「自动长尾词」是**候选池**（五维台账派生的 20-25 条），而自动态**实际只抓 5 条**
      —— 五维各取"频率最高的实体 × 搜索量最高的达标词"组出来的那 5 条。
      拿整份池子去预填，用户不改直接保存 → 转人工清单 → 抓取量从 5 变 25（页数/耗时/成本暴涨）。
      所以这里算出**真在抓的那几条**给面板预填，做到"所见 = 所用"。

    ⚠ **必须复用步骤9 自己的函数**，不能自己复刻：口径有好几层
      （基座=搜索量最高且字母序最小的达标词、每维取频率最高的实体、命中的配件/耗材词要剔除、
       台账缺失时回退"按相关度取最多 5 个"）—— 自己抄一遍必然漂移，那"所见=所用"就是假的。
    """
    try:
        from steps._keywords import crawl_words, parse_entity_artifact
        from steps.step09_search_collect import (
            _accessory_terms_from, _matches_accessory, _top_related_longtail,
        )
    except Exception:                                            # noqa: BLE001 — 拿不到就回空(前端退回旧行为)
        return []
    try:
        # ① 基座 = 达标核心词里**站内月搜索量最高**的一个（并列时按**字母序**取最小，与 step09 逐字一致）
        data = json.loads((base / f"{cat}-amazon-keyword-data.json").read_text(
            encoding="utf-8-sig", errors="replace")) if (base / f"{cat}-amazon-keyword-data.json").is_file() else []
        volume: dict[str, float] = {}
        for rec in (data if isinstance(data, list) else []):
            if isinstance(rec, dict):
                kw = str(rec.get("keyword") or "").strip()
                vol = rec.get("monthly_search_volume")
                if kw and isinstance(vol, (int, float)):
                    volume[kw] = float(vol)
        if not qualified_core:
            return []
        ranked = sorted(qualified_core, key=lambda w: (-volume.get(w, 0.0), w))
        base_word = ranked[0]
        # ② 五维台账 → 每维频率最高的实体 × 基座
        bad = _accessory_terms_from(json.loads(
            (base / f"{cat}-filter-prompt.json").read_text(encoding="utf-8-sig", errors="replace")
        ) if (base / f"{cat}-filter-prompt.json").is_file() else {})
        ent_path = base / f"{cat}-keywords-entities.txt"
        if ent_path.is_file():
            report = parse_entity_artifact(ent_path.read_text(
                encoding="utf-8-sig", errors="replace").splitlines())
            out = [w for _, _, w in crawl_words(
                report, base_word, reject=lambda w: _matches_accessory(w, bad))]
            if out:
                return out
        # ③ 台账缺失/无可用实体 → 与步骤9 完全一样地回退：按相关度取最多 5 个长尾词
        return list(_top_related_longtail(qualified_core, auto_tail, 5, bad))
    except Exception:                                            # noqa: BLE001
        return []


def _longtail_pool(base: Path, cat: str, qualified_core: list[str]) -> tuple[dict, dict]:
    """Expose every verified long-tail and separately labelled generated ideas."""
    import csv
    from steps._keywords import crawl_words, parse_entity_artifact
    from steps.step09_search_collect import _accessory_terms_from, _matches_accessory
    verified, generated = {}, {}
    source = base / f"{cat}-kwgen-candidates.csv"
    if source.is_file():
        with source.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                dim = str(row.get("dimension") or "").strip()
                word = str(row.get("keyword") or "").strip()
                if dim and word and str(row.get("is_longtail") or "").strip().lower() in ("true", "1") and row.get("relation") == "same_product":
                    verified.setdefault(dim, []).append(word)
    path = base / f"{cat}-keywords-entities.txt"
    if path.is_file() and qualified_core:
        report = parse_entity_artifact(path.read_text(encoding="utf-8-sig", errors="replace").splitlines())
        data_path = base / f"{cat}-amazon-keyword-data.json"
        rows = json.loads(data_path.read_text(encoding="utf-8-sig")) if data_path.is_file() else []
        volumes = {str(r.get("keyword") or ""): r.get("monthly_search_volume") for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
        base_word = sorted(qualified_core, key=lambda w: (-(float(volumes[w]) if isinstance(volumes.get(w), (int, float)) else 0), w))[0]
        policy_path = base / f"{cat}-filter-prompt.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8-sig")) if policy_path.is_file() else {}
        bad = _accessory_terms_from(policy)
        for dim, _, word in crawl_words(report, base_word, reject=lambda w: _matches_accessory(w, bad), per_dim=10000):
            generated.setdefault(dim, []).append(word)
    return verified, generated


def keyword_lists_panel(cat: str, data_root: str = "data") -> dict:
    """组装「关键词清单」面板数据(自动/人工/生效词 + 门槛判定 + 预计搜索量)。"""
    if not cat:
        return {"ok": False, "error": "配置缺少 CAT(类目名)"}
    from core.keyword_lists import (
        INJECT_SECTION,
        MAX_INJECT,
        MAX_NOINJECT,
        NOINJECT_SECTION,
        longtail_dimensions,
        read_keywords_file,
        resolve as resolve_kw,
        status as kw_status,
        used_head,
    )
    base = cat_root_dir(cat, data_root)
    inj = keyword_candidates(cat, data_root)
    if not inj.get("ok"):
        return inj
    vals = load_yaml_file(base / "config.yaml")
    try:
        gate = max(1, int(float(vals.get("GATE_MIN_QUALIFIED", 2) or 2)))
    except (TypeError, ValueError):
        gate = 2
    try:
        core_pages = max(1, int(float(vals.get("CORE_SEARCH_PAGES", 5) or 5)))
    except (TypeError, ValueError):
        core_pages = 5
    try:
        tail_pages = max(1, int(float(vals.get("LONGTAIL_SEARCH_PAGES", 4) or 4)))
    except (TypeError, ValueError):
        tail_pages = 4
    auto_tail: list[str] = []
    kw_path = base / f"{cat}-keywords.txt"
    if kw_path.is_file():
        _, auto_tail = read_keywords_file(kw_path)
    verdict_flat = {str(r.get("word") or ""): str(r.get("verdict") or "")
                    for r in (inj.get("candidates") or [])}
    plan = resolve_kw(base, cat,
                      auto_core=inj.get("auto_qualified") or [],
                      auto_longtail=auto_tail,
                      screen_verdict=verdict_flat,
                      gate=gate,
                      gate_applies=inj.get("site_scope") == "us")
    try:
        verified_tail, generated_tail = _longtail_pool(base, cat, list(inj.get("auto_qualified") or []))
    except (OSError, ValueError, TypeError):
        verified_tail, generated_tail = {}, {}
    # 抓取词条数: 人工长尾清单生效时就是清单本身; 自动态由五维实体组装, 最多 5 条
    crawl_manual = bool(plan.manual_longtail)
    crawl_n = len(plan.longtail) if crawl_manual else min(5, len(plan.longtail))
    pages = len(plan.core) * core_pages + crawl_n * tail_pages
    return {
        "ok": True,
        "cat": cat,
        "gate": gate,
        "site_scope": inj.get("site_scope"),
        "core_pages": core_pages,
        "tail_pages": tail_pages,
        "mode": plan.mode,
        "mode_label": plan.mode_label,
        "stopped": plan.stopped,
        "core": {
            "auto": list(inj.get("auto_qualified") or []),
            "manual": list(plan.manual_core),
            "effective": list(plan.core),
            "qualified": plan.qualified,
            "no_verdict": plan.no_verdict,
            "gate_ok": bool(plan.gate_ok) and not plan.stopped,
            "source_text": plan.source_text(),
            "gate_text": plan.gate_text(),
        },
        "longtail": {
            "auto": list(auto_tail),
            "manual": list(plan.manual_longtail),
            "effective": list(plan.longtail),
            # ★ 自动态**实际会抓**的那几条（五维组装，≤5）；面板预填用它，做到"所见=所用"。
            #   ⚠ 别拿 `auto`(候选池 20-25 条)去预填 —— 直接保存会把抓取量从 5 放大到 N。
            "crawl": _auto_crawl_longtail(base, cat, list(inj.get("auto_qualified") or []),
                                          auto_tail),
            "dimensions": longtail_dimensions(base, cat),
            "verified_pool": verified_tail,
            "generated_pool": generated_tail,
            "source_text": plan.longtail_text(),
        },
        # 注入词三块(2026-10-12 用户口径 A)：
        #   `words`    = `[仅注入]`(手加, 只判定不抓取)
        #   `excluded` = `[不注入]`(从注入里**扣掉**, 但**仍是核心大词、仍被抓取**)
        #   `effective_inject` = 合并后的**实际注入词**(= 核心 − excluded + words), 面板照它显示
        #   `core_part` = 上面那批里**来自核心大词**的部分(面板给它们打 `核心` 标记)
        "inject_only": {
            "words": list(plan.inject_only),
            "excluded": list(plan.no_inject),
            "effective_inject": list(plan.inject_words),
            "core_part": [w for w in plan.inject_words if w in set(plan.core)],
            "max": MAX_INJECT,
            "max_excluded": MAX_NOINJECT,
            "section": INJECT_SECTION,
            "excluded_section": NOINJECT_SECTION,
            "hint": ("仅注入词: 只作步骤10 的判定参考词, **不会被步骤9 抓取**、不计入门槛 —— "
                     "也不受三态影响(自动态下同样生效)。保存在同一文件的 "
                     "`[仅注入]` 分区。"),
            "hint_excluded": (f"不注入词: 从这里扣掉的词**仍然是核心大词、仍然被步骤9 抓取**, "
                              f"只是**不再参与步骤10 的判定**。保存在同一文件的 "
                              f"`{NOINJECT_SECTION}` 分区。"),
        },
        "candidates": inj.get("candidates") or [],
        "auto_qualified": inj.get("auto_qualified") or [],
        "files": kw_status(base, cat),
        "used_head": used_head(base, cat),
        "search_plan": {
            "core_words": len(plan.core),
            "crawl_words": crawl_n,
            "crawl_manual": crawl_manual,
            "pages": pages,
            "text": (f"核心 {len(plan.core)} 词 × {core_pages} 页 + "
                     f"长尾 {crawl_n} 词 × {tail_pages} 页 ≈ {pages} 页"
                     + ("(长尾=人工清单)" if crawl_manual else "(长尾由五维实体组装, 最多 5 条)")),
        },
    }


def save_keyword_lists(cat: str, data_root: str, kind: str, mode: str, words=None) -> dict:
    """保存核心/长尾人工清单。mode='auto' 删除清单文件(回自动态); 空清单等价回自动。"""
    if not cat:
        return {"ok": False, "error": "配置缺少 CAT(类目名)"}
    from core.keyword_lists import set_list
    base = cat_root_dir(cat, data_root)
    base.mkdir(parents=True, exist_ok=True)
    return set_list(base, cat, kind, mode, words)


def save_keyword_stop(cat: str, data_root: str, stopped: bool) -> dict:
    """设置/清除「人工停止」标记(只写时间戳, **不需要填理由**)。"""
    if not cat:
        return {"ok": False, "error": "配置缺少 CAT(类目名)"}
    from core.keyword_lists import set_stop
    base = cat_root_dir(cat, data_root)
    base.mkdir(parents=True, exist_ok=True)
    return set_stop(base, cat, bool(stopped))


def parse_step_status(path: Path) -> dict:
    """step-status.txt -> {num: {status,start,end,reason,acc}}"""
    out = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return out
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        try:
            num = int(parts[0])
        except ValueError:
            continue
        entry = {
            "status": parts[1] or "PENDING",
            "start": parts[2],
            "end": parts[3],
            "reason": parts[4] if len(parts) > 4 and parts[4] else "",
            "acc": 0,
            "fails": 0,
            "ints": 0,
        }
        if len(parts) > 5:
            try:
                entry["acc"] = int(float(parts[5]))
            except (TypeError, ValueError):
                entry["acc"] = 0
        if len(parts) > 6:
            try:
                entry["fails"] = int(float(parts[6]))
            except (TypeError, ValueError):
                entry["fails"] = 0
        if len(parts) > 7:
            try:
                entry["ints"] = int(float(parts[7]))
            except (TypeError, ValueError):
                entry["ints"] = 0
        out[num] = entry
    return out


def tail_text_lines(path: Path, n: int = 300, max_bytes: int = 200_000) -> list:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                head = fh.readline()  # 丢弃可能截断的首行
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        return [ln for ln in text.splitlines() if ln.strip()]
    except Exception:
        return []


def parse_log_lines(lines: list) -> list:
    """把日志行解析为 {ts, level, text}。截断由调用方决定(2026-09-08:
    原内部固定 lines[-300:] 导致"完整日志"接口也只能拿到 300 行, 已移除)。"""
    out = []
    for ln in lines:
        m = LOG_LINE_RE.match(ln)
        if m:
            out.append({"ts": m.group(1), "level": m.group(2), "text": m.group(3)})
        else:
            out.append({"ts": "", "level": "INFO", "text": ln})
    return out


def read_health(mdir: Path) -> dict:
    p = mdir / "health-report.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return {}


# 卡片 FAILED/INTERRUPTED 次数 = 按监控日志累计的事件次数(兼容升级前旧格式
# 状态文件无计数列的历史; 重跑/重置不清零, 清空日志文件才归零)。
# 事件来源两个文件(同事件会双写, 按身份去重):
#   - pipeline-progress.log: 步骤失败 ERROR / 强杀残留报告 WARNING / Ctrl+C 标记
#   - monitor-alerts.txt:     步骤失败 CRIT / 强杀残留报告 WARN / 页面手动中断(仅此文件)
# 2026-09 修复:
#   - 页面手动中断行(仅 alerts)计入; 其对应的强杀残留报告不再重复计(区间判定)
#   - 强杀残留报告按"开始于 <ts>"去重(同一残留被多轮重复报告只算 1 次)
#   - 新版文案兼容: "上次运行被进程强杀/终止" 与旧版 "上次运行被终止"
_RE_STEP_FAIL = re.compile(r"步骤\s+\d{1,2}\s+.*失败:")
_RE_STALE = re.compile(r"步骤\s+\d{1,2}\s+.*上次运行被(?:进程强杀/)?终止\(开始于\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_RE_CTRL = re.compile(r"已被标记 INTERRUPTED")
_RE_PAGE_STOP = re.compile(r"流水线被页面手动中断")
_EVENT_SCAN_BYTES = 8_000_000
_EVENT_CACHE: dict = {}   # ((progress_mtime, alerts_mtime)) -> (fails, ints)
_EVENT_RECENT: dict = {}  # status polling only: path -> (monotonic time, result)


def _event_scan_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > _EVENT_SCAN_BYTES:
                fh.seek(size - _EVENT_SCAN_BYTES)
                fh.readline()
            else:
                fh.seek(0)
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _ts_of(line: str) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    return m.group(1) if m else ""


def log_event_counts(mdir: Path, max_age: float = 0) -> tuple[int, int]:
    """统计 失败事件次数 与 中断事件次数(去重, 口径见模块注释)。"""
    prog = mdir / "pipeline-progress.log"
    alert = mdir / "monitor-alerts.txt"
    recent_key = str(mdir)
    recent = _EVENT_RECENT.get(recent_key)
    if max_age > 0 and recent and time.monotonic() - recent[0] < max_age:
        return recent[1]
    try:
        # 2026-09-11 修复: 缓存键**必须带上文件路径** —— 原来只有 (progress_mtime, alerts_mtime),
        # 而不同类目的监控文件很容易落在同一时间戳(NTFS 时间戳粒度 ~15ms, 连续写入会撞上),
        # 于是 A 类目会拿到 B 类目的计数(测试里表现为"同一用例交替失败")。
        prog_stat, alert_stat = prog.stat(), alert.stat()
        key = (str(prog), prog_stat.st_mtime_ns, prog_stat.st_size,
               str(alert), alert_stat.st_mtime_ns, alert_stat.st_size)
    except OSError:
        return 0, 0
    cached = _EVENT_CACHE.get(key)
    if cached:
        _EVENT_RECENT[recent_key] = (time.monotonic(), cached)
        return cached
    if len(_EVENT_CACHE) > 64:      # 简单上界: 避免长期运行无限增长
        _EVENT_CACHE.clear()

    fails = 0
    # 页面手动中断时间集合(alerts 独有; 其后的强杀残留报告不再重复计)
    page_stops: set[str] = set()
    # 强杀残留报告: {开始于 ts: 报告行 ts}(两文件同事件双写, 按 start 去重)
    stale: dict[str, str] = {}
    ctrl_n = 0
    # FAILED 以 progress 的 ERROR 行为准(alerts 的 CRIT 是同事件双写, 避免翻倍)
    for ln in _event_scan_text(prog).splitlines():
        if _RE_STEP_FAIL.search(ln):
            fails += 1
        elif _RE_CTRL.search(ln):
            ctrl_n += 1
        else:
            m = _RE_STALE.search(ln)
            if m:
                start = m.group(1)
                if start not in stale:
                    stale[start] = _ts_of(ln)
    for ln in _event_scan_text(alert).splitlines():
        if _RE_PAGE_STOP.search(ln):
            page_stops.add(_ts_of(ln))
        elif _RE_STALE.search(ln):
            m = _RE_STALE.search(ln)
            start = m.group(1)
            if start not in stale:
                stale[start] = _ts_of(ln)

    # 残留报告若其 (开始于, 报告) 区间内存在页面手动中断 → 该中断已由页面行计入, 不重复计;
    # 否则(进程自行崩溃/外部终止)由报告行计入。
    stale_n = 0
    for start, rep_ts in stale.items():
        if any(page >= start and page < rep_ts for page in page_stops):
            continue
        stale_n += 1
    ints = len(page_stops) + ctrl_n + stale_n
    _EVENT_CACHE[key] = (fails, ints)
    if len(_EVENT_RECENT) > 128:
        _EVENT_RECENT.clear()
    _EVENT_RECENT[recent_key] = (time.monotonic(), (fails, ints))
    return fails, ints


# INTERRUPTED 步骤的结束时刻推断: 状态文件被强杀时无结束时间,
# 从 pipeline-progress.log 取"该步骤最后一条日志的时间"作为近似中断时刻。
_LOGSTEP_CACHE: dict = {}   # (monitor_dir, num) -> (log_mtime_ns, end_ts)
_MAX_LOG_SCAN = 4_000_000   # 只扫日志尾部 4MB(单轮足够, 跨轮取最后一次出现)


def _is_step_activity(line: str, num: int) -> bool:
    """判断日志行是否属于该步骤的**真实执行活动**(而非启动/预检/归档等编排行)。"""
    if f"[Step{num:02d}" in line:          # 步骤类名日志, 如 [Step11TrafficSource]
        return True
    if re.search(r"=== 步骤\s*%d\s+" % num, line):   # === 步骤 11 xx 开始 ===
        return True
    if re.search(r"步骤\s*%d\s+(完成|失败|跳过)" % num, line):  # 步骤结束行
        return True
    return False


def step_last_log_ts(mdir: Path, num: int, since_ts: str = "") -> str:
    """返回日志中该步骤真实执行活动在 since_ts 之后最后一次出现的时间。

    用于被中断步骤(状态文件无结束时间)的结束时刻近似: 取其最后一条
    [StepNN...] / '步骤 NN …' 日志时间(≈中断前最后动作)。排除
    '断点续跑/从步骤 N 开始执行/上次运行被终止' 等编排启动行。
    """
    key = (str(mdir), num)
    p = mdir / "pipeline-progress.log"
    if not p.exists():
        return ""
    try:
        mtime_ns = p.stat().st_mtime_ns
    except Exception:
        return ""
    cached = _LOGSTEP_CACHE.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    try:
        with open(p, "rb") as fh:
            size = fh.seek(0, 2)
            if size > _MAX_LOG_SCAN:
                fh.seek(size - _MAX_LOG_SCAN)
                fh.readline()  # 丢弃可能截断的首行
            else:
                fh.seek(0)
            data = fh.read()
    except Exception:
        return ""
    text = data.decode("utf-8", errors="replace")
    best = ""
    for ln in text.splitlines():
        m = LOG_LINE_RE.match(ln)
        if not m:
            continue
        ts = m.group(1)
        if since_ts and ts < since_ts:
            continue
        if _is_step_activity(ln, num):
            best = ts
    _LOGSTEP_CACHE[key] = (mtime_ns, best)
    return best


def _cat_cumulative_sec(mdir: Path, cat: str, live_started: str = "") -> int:
    """该类目的**累计执行时长**(秒) —— 供类目中心列表的「累计运行时长」列。

    与 ②流程状态 的 `durations.total_sec`(以及流水线日志完成行)走**同一份实现**
    `core.state.RunState.cumulative_seconds()`。

    **2026-09-16 修(用户报障「运行中 类目中心的累计运行时长 与 运行台的累计执行时长 不统一,
    跑完就正常了」)**: `end_hint_for`(把 RUNNING 步骤截断到"日志最后活动时刻")**只对残留步骤用**。
    该 hint 本来是为"进程已死、日志停了"的残留 RUNNING 设计的; 但原先**无条件**传进去, 于是
    **真正在跑**的步骤也被截断 → 总量只在该步写一条日志时才跳一次(步骤10 约 4 秒; 步骤16 约
    135 秒 → 会冻结两分多钟), 而两个页面在不同瞬间取数 → 看着"两个数打架"; 步骤一跑完就有
    `end`, 走 `end-start` → 两边立刻一致(正是用户描述的"跑完就正常")。
    现在: `live_started` 传"本类目存活进程的启动时刻" → `start >= live_started` 的 RUNNING 步骤
    属于**当前进程段、真在跑** → 不给 hint(按 now 每秒计); 更早的(残留)才截断到日志活动时刻。
    缺省 `live_started=""`(= 判不出/没有存活进程) → 全部按残留处理, 即**旧行为**, 保守不漏计。
    任何异常都返回 0(单行算不出不得让整个列表失败)。
    """
    try:
        from core.state import RunState
        st = RunState(mdir / "step-status.txt", mdir / "step-duration.csv")

        def _hint(num: int) -> str:
            s = str(st.steps.get(num, {}).get("start", "") or "")
            if live_started and s and s >= str(live_started):
                return ""                 # 属于当前存活进程段 → 真在跑 → 按 now 计
            return step_last_log_ts(mdir, num)

        return int(st.cumulative_seconds(end_hint_for=_hint))
    except Exception:
        return 0


# LLM 用量/费用(2026-09-28): 看板展示"这类目本轮烧了多少 token / 花了多少钱"。
# 取数**只读**账本(不 new meter、不联网、不触发价格抓取 —— 价表快照就在账本首行 meta 里)。
_USAGE_CACHE: dict = {}      # str(mdir) -> (指纹, usage dict)


def _usage_fingerprint(mdir: Path) -> tuple:
    """账本文件的 (名字, mtime_ns, size) 指纹 —— 任何一个变了就重算。"""
    from core.usage import ledger_paths        # 延迟导入, 与 _cat_cumulative_sec 同风格

    out = []
    for p in ledger_paths(str(mdir)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((os.path.basename(p), st.st_mtime_ns, st.st_size))
    return tuple(out)


def cat_usage(mdir: Path | None) -> dict:
    """该类目**最近一轮**的 LLM 用量与费用; 无账本返回 `{}`。

    **为什么必须缓存(2026-09-28 实测)**: 开销几乎全在 JSON 解析上 ——
    1.9MB/5k 次调用 ≈ 25ms, 7.7MB/20k ≈ 97ms, 19.3MB/50k ≈ 245ms(聚合本身只 1-11ms)。
    而 `/api/history` 每 12s 静默刷一次、要轮 12 个类目 → 不缓存最坏 ~2.9s/次, 会把看板拖卡。
    这里按账本 (mtime,size) 指纹缓存: 运行中只有 1-3 个类目在写, 其余全部命中缓存。

    返回结构(全部 JSON 友好):
      run_id / calls / total_tokens / cost_cny / in_hit / in_miss / out /
      currency / price_source / unpriced_calls / failed_attempts /
      by_channel{通道:合计} / by_model{通道/模型:合计} / steps{步骤号:该步合计(含 by_channel)}
      + **累计口径(2026-09-29)**: 上面的 `calls/total_tokens/cost_cny` 与 `steps` 是
        **该类目累计**(历史轮次 + 本次 + 重跑 + 中断, 不因重置清零); 本轮(最近 run_id)
        另给 `run_calls / run_total_tokens / run_cost_cny`, 每步另给
        `run_calls / run_total_tokens / run_cost_cny / attempts / last_ts / status`。
    """
    if mdir is None:
        return {}
    key = str(mdir)
    fp = _usage_fingerprint(mdir)
    if not fp:
        _USAGE_CACHE.pop(key, None)
        return {}
    hit = _USAGE_CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]
    try:
        from core.usage import ledger_paths, load_records, summarize_category

        records: list[dict] = []
        for path in ledger_paths(str(mdir)):
            records.extend(load_records(path))
        if not records:
            _USAGE_CACHE.pop(key, None)
            return {}
        agg = summarize_category(records)
        t = agg["totals"]
        if not t["calls"] and not t["failed_attempts"]:
            _USAGE_CACHE.pop(key, None)
            return {}
        rt = agg.get("run_totals") or {}
        steps = {
            str(num): {
                "calls": row["calls"], "total_tokens": row["total_tokens"],
                "cost_cny": row["cost_cny"],
                "failed_attempts": row["failed_attempts"],
                "no_usage_calls": row.get("no_usage_calls", 0),
                "unpriced_calls": row.get("unpriced_calls", 0),
                "by_channel": row.get("by_channel") or {},
                # 展示用(2026-09-29): 累计为主, 本轮该步 + 尝试次数/最近一次时间备查
                "run_calls": row.get("run_calls", 0),
                "run_total_tokens": row.get("run_total_tokens", 0),
                "run_cost_cny": row.get("run_cost_cny", 0.0),
                "attempts": row.get("attempts", 0),
                "last_ts": row.get("last_ts", ""),
                "status": row.get("status", ""),
            }
            for num, row in (agg["steps"] or {}).items()
        }
        data = {
            "run_id": agg["run_id"],
            "cat": agg["cat"],
            "currency": agg["currency"],
            "price_source": agg["price_source"],
            "calls": t["calls"],
            "total_tokens": t["total_tokens"],
            "cost_cny": t["cost_cny"],
            "in_hit": t["in_hit"],
            "in_miss": t["in_miss"],
            "out": t["out"],
            "failed_attempts": t["failed_attempts"],
            "unpriced_calls": t["unpriced_calls"],
            "no_usage_calls": t.get("no_usage_calls", 0),
            "by_channel": agg.get("totals_by_channel") or {},
            "by_model": agg.get("totals_by_model") or {},
            "steps": steps,
            # 本轮口径(2026-09-29): 与"累计"并列, 便于对照"这一类目总共花了多少 / 这一轮花了多少"
            "run_calls": int(rt.get("calls") or 0),
            "run_total_tokens": int(rt.get("total_tokens") or 0),
            "run_cost_cny": float(rt.get("cost_cny") or 0.0),
            "runs": len(agg.get("runs") or []),
            "orphan_calls": int(agg.get("orphan_calls") or 0),
        }
    except Exception:                       # noqa: BLE001 — 监控不得让接口 500
        return {}
    _USAGE_CACHE[key] = (fp, data)
    return data


def usage_lite(usage: dict) -> dict:
    """类目中心只要合计(不算 per-step 投影) —— 明细留给运行台 ② 与 tooltip。

    2026-09-29 起合计为**累计口径**(该类目历史 + 本次, 不因重置清零), 另带本轮的
    `run_*` 三个数供 tooltip 对照。
    """
    if not usage:
        return {}
    return {k: usage.get(k) for k in
            ("run_id", "calls", "total_tokens", "cost_cny",
             "run_calls", "run_total_tokens", "run_cost_cny",
             "by_channel", "currency", "price_source")}


def _lock_live_started(cat: str, data_root: str) -> tuple[bool, str | None, bool]:
    """按 data/<CAT>/.run.lock 判断是否有**存活**流水线进程(web/CLI 都写锁)。

    2026-09-14 起判活口径统一到 `core/run_lock.py::probe_lock`:
      ① 试拿 OS 独占文件锁 —— 拿到=没人持有(残留锁), 立即释放;
      ② 拿到后再用 psutil 复核文件里记录的 pid(兼容旧版"只写文件不持锁"的进程);
      ③ 文件锁不可用 → pid 三态判定, 仍无法判定时按"活跃度是否超 TAKEOVER_MINUTES"推断,
         并把"无法判定"标记返回给调用方(界面可据此提示, 避免把真运行显示成中断)。

    返回 (是否存活, 锁内启动时间|None, 是否无法判定)。
    """
    try:
        from core.run_lock import probe_lock
        root = os.path.abspath(os.path.join(data_root, cat))
        return probe_lock(root, data_root)
    except Exception:
        return False, None, False


def _cat_data_root(cat: str) -> str:
    """找某类目的 DATA_ROOT(扫 `data/*/config.yaml` 匹配 CAT); 找不到按缺省 "data"。

    2026-09-16: 「中断」需要按类目名定位 `.run.lock`(用于停"不是本服务启动的"孤儿运行),
    而 `stop(cat)` 只拿到类目名 —— 配置里怎么写 DATA_ROOT 就怎么用(相对路径按项目根解析)。

    ⚠️ **故意不 import `core.category_config`**: `core/__init__.py` 会连带导入 `.browser`
    → playwright → asyncio, 而 asyncio 在"`subprocess.Popen` 被测试替身替换"的窗口里**无法导入**
    (`asyncio/windows_utils.py: class Popen(subprocess.Popen)` 直接 TypeError), 且首次失败会弄脏
    `sys.modules`, 让同一会话后续所有 `core.*` 导入跟着崩。这里只用 glob + yaml 读取, 零重依赖。
    """
    try:
        base = PROJECT_ROOT / "data"
        for cfg in sorted(base.glob("*/config.yaml")):
            try:
                vals = load_yaml_file(cfg) or {}
            except Exception:  # noqa: BLE001
                continue
            if str(vals.get("CAT", "") or "").strip() == str(cat or "").strip():
                return str(vals.get("DATA_ROOT", "") or "") or "data"
    except Exception:  # noqa: BLE001
        pass
    return "data"


def _lock_pid(cat: str, data_root: str) -> int | None:
    """读 `.run.lock` 里记录的流水线 pid(锁正文首行; 无锁/格式异常 → None)。

    用于停"孤儿运行": 注册表(RUNNER 内存)在**看板重启后清空**, 只有锁文件还认得出那个进程。
    """
    try:
        path = cat_root_dir(cat, data_root) / ".run.lock"
        first = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        for line in first:
            line = line.strip()
            if line.isdigit():
                return int(line)
    except OSError:
        return None
    return None


def _kill_pid_tree(pid: int) -> None:
    """按 pid 强杀整棵进程树(Windows: `taskkill /PID <pid> /T /F`; 其它: SIGTERM)。

    与停自己子进程(`_stop_one`)同款强杀方式; 差别是这里没有 Popen 句柄可 poll,
    存活与否由 `core/run_lock.probe_lock`(OS 独占锁 + pid)复查。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        import signal as _signal  # noqa: PLC0415
        os.kill(pid, _signal.SIGTERM)


def site_market_warnings(vals: dict) -> list[str]:
    """站点三口径(CAT 站点后缀/SITE/MARKET)不一致的提示(2026-09-14): 只提示不阻断。

    混用会静默产出"某国关键词/趋势数据 + 另一国商品页面"; SITE 写成非卖家精灵站点名时
    `core/mcp.py::_marketplace()` 还会静默回落成美国站(US)。规则与文案都在
    `core/category_config.py::check_site_market_consistency()`。
    """
    try:
        from core.category_config import check_site_market_consistency as _check_sm
        return _check_sm(str(vals.get("CAT", "") or ""), str(vals.get("SITE", "") or ""),
                         str(vals.get("MARKET", "") or ""))
    except Exception:
        return []


def _recent_monitor_activity(cat: str, data_root: str, minutes: float = 5.0) -> bool:
    """监控目录(日志/状态文件)在最近 N 分钟内是否有写入 —— 锁状态"无法判定"时的兜底证据。"""
    try:
        d = monitor_dir(cat, data_root)
    except Exception:
        return False
    limit = time.time() - max(1.0, float(minutes)) * 60
    for name in ("pipeline-progress.log", "step-status.txt", "step-duration.csv"):
        try:
            if os.path.getmtime(os.path.join(str(d), name)) >= limit:
                return True
        except OSError:
            continue
    return False


def _residual_running(run_active: bool, run_started: str, start: str) -> bool:
    """RUNNING 步骤是否属于"残留"(上次进程遗留、本次并没在跑) —— **唯一判定实现**。

    2026-09-16 用户报障("UK 的不是已经断了吗, 工作台卡片上还显示 running")驱动:
    运行台 `status_snapshot` 与类目中心 `_api_history` 原先各写一套"在跑"判定, 口径不同 →
        · 运行台: `run_active = RUNNER 记录 or .run.lock 存活`(锁/pid 兜底, 重启看板后仍准确)
        · 类目中心: 只看 `RUNNER.active_cats()`(**内存注册表**) → 重启看板后注册表清空,
          仍在跑的"孤儿运行"(上一实例启动、新实例不认识)被强行改判成 INTERRUPTED
    实测(2026-09-16 17:2x): UK 步骤16 真在跑(pid 10116 存活、日志 17:24:05 写 380/984),
    运行台显示 RUNNING 而类目中心显示"卡在步骤16(INTERRUPTED)"。现两处共用本函数。

    规则(与原运行台实现逐字等价):
      · 无存活进程(`run_active=False`)            → 残留;
      · 有存活进程且知道其启动时刻, 步骤开始得更早 → 残留(上一段进程的遗留, 本次尚未轮到);
      · 其余(含"判不出存活进程启动时刻")           → 不算残留(保持 RUNNING)。
    """
    if not run_active:
        return True
    if run_started and start:
        try:
            t_start = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
            t_run = datetime.strptime(run_started, "%Y-%m-%d %H:%M:%S")
            return t_start < t_run
        except (TypeError, ValueError):
            return False
    return False


def status_snapshot(cfg_values: dict, stream_after: int | None,
                    run_active: bool = True, run_started: str | None = None,
                    ring: "RingLog | None" = None) -> dict:
    """cfg_values: 用于定位监控目录的有效配置(CAT/DATA_ROOT)。
    ring: 实时日志缓冲 —— **多类目并行时传该类目自己的缓冲**(2030-09-15),
    缺省用全局缓冲(CLI 启动的运行没有本服务的运行记录, 只能看服务器级事件)。

    残留 RUNNING 判定(2026-09-09 修复"步骤16 残留仍显示 RUNNING"):
    - 本服务无进程在跑(run_active=False) -> RUNNING 全标 INTERRUPTED(旧逻辑);
    - 本服务有进程在跑, 但某 RUNNING 步骤的**开始时间早于本次运行启动时间**
      (run_started) -> 是**上次进程强杀/中断遗留**的残留(本次运行还没轮到它,
      比如"仅运行步骤10"时昨晚残留的步骤16) -> 同样标 INTERRUPTED, 不再误显 RUNNING;
    - 只有开始时间 >= 本次运行启动的 RUNNING 步骤才真在跑(前端实时计时)。
    """
    ring = ring or RING
    cat = str(cfg_values.get("CAT", "") or "").strip()
    data_root = str(cfg_values.get("DATA_ROOT", "data") or "data")
    mdir = monitor_dir(cat, data_root) if cat else None

    steps = []
    parsed = {}
    if mdir:
        parsed = parse_step_status(mdir / "step-status.txt")
    dur_total = 0.0          # 有起止记录的步骤时长之和(≈已跑通总用时)
    dur_covered = 0          # 计入时长的步骤数
    dur_excluded = 0         # 无结束时间(中断残留/运行中)未计入的步骤数
    residual_nums: set[int] = set()   # 被判为"残留 RUNNING"(上次中断遗留)的步骤号, 见下方 _dur_hint
    for num in range(STEP_COUNT):
        entry = parsed.get(num, {})
        orig = entry.get("status", "PENDING")
        status = orig
        reason = entry.get("reason", "")
        acc = int(entry.get("acc", 0) or 0)      # 历史中断/失败段累计秒(重跑后并入)
        start = entry.get("start", "")
        end = entry.get("end", "")
        if status == "RUNNING":
            # 残留判定与类目中心共用一份实现(见 `_residual_running`, 2026-09-16 统一口径)
            stale = _residual_running(run_active, str(run_started or ""), str(start or ""))
            if stale:
                status = "INTERRUPTED"
                residual_nums.add(num)      # 只有这些步骤才用"日志最后活动时刻"截断(见 _dur_hint)
                hint = ("进程已结束(被中断/异常退出), 步骤未完成; 再次运行将自动从该步重跑"
                        if not run_active else
                        "残留 RUNNING(上次运行中断遗留, 本次运行尚未轮到该步); "
                        "再次运行将自动从该步重跑")
                reason = f"{reason} {hint}".strip() if reason else hint
        # 时长累计(用户口径 2026-09): 每步只要执行过就计时 —— DONE/SKIP/FAILED/INTERRUPTED
        # 只要有起止, 均计入 acc + 本轮 开始→结束(中断/失败后重跑从暂停的累计继续);
        # 进程被强杀遗留的 RUNNING(本服务确认无进程)合成 INTERRUPTED 后同样计入:
        #   acc(历史中断/失败段) + 本轮 开始→日志最后活动(近似中断时刻);
        # 仅真正 RUNNING(有进程在跑)不计静态部分(由前端实时计时递增)。
        dur_sec = None
        if start and end and orig in ("DONE", "SKIP", "FAILED", "INTERRUPTED"):
            try:
                a = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
                b = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
                if b >= a:
                    span = int((b - a).total_seconds())
                    dur_total += acc + span
                    dur_covered += 1
                    dur_sec = acc + span
            except ValueError:
                pass
        elif orig == "RUNNING" and start:
            # 合成中断(进程被强杀, 状态无结束): 从日志取该步骤最后活动时间作近似中断时刻
            if status == "INTERRUPTED" and not end and mdir:
                end = step_last_log_ts(mdir, num, start)
            if status == "INTERRUPTED":
                # 进程已不在运行: 该步骤按 中断/失败 同口径计入静态累计(含历史 acc),
                # 使"累计运行时长"不因强杀漏计已执行时间(修复: 之前只记 excluded 不累计)。
                span = 0
                try:
                    if end:
                        a = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
                        b = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
                        if b >= a:
                            span = int((b - a).total_seconds())
                except ValueError:
                    span = 0
                dur_total += acc + span
                dur_covered += 1
                dur_sec = acc + span
            else:
                dur_excluded += 1
        steps.append({
            "number": num,
            "status": status,
            "start": start,
            "end": end,
            "reason": reason,
            "acc": acc,
            "dur_sec": dur_sec,
            "fails": int(entry.get("fails", 0) or 0),
            "ints": int(entry.get("ints", 0) or 0),
        })

    # 累计执行时长的**汇总口径**(2026-09-15 与流水线日志「本轮总耗时」统一):
    # 汇总不再就地累加 —— 改调 `core.state.RunState.cumulative_seconds()`, 让
    # 看板与 `core/orchestrator.py` 完成行走**同一份实现**(否则两侧各写一遍,
    # 迟早漂移: 实例 2026-09-15 同一轮分 3 段跑完 18 步, 日志单进程段口径报
    # 4h33m、页面累计口径 12h51m, 用户看着像两个数打架)。
    # 上面的循环仍逐步骤算 `dur_sec`(步骤卡要用), 只有总数改为同源计算。
    #
    # ⚠️ 2026-09-16 修(用户: "运行中 类目中心累计运行时长 与 运行台累计执行时长 不统一,
    #    跑完就正常了"): `end_hint`(截断到日志最后活动时刻)**只给残留步骤** ——
    #    真正在跑的步骤必须按 `now` 计, 否则总量只在该步写日志时才跳一次(步骤16 约 135 秒
    #    才写一行 → 冻结两分多钟), 而两个页面在不同瞬间取数 → 数字不统一, 跑完(有 end)才一致。
    if mdir:
        try:
            from core.state import RunState as _RunState

            def _dur_hint(num: int) -> str:
                if num not in residual_nums:
                    return ""          # 真在跑 → 按 now 实时计(每秒涨)
                return step_last_log_ts(mdir, num, parsed.get(num, {}).get("start", ""))

            dur_total = _RunState(
                mdir / "step-status.txt", mdir / "step-duration.csv"
            ).cumulative_seconds(end_hint_for=_dur_hint)
        except Exception:
            pass

    alerts = []
    if mdir:
        raw = tail_text_lines(mdir / "monitor-alerts.txt", n=80)
        alerts = parse_log_lines(raw)

    file_lines = []
    if mdir:
        file_lines = parse_log_lines(tail_text_lines(mdir / "pipeline-progress.log", n=300))

    health = read_health(mdir) if mdir else {}

    counts = {"PENDING": 0, "RUNNING": 0, "DONE": 0, "SKIP": 0}
    cur_failed = 0
    cur_ints = 0
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
        if s["status"] == "FAILED":
            cur_failed += 1
        if s["status"] == "INTERRUPTED":
            cur_ints += 1
    # FAILED/INTERRUPTED 卡片 = **按监控日志累计的事件次数**(失败行/页面中断/Ctrl+C/强杀残留行,
    # 兼容升级前旧格式状态文件(无计数列)的历史; 重跑/重置不清零, 清空日志文件才归零);
    # 同事件双写(progress+alerts)去重; 强杀残留报告按"开始于"去重、页面中断区间覆盖不重复计。
    # 同时取 max(累计, 当前待处理数), 防止日志被截断(尾部扫描)时漏掉"当前仍失败/中断"的步骤。
    log_fails = log_ints = 0
    if mdir:
        log_fails, log_ints = log_event_counts(mdir, max_age=3.0)
    counts["FAILED"] = max(log_fails, cur_failed)
    counts["INTERRUPTED"] = max(log_ints, cur_ints)

    stream = ring.since(stream_after)
    # The cursor must never advance beyond the entries captured in this response.
    stream_watermark = stream[-1]["id"] if stream else max(stream_after or 0, 0)
    return {
        "ok": True,
        "ts": now_str(),
        "cat": cat,
        "monitor_dir": str(mdir) if mdir else "",
        "steps": steps,
        "counts": counts,
        "progress": round(100.0 * (counts["DONE"] + counts["SKIP"]) / STEP_COUNT, 1),
        "durations": {"total_sec": int(dur_total), "covered": dur_covered, "excluded": dur_excluded},
        "alerts": alerts[-60:],
        "health": health,
        # LLM 用量/费用(2026-09-28): 最近一轮的总账 + 逐步明细(运行台 ② 卡片与顶部合计用)
        "usage": cat_usage(mdir),
        "file_log": file_lines[-200:],
        "stream": stream,
        "stream_watermark": stream_watermark,
    }


# ---------------------------------------------------------------- HTTP 服务
_NO_BASIS = {"", "-", "—", "--", "无", "无依据", "未知", "未明确", "n/a", "na",
             "none", "null", "没有依据", "无来源"}
# ★ 2026-10-28 用户口径: 文字模式**不许推断**——出现这些"没写/猜"的字样即视为无依据。
_NEG_BASIS = ("未提及", "未提到", "没有提及", "没有提到", "全文未", "文中未",
              "未说明", "未标注", "未明确指出", "无相关", "无法确定",
              "推测", "猜测", "估计", "猜测为", "大概", "应该是", "应为",
              "判断为", "推为", "推断为", "可能是", "疑似")


def _norm_text(s) -> str:
    import re as _re
    return _re.sub(r"\s+", "", str(s or "")).lower()


def _quote_in_text(basis: str, texts) -> str:
    """依据是否是**原文里真的出现过**的话。返回命中的字段名，没命中返回 ""。

    允许模型用 `...` 省略中间内容：按省略号切成若干段，要求每段都在原文里。
    也会去掉模型爱加的前缀（`标题：` / `五点描述：` / `Title:` 等）与包裹的引号。
    """
    import re as _re
    _pfx = _re.compile(r"^\s*(标题|五点描述|详细参数|title|bullet|details?|description)\s*[:：]\s*",
                       _re.I)
    raw = str(basis or "")
    frags = [f.strip() for f in _re.split(r"\.{2,}|…+", raw) if len(f.strip()) >= 4]
    if not frags:
        return ""
    def _clean(f):
        f = _pfx.sub("", f)
        return f.strip().strip("「」『』\"'“”‘’《》 ")
    frags = [c for c in (_clean(f) for f in frags) if len(c) >= 4]
    if not frags:
        return ""
    # 只要**有一段原文**命中就算有依据（模型常把长句改写/翻译，要求整句逐字不现实）；
    # 优先用较长的片段，至少 6 个字才算"准确的词"。
    _strong = [f for f in frags if len(_norm_text(f)) >= 6] or frags
    for name, txt in texts.items():
        t = _norm_text(txt)
        if not t:
            continue
        if any(_norm_text(f) in t for f in _strong):
            return name
    return ""


_MULTI_SEPS = ("、", ",", "，", ";", "；", "+", "/", "|")


def _gate_basis(val, basis, src, downgrades: list, texts=None) -> dict:
    """★ 2026-10-28 用户口径: 重判必须给出**依据 + 来源**，否则一律打回「未明确」。

    `texts`：文字模式传该行的 {字段名: 原文}，要求依据里的原话**真的出现在原文**里
             （不能用「未提及/推测/应为」这类否定或猜测式依据）。
             视觉模式传 None —— 允许"看图推断"，但仍必须写出看了什么。
    """
    v = str(val or "").strip()
    b = str(basis or "").strip()
    s = str(src or "").strip()
    if not v or v == "未明确":
        return {"值": "未明确", "依据": b or "未明确（无依据）", "来源": s}
    if b.lower() in _NO_BASIS or len(b) < 2:
        downgrades.append("无依据")
        return {"值": "未明确", "依据": "未明确（模型未给出依据）", "来源": ""}
    if any(k in b for k in _NEG_BASIS):
        downgrades.append("否定式依据")
        return {"值": "未明确",
                "依据": "未明确（依据为“未提及/推测”等否定式表述，不算依据）", "来源": ""}
    if texts:
        hit = _quote_in_text(b, texts)
        if not hit:
            downgrades.append("依据不在原文")
            return {"值": "未明确",
                    "依据": "未明确（依据不是原文里出现过的词句）", "来源": ""}
        if not s:
            s = hit                      # 来源为空 → 用命中的字段补上
    return {"值": v, "依据": b, "来源": s}


def _reannot_rule(hint: str, uniq: list, req: str) -> str:
    """重判判据：字段说明 + 用户要求 + 唯一值（参考）+ 判断要求。

    ★ 2026-10-27 用户口径：不要「只能从下面这些值里选一个」这种硬约束；
      让 LLM 按「字段说明 + 用户要求」自己判断，唯一值只作参考。
      一律字符串拼接（hint 可能含 `%`，用 % 格式化会抛 TypeError）。
    """
    return ("请逐行判断该属性的值。\n"
            "【字段说明】"
            + (hint or "（该属性没有判定说明，按属性名的语义判断）") + "\n"
            + ("【用户的命名要求】" + req + "\n" if req else "")
            + ("【当前唯一值（供参考，**不是限定范围**）】"
               + "、".join(uniq) + "\n" if uniq else "")
            + "按上面说明判断后直接写出该属性的值；判断不出写「未明确」。\n"
            # ★ 2026-10-28 用户口径: 依据可以是原文/图上文字，也可以是**有依据的推断**，
            #   但绝不能空着——没有依据的值会被系统打回「未明确」。
            + "【依据怎么写（必填，不得为空或写「无」）】\n"
              "  · 原文摘录：从标题/五点/参数里抄一段能支撑判断的原文；\n"
              "  · 图上文字：图上看到的字（如规格图上的 450W、标签上的文字）；\n"
              "  · **推断**：按外观/结构/类目常识推出值，但必须写清**凭什么推**，"
              "例如「外形成圆柱且带提手，推为家用小型」。\n"
              "  「来源」填 标题 / 五点描述 / 详细参数 / 图片视觉 之一。\n")


_STD_SUMMARY_GUARD = threading.Lock()
_STD_SUMMARY_LOCKS = {}
_STD_SUMMARY_CACHE = {}


def _std_summary_fingerprint(workspace):
    """Track all files that can change the effective summary or its history."""
    from tools.attr_stability.step18_workspace import OUTPUT
    paths = [workspace.pointer, workspace.spec_path, workspace.rules_path,
             workspace.root / "17_属性标注/input.json",
             workspace.root / "17_属性标注/稳定性结果.json",
             workspace.root / "17_属性标注/视觉补充.json",
             workspace.store / "export.json",
             workspace.root / "18_标准化" / OUTPUT.format(cat=workspace.cat)]
    paths.extend(sorted((workspace.store / "versions").glob("V*.json")))
    result = []
    for path in paths:
        if path is None:
            result.append(None)
            continue
        try:
            info = path.stat()
            result.append((str(path), info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            result.append((str(path), None, None))
    return tuple(result)


def _std_workspace_summary(workspace):
    key = str(workspace.store)
    with _STD_SUMMARY_GUARD:
        lock = _STD_SUMMARY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        before = _std_summary_fingerprint(workspace)
        with _STD_SUMMARY_GUARD:
            cached = _STD_SUMMARY_CACHE.get(key)
        if cached and cached[0] == before:
            return cached[1]
        result = workspace.summary()
        after = _std_summary_fingerprint(workspace)
        if before == after and workspace.pointer.exists():
            with _STD_SUMMARY_GUARD:
                if len(_STD_SUMMARY_CACHE) >= 16:
                    _STD_SUMMARY_CACHE.clear()
                _STD_SUMMARY_CACHE[key] = (after, result)
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoWorkflowWebUI/1.0"

    # ---- 基础
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _security_origin(self):
        """Only the configured HTTPS origin, or a local loopback origin, is accepted."""
        configured = os.environ.get("WEBUI_PUBLIC_ORIGIN", "").strip().rstrip("/")
        host = self.headers.get("Host", "")
        if configured:
            parsed = urlparse(configured)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                return None
            return configured if host.lower() == parsed.netloc.lower() else None
        parsed = urlparse("http://" + host)
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        return "http://" + host

    def _session_token(self):
        from http.cookies import SimpleCookie, CookieError
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get("Cookie", ""))
        except CookieError: return ""
        return cookie["wui_session"].value if "wui_session" in cookie else ""

    def _auth_response(self, obj, code=200, cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        if cookie is not None:
            flags = "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if os.environ.get("WEBUI_PUBLIC_ORIGIN", "") else "")
            self.send_header("Set-Cookie", "wui_session=" + cookie + flags + ("; Max-Age=0" if not cookie else ""))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorize(self, path, *, post=False):
        from webui import auth
        origin = self._security_origin()
        if not origin:
            self._json({"ok": False, "error": "访问地址未配置或不受信任"}, 403); return None
        if post and (self.headers.get("Origin") != origin or not self.headers.get("Content-Type", "").lower().startswith("application/json")):
            self._json({"ok": False, "error": "请求来源无效"}, 403); return None
        # This legacy read endpoint can also create a confirmation gate. A
        # cross-origin navigation must not trigger that side effect.
        if not post and path == "/api/keyword-lists" and self.headers.get("Sec-Fetch-Site", "") in ("cross-site", "same-site"):
            self._json({"ok": False, "error": "请求来源无效"}, 403); return None
        if path in ("/login", "/api/auth/login"):
            return {"public": True}
        user = auth.session(self._session_token())
        if not user:
            if not post and path in ("/", "/index.html", "/account", "/accounts", "/readonly"):
                self._redirect("/login")
            else: self._json({"ok": False, "error": "请先登录"}, 401)
            return None
        if post:
            csrf = str(self.headers.get("X-CSRF-Token", ""))
            if not csrf.isascii() or len(csrf) > 128 or not hmac.compare_digest(csrf, user["csrf"]):
                self._json({"ok": False, "error": "会话验证失败，请刷新页面"}, 403); return None
        if (path == "/accounts" or path == "/api/auth/audit" or path in ("/oplog", "/api/oplog")
                or path.startswith("/api/auth/users")) and user["role"] != "admin":
            # 2026-11-12: 操作日志只给管理员看（含日志页面本身）
            self._json({"ok": False, "error": "仅管理员可查看操作日志/管理账号"}, 403); return None
        if user["role"] == "viewer":
            if not post and path in ("/", "/index.html"):
                self._redirect("/readonly"); return None
            if post and path not in ("/api/auth/logout", "/api/auth/password"):
                self._json({"ok": False, "error": "只读账号不能执行此操作"}, 403); return None
            if not post and path not in ("/readonly", "/oplog", "/account", "/api/history", "/api/status", "/api/auth/me", "/api/oplog"):
                self._json({"ok": False, "error": "只读账号无权访问此内容"}, 403); return None
        # ★ 2026-11-12: 记住本次请求的登录用户，供 _require_cat() 做类目白名单校验
        self._user = user
        return user

    def _allowed_cats(self):
        """当前用户**可见**的类目集合；None = 全部（admin / 白名单含 "*"）。

        2026-11-12: 可见 = admin | 自己创建的(owner) | 被共享(任一 level) | users.categories 白名单
        """
        user = getattr(self, "_user", None) or {}
        if user.get("role") == "admin":
            return None
        cats = set(user.get("categories") or [])
        if "*" in cats:
            return None
        name = str(user.get("name") or "")
        if name:
            try:
                from webui import auth
                cats |= set(auth.shared_cats(name))          # 被共享到的（view / run 都算可见）
                cats |= set(auth.owned_cats(name))           # 自己创建的
            except Exception:
                pass
        return cats

    def _can_run_cat(self, cat: str) -> bool:
        """**可跑**：admin | 该类目 owner | 共享级别=run | 白名单里的（兼容旧口径）。"""
        user = getattr(self, "_user", None) or {}
        if user.get("role") == "admin":
            return True
        name = str(user.get("name") or "")
        try:
            from webui import auth
            row = auth.owner_of(cat) or {}
            if row and str(row.get("owner") or "").lower() == name.lower():
                return True
            if str(auth.shared_cats(name).get(cat) or "") == "run":
                return True
        except Exception:
            pass
        cats = user.get("categories") or []
        return "*" in cats or cat in cats

    def _require_run(self, cat: str) -> None:
        """写操作门禁：只能跑自己创建的 / 被授「可跑」的 / 白名单内的类目。"""
        if self._can_run_cat(cat):
            return
        if self._cat_allowed(cat):
            raise Forbidden("你对类目「%s」只有查看权限，不能运行或修改；"
                            "请让管理员或创建人把共享级别改为「可跑」" % (cat or "-"))
        raise Forbidden("无权访问该类目「%s」：请让管理员在账号页为你勾选，或请创建人共享给你" % (cat or "-"))

    def _cat_allowed(self, cat: str) -> bool:
        allowed = self._allowed_cats()
        return allowed is None or (bool(cat) and cat in allowed)

    def _require_cat(self, cat: str) -> None:
        """非 admin 访问未授权类目 → 403。"""
        if self._cat_allowed(cat):
            return
        raise Forbidden("无权访问该类目「%s」：请让管理员在账号页为你勾选该类目" % (cat or "-"))

    def _serve_auth_page(self, filename):
        self._send(200, (WEBUI_DIR / filename).read_bytes(), "text/html; charset=utf-8",
                   headers={"X-Frame-Options": "DENY"})

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", headers=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_GONE as exc:
            # 客户端已断开: 记一行即可, 不再往外抛(否则 socketserver 会打整段 traceback,
            # 且调用方的兜底分支还会再写一次、再抛一次 —— 一次刷新打出两大段)
            log_client_abort(f"{self.command} {urlparse(self.path).path}", exc)

    def _send_file(self, path: Path, name: str) -> None:
        """流式下发产物文件(附件下载, 文件名 UTF-8 编码)。"""
        try:
            size = path.stat().st_size
            if size > MAX_DOWNLOAD_BYTES:
                self._json({"ok": False, "error": f"文件过大(>{MAX_DOWNLOAD_BYTES // 1024 // 1024}MB), 拒绝下载"}, 413)
                return
        except OSError:
            self._json({"ok": False, "error": "文件不可读"}, 404)
            return
        ctype = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(name)}")
            self.end_headers()
        except CLIENT_GONE as exc:
            log_client_abort(f"{self.command} {urlparse(self.path).path}", exc)
            return
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except CLIENT_GONE as exc:
            # 下载中途取消(2026-09-13: 原来只捕 BrokenPipe/ConnectionReset, 漏了
            # ConnectionAbortedError=WinError 10053, 取消大文件下载会打 traceback)
            log_client_abort(f"{self.command} {urlparse(self.path).path}(下载中断)", exc)

    def _json(self, obj, code: int = 200):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception as exc:
            data = json.dumps({"ok": False, "error": f"序列化失败: {exc}"}).encode("utf-8")
            code = 500
        self._send(code, data)

    def _read_body(self) -> dict:
        # ★ 2026-11-12: 写操作日志时要在分发前读一次 body —— 这里做缓存,
        #   否则第二次 read 会把 socket 读空（每个请求一个 Handler 实例，缓存安全）。
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length > 16 * 1024 * 1024:
            raise ValueError("请求体过大")
        if length <= 0:
            self._body_cache = {}
            return self._body_cache
        try:
            raw = self.rfile.read(length)
            self._body_cache = json.loads(raw.decode("utf-8"))
            return self._body_cache
        except Exception as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}")

    # ---- 操作日志（2026-11-12）: 谁 / 对哪个类目 / 做了什么 ----
    def _op_cat(self, body: dict) -> str:
        """从请求体里的 config_file/file/cat 解析出类目名（解析不到就空）。"""
        for key in ("config_file", "file"):
            v = str((body or {}).get(key) or "").strip()
            if not v:
                continue
            try:
                return str(load_yaml_file(safe_config_path(v)).get("CAT", "") or "").strip()
            except Exception:
                return ""
        return str((body or {}).get("cat") or "").strip()

    @staticmethod
    def _op_detail(path: str, body: dict) -> str:
        b = body or {}
        parts: list[str] = []
        if path == "/api/run":
            parts.append("模式=%s" % (b.get("mode") or "resume"))
            if b.get("step") is not None:
                parts.append("步骤=%s" % b.get("step"))
            if b.get("from_step"):
                parts.append("起始步骤=%s" % b.get("from_step"))
        elif path == "/api/stop":
            parts.append("范围=%s" % (b.get("cat") or b.get("config_file") or "全部在跑"))
        elif path == "/api/reset-step":
            parts.append("步骤=%s" % (b.get("step") if b.get("step") is not None else ""))
        elif path.startswith("/api/std-workspace/"):
            for k in ("job_id", "proposal_id", "version", "attribute"):
                if b.get(k):
                    parts.append("%s=%s" % (k, str(b.get(k))[:24]))
        elif path in ("/api/config/save", "/api/keyword-lists/save", "/api/attr-spec/save"):
            vals = b.get("values") or {}
            if isinstance(vals, dict) and vals:
                parts.append("字段=%s" % "、".join(sorted(vals)[:8]))
        return " · ".join(p for p in parts if p and not p.endswith("="))

    def _log_operation(self, user: dict, path: str) -> None:
        """把一次 POST 记成一条操作日志（读失败也不影响业务）。"""
        try:
            body = self._read_body()
        except Exception:
            self._body_cache = {}
            body = {}
        try:
            from webui import auth
            action = OP_ACTIONS.get(path)
            if action is None and path.startswith("/api/std-workspace/"):
                action = STD_ACTIONS.get(path.rsplit("/", 1)[-1], "步骤18 操作")
            auth.log_action(str((user or {}).get("name") or ""), action or "操作",
                            target=path, cat=self._op_cat(body),
                            detail=self._op_detail(path, body))
        except Exception:
            pass

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    # ---- 路由
    def _api_files(self, q) -> dict:
        cfg = self._status_cfg(q.get("file", [None])[0])
        return {"ok": True, "cat": cfg["CAT"], "files": list_artifacts(cfg["CAT"], cfg["DATA_ROOT"])}

    def _api_step_files(self, q) -> dict:
        """某步骤的产物清单(点击步骤卡片弹窗用)。

        2026-09-16: **默认读归档快照**(用户定调"读取每个类目 archive 中每个步骤里的数据");
        `&source=live` 可回退到旧的"声明产物 + 实时目录 + 未归属"口径(排查用);
        `&snapshot=<快照目录名>` 可指定看哪一份快照(缺省最新)。
        """
        cfg = self._status_cfg(q.get("file", [None])[0])
        raw = (q.get("step", [None])[0] or "").strip()
        try:
            step = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"step 必须是 0-{STEP_COUNT - 1} 的整数")
        if not 0 <= step < STEP_COUNT:
            raise ValueError(f"step 必须在 0-{STEP_COUNT - 1}")
        source = str((q.get("source", [None])[0] or "archive")).strip().lower()
        snapshot = str((q.get("snapshot", [None])[0] or "")).strip()
        data = step_files(cfg["CAT"], cfg["DATA_ROOT"], step,
                          source=source, snapshot=snapshot)
        data["ok"] = True
        return data

    # ---- 实现: 整合市场报告(HTML, 类目中心入口)
    def _api_integrated_progress(self, q) -> dict:
        """GET /api/integrated-report/progress?cat=… → 生成进度(按钮上直接显示"正在做什么")。"""
        cat = safe_cat_name(q.get("cat", [""])[0])
        droot = str(q.get("data_root", [""])[0] or "").strip() or _cat_data_root(cat)
        return {"ok": True, **integrated_progress(cat, droot)}

    def _api_integrated_download(self, q) -> None:
        """GET /api/integrated-report/download?cat=…&kind=… → 下载整合报告 HTML 或**三份源数据**。

        kind: integrated(整合报告 HTML) / keyword(步骤8 关键词报告) / product(步骤18 标准化交付) /
              voc(外部提供的 VOC 报告) —— 白名单之外一律拒; 文件不存在则 404(前端据此置灰)。
        """
        cat = safe_cat_name(q.get("cat", [""])[0])
        kind = str(q.get("kind", [""])[0] or "").strip().lower()
        if kind not in INTEGRATED_DOWNLOADS:
            self._json({"ok": False, "error": f"未知的下载类型: {kind!r}"}, 400)
            return
        droot = str(q.get("data_root", [""])[0] or "").strip() or _cat_data_root(cat)
        p = integrated_download_path(cat, droot, kind)
        if p is None or not p.is_file():
            self._json({"ok": False, "error": f"文件不存在(该步骤可能还没跑): {p}"}, 404)
            return
        self._send_file(p, INTEGRATED_DOWNLOADS[kind][1].format(cat=cat))

    def _api_integrated(self, q) -> dict:
        """GET /api/integrated-report?cat=… → 三份输入是否齐备 + 成品状态(按钮据此显示)。"""
        cat = safe_cat_name(q.get("cat", [""])[0])
        droot = str(q.get("data_root", [""])[0] or "").strip() or _cat_data_root(cat)
        return {"ok": True, **integrated_report_status(cat, droot)}

    def _api_integrated_build(self, body) -> dict:
        """POST /api/integrated-report/build {cat} → 跑生成脚本。

        **门槛不过直接拒**(用户口径: 三份缺一不可), 不做部分报告; 输出逐行写进该类目实时日志。
        """
        body = body or {}
        cat = safe_cat_name(body.get("cat") or "")
        # ★ 2026-11-12: 生成整合报告属于写操作 —— 需要「可跑」
        self._require_run(cat)
        droot = str(body.get("data_root") or "").strip() or _cat_data_root(cat)
        prog = integrated_progress(cat, droot)
        if prog.get("running"):
            raise UserFacingError(
                "该类目的整合报告正在生成中(%s %s%%) —— 请等它跑完再点。"
                % (prog.get("phase") or "生成中", prog.get("pct", 0)))
        st = integrated_report_status(cat, droot)
        if not st["inputs_ok"]:
            names = "、".join("步骤%d %s" % (m["step"], m["name"]) for m in st["missing"])
            raise UserFacingError(
                "整合报告要求三份输入齐备(缺一不可), 当前缺: %s —— 请先跑对应步骤(可单步), 再回来点生成。"
                % names)
        cmd = [sys.executable, "-X", "utf8", str(PROJECT_ROOT / INTEGRATED_REPORT_SCRIPT),
               "--cat", cat, "--data-root", droot]
        push_cat(cat, "▶ 生成整合报告(HTML): " + " ".join(cmd[2:]))
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        try:
            r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env,
                               timeout=INTEGRATED_BUILD_TIMEOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            push_cat(cat, "✗ 生成超时(>%ds), 已放弃" % INTEGRATED_BUILD_TIMEOUT, level="ERROR")
            raise UserFacingError("生成整合报告超时(>%d 秒), 请重试" % INTEGRATED_BUILD_TIMEOUT)
        out = (r.stdout or "").rstrip()
        for ln in out.splitlines()[-40:]:
            push_cat(cat, "  " + ln)
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            for ln in err.splitlines()[-6:]:
                push_cat(cat, "  " + ln, level="ERROR")
            push_cat(cat, "✗ 整合报告生成失败(退出码 %d)" % r.returncode, level="ERROR")
            if not err:
                # 脚本把失败原因打在 **stdout 最后几行**(如 "✗ 生成失败: ValueError: …"), stderr 常为空。
                # 不留兜底时前端 `api()` 拿到 error="" 会退回显示 "HTTP 200", 用户完全看不出原因
                # (2026-10-10 真事故: VOC 路径没接线 → 只看到一句 HTTP 200)。
                err = "\n".join(out.splitlines()[-6:]).strip() or ("脚本退出码 %d(无输出)" % r.returncode)
            return {"ok": False, "code": r.returncode, "output": out[-4000:], "error": err[-1500:]}
        st = integrated_report_status(cat, droot)
        html = (st.get("html") or {}).get("path") or "(未找到产物)"
        push_cat(cat, "✓ 整合报告已生成: %s" % html)
        return {"ok": True, "code": 0, "output": out[-4000:], "status": st}

    def _serve_integrated_report(self, q) -> None:
        """GET /api/integrated-report/view?cat=… → 直接吐 HTML(浏览器新标签页打开)。"""
        cat = safe_cat_name(q.get("cat", [""])[0])
        droot = str(q.get("data_root", [""])[0] or "").strip() or _cat_data_root(cat)
        p = cat_root_dir(cat, droot) / INTEGRATED_REPORT_SUBDIR / "index.html"
        if not p.is_file():
            self._send(404, "整合报告尚未生成 —— 请在类目中心点「📊 生成整合报告」".encode("utf-8"),
                       "text/plain; charset=utf-8")
            return
        self._send(200, p.read_bytes(), "text/html; charset=utf-8",
                   headers={"Content-Security-Policy": "sandbox allow-scripts allow-downloads allow-forms allow-popups"})

    def _api_download(self, q) -> dict | None:
        """产物下载(校验: 必须落在该类目数据目录内 / 归档快照内); 非法/缺失返回错误 dict, 正常返回 None(已写文件流)。"""
        cfg = self._status_cfg(q.get("file", [None])[0])
        cat = cfg["CAT"]
        if not cat:
            return {"ok": False, "error": "配置缺少 CAT(类目名)"}
        step = q.get("step", [None])[0]
        name = q.get("name", [None])[0]
        if step is None or name is None:
            return {"ok": False, "error": "缺少 step/name 参数"}
        source = str((q.get("source", [None])[0] or "live")).strip().lower()
        snapshot = str((q.get("snapshot", [None])[0] or "")).strip()
        path = _artifact_abs(cat, cfg["DATA_ROOT"], step, name,
                             source=source, snapshot=snapshot)
        if path is None:
            return {"ok": False, "error": "文件不存在, 或不在该类目数据目录内"}
        self._send_file(path, os.path.basename(str(name)))
        return None

    def _step15_review_root(self, file):
        path = safe_config_path(file)
        raw = load_yaml_file(path)
        if not str(raw.get("CAT") or "").strip():
            raise ValueError("类目配置无效，请重新选择类目")
        cfg = self._status_cfg(file)
        return cfg, cat_root_dir(cfg["CAT"], cfg["DATA_ROOT"])

    def _api_step15_review(self, q):
        from core.step15_review import view
        cfg, root = self._step15_review_root(q.get("file", ["config.yaml"])[0])
        return {"ok": True, "cat": cfg["CAT"], **view(root)}

    def _api_step15_pdf(self, q):
        from core.step15_review import view
        from core.step15_pdf import render_pdf
        cfg, root = self._step15_review_root(q.get("file", ["config.yaml"])[0])
        snapshot = view(root)
        expected = q.get("revision", [""])[0]
        if expected and expected != snapshot["revision"]:
            self._json({"ok": False, "error": "报告已更新，请刷新后重新下载"}, 409)
            return
        result = render_pdf(cfg["CAT"], snapshot)
        self._send(200, result, "application/pdf", {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(cfg['CAT'] + '-步骤15选品报告.pdf')}"})

    def _api_step15_settings(self, q):
        from core.step15_settings import describe
        cfg, root = self._step15_review_root(q.get("file", ["config.yaml"])[0])
        return {"ok": True, "cat": cfg["CAT"], **describe(root, q.get("scope", ["global"])[0])}

    def _api_step15_settings_save(self, body):
        from core.step15_settings import save
        cfg, root = self._step15_review_root(str(body.get("config_file") or "config.yaml"))
        self._require_run(cfg["CAT"])
        scope = str(body.get("scope") or "global")
        if (getattr(self, "_user", None) or {}).get("role") != "admin":
            raise Forbidden("只有管理员可以修改通用选品标准")
        if RUNNER.active_for(cfg["CAT"]) or _lock_live_started(cfg["CAT"], cfg["DATA_ROOT"])[0]:
            raise ValueError("该类目仍在运行，请先停止再修改标准")
        rev = save(root, body.get("rules"), body.get("revision"), scope)
        return {"ok": True, "revision": rev, "scope": scope}

    def _api_step15_review_confirm(self, body):
        from core.step15_review import confirm
        cfg, root = self._step15_review_root(str(body.get("config_file") or "config.yaml"))
        self._require_run(cfg["CAT"])
        if RUNNER.active_for(cfg["CAT"]) or _lock_live_started(cfg["CAT"], cfg["DATA_ROOT"])[0]:
            raise ValueError("该类目仍在运行，请等待步骤15暂停后再确认")
        confirm(root, body.get("revision"))
        return {"ok": True, "cat": cfg["CAT"]}

    def _api_status(self, q, path):
        after_raw = q.get("after", [None])[0]
        after = int(after_raw) if after_raw and after_raw.isdigit() else None
        cfg = self._status_cfg(q.get("file", [None])[0])
        cfg_cat = str(cfg.get("CAT", "") or "").strip()
        cfg_root = str(cfg.get("DATA_ROOT", "data") or "data")
        # 多类目并行(2026-09-15): 运行信息与实时日志都**按类目取** —— 看哪个类目就是
        # 哪个类目的运行横幅/控制台; 另外把"别的类目也在跑"带出去供前端提示。
        run = RUNNER.snapshot(cfg_cat)
        ring = RUNNER.ring_for(cfg_cat) or RING
        _others = [r for r in RUNNER.snapshots()
                   if r.get("active") and str(r.get("cat") or "") != cfg_cat]
        lock_alive, lock_started, lock_unknown = _lock_live_started(cfg_cat, cfg_root)
        # 本类目是否有进程在跑(2026-09-14): RUNNER 的子进程只在"它跑的正是本类目"时算数,
        # 混合/切换类目场景下不再拿别的类目的运行态来判本类目(否则看 B 类目会把 B 的
        # 历史残留 RUNNING 当成"本轮在跑")。CLI 运行(无 RUNNER 记录)由锁/pid 兜住。
        run_is_here = bool(run.get("active")) and str(run.get("cat", "") or "") == cfg_cat
        active = run_is_here or lock_alive
        # 锁状态"无法判定"(仅"文件锁不可用 + pid 查不出"的极端环境) → 用近期监控活动兜底,
        # 避免把真正在跑的运行显示成"中断/残留"(2026-09-14)
        if not active and lock_unknown:
            active = _recent_monitor_activity(cfg_cat, cfg_root)
        # 运行启动时间: 本类目的 RUNNER 子进程优先; CLI 跑(仅锁可见)用锁内时间做残留判定;
        # 看的是别的类目时两者都不适用(本类目没有进程在跑) → 不传, 残留 RUNNING 一律按中断显示
        run_started = (run.get("started_at") if run_is_here else lock_started)
        snap = status_snapshot(cfg, after, run_active=active, run_started=run_started, ring=ring)
        run.pop("effective_conf", None)
        snap["run"] = run
        # 别的类目也在跑(多类目并行, 2026-09-15): 前端在运行横幅里提示"并行中: B, C";
        # 本类目自己的运行横幅/停止按钮只按 `run`(本类目)显示 —— 停止只停本类目。
        snap["other_runs"] = [{"cat": r.get("cat"), "pid": r.get("pid"),
                              "label": r.get("label"), "started_at": r.get("started_at"),
                              "user": str(r.get("user") or "")}
                             for r in _others]
        # 本页所显示类目**自己**是否有进程在跑(②步骤看板/③日志模式用; 2026-09-15 起
        # 运行横幅与"停止"按钮也按本类目显示 —— 多类目并行时停的是当前类目这个进程)
        snap["cat_running"] = bool(active)
        # 外/孤儿运行(2026-09-16 用户确认): 本类目有存活进程但**不是本服务启动的**
        # (看板重启后上一实例留下的子进程, RUNNER 注册表已清空) —— 前端据此把运行横幅
        # 与「中断」按钮也点亮(否则按钮恒禁用, 用户以为没在跑/点不动), 并显示真实 pid/开始时刻。
        snap["cat_external"] = bool(active and not run_is_here)
        if snap["cat_external"]:
            try:
                _lp = _lock_pid(cfg_cat, cfg_root)
            except Exception:  # noqa: BLE001
                _lp = None
            snap["cat_pid"] = _lp
            snap["cat_live_started"] = str(lock_started or "")
        snap["lock_unknown"] = bool(lock_unknown)
        if lock_unknown:
            snap["lock_note"] = ("单实例锁状态未知(本环境既拿不到文件锁也查不出进程存活), "
                                 "已按监控日志活动推断运行状态")
        # **游标归属**(2026-09-15, 多类目并行): 每个在跑的类目有**自己的**日志缓冲(id 序列各自从 1 开始),
        # 没有运行记录时回落到全局缓冲 —— 前端必须知道"这个 watermark 属于哪个缓冲", 否则换类目后
        # 继续用上一个类目的游标(数值很大)去请求新缓冲(`since(大数)`)会**永远返回空**,
        # 表现就是"点进 FR 的运行台, 日志面板还停在 UK 的内容上"(用户实测报障)。
        snap["stream_key"] = cfg_cat if RUNNER.ring_for(cfg_cat) else "__server__"
        try:
            from core.manual_gates import pending_gates
            statuses = {s["number"]: s["status"] for s in snap["steps"]}
            pending = pending_gates(cat_root_dir(cfg_cat, cfg_root), statuses) if cfg_cat else []
            snap["manual_gate"] = pending[0] if pending else None
        except Exception as exc:
            snap["manual_gate"] = {"kind":"error", "message":f"人工确认状态无法核验：{exc}"}
        return snap

    def _api_history(self) -> dict:
        """类目中心列表(2026-09-08 改): 数据源 = data/<CAT>/config.yaml(配置随类目,
        真实当前类目全集), **不再从 .webui/runs 快照推断**——快照是"某次运行的历史
        生效值", 曾因链式/陈旧快照导致状态错乱混淆。进度仍由 step-status.txt 汇总。
        返回 [{run_file, run_time, base, cat, dry_run, done, total, running,
                failed, interrupted, current(当前卡点), can_resume,
                dur_sec(累计执行时长, 与运行台同口径, 2026-09-15 新增)}]。
        """
        by_cat: dict[str, dict] = {}
        # 性能(2026-10-09): 原实现用 list_category_configs() 重新解析全部类目 config.yaml
        # (冷态 ≈1.2s, 占本接口八成耗时) → 改用**已缓存的**配置扫描结果(_scan_configs),
        # 并按 step-status.txt 的 mtime 缓存"最近时间 + 状态解析"。
        try:
            for it in self._scan_configs():
                name = str(it.get("name") or "")
                if not name.startswith("data/"):
                    continue
                cfg = PROJECT_ROOT / name
                cat = str(it.get("cat") or "").strip()
                if not cat:
                    continue
                # ★ 2026-11-12: 类目中心也按白名单过滤 —— 只有 admin 能看到所有类目
                if not self._cat_allowed(cat):
                    continue
                vals = {"DRY_RUN": it.get("dry_run"), "DATA_ROOT": it.get("data_root") or "data"}
                by_cat[cat] = {
                    "run_file": name,
                    "run_time": "",
                    "base": "config.yaml",
                    "cat": cat,
                    "dry_run": bool(vals.get("DRY_RUN", False)),
                    # 该类目的数据根(与 _status_cfg 同规则): 配置里怎么写就怎么用,
                    # 相对路径按项目根解析 —— 否则 dur_sec 会去默认 "data" 下找状态文件,
                    # 自定义 DATA_ROOT 的类目会恒为 0(2026-09-15 由单测发现)
                    "data_root": str(vals.get("DATA_ROOT", "") or "") or "data",
                }
        except Exception:
            pass
        items = []
        # 多类目并行(2026-09-15): 不再是"单条运行", 而是**在跑的类目集合** ——
        # 每个类目各自判断自己有没有进程在跑(否则并行时只有一条会被认成运行中)。
        _active_cats = set(RUNNER.active_cats())
        for cat, info in by_cat.items():
            _droot = str(info.pop("data_root", "data") or "data")   # 该类目数据根(原行为: 不返回给前端)
            # 状态文件位置 = **monitor_dir(cat, DATA_ROOT)**(与运行台同源; 2026-10-09 修:
            # 不要用配置所在目录 —— 自定义 DATA_ROOT 的类目/单测夹具里两者并不相同)。
            _mdir = monitor_dir(cat, _droot)
            _last, parsed = _hist_status_snapshot(_mdir)
            info["run_time"] = _last
            # 本类目是否**有存活进程** + 其启动时刻。**与运行台 _api_status 完全同源**:
            # ①本服务启动的子进程(RUNNER 记录, 记着 started_at) ②`.run.lock` 里的存活进程
            # (CLI 运行, 以及"看板重启后留下的孤儿运行" —— 2026-09-16 修: 原来只认 RUNNER
            #  内存注册表, 重启看板后注册表清空 → 仍在跑的孤儿被强行改判成 INTERRUPTED,
            #  与运行台显示打架; 用户报障 "UK 的不是已经断了吗, 卡片上还显示 running")。
            try:
                _lock_alive, _lock_started, _lock_unknown = _lock_live_started(cat, _droot)
            except Exception:  # noqa: BLE001
                _lock_alive, _lock_started = False, None
            try:
                _rec = RUNNER.snapshot(cat) or {}
            except Exception:  # noqa: BLE001
                _rec = {}
            _run_here = bool(_rec.get("active")) and str(_rec.get("cat") or "") == cat
            _alive = _run_here or cat in _active_cats or bool(_lock_alive)
            # 存活进程的启动时刻: RUNNER 记录优先, 否则锁内时刻(都没有 → 空 = 判不出, 保持旧行为)
            _live_started = str(_rec.get("started_at") or "") if _run_here else ""
            if not _live_started:
                _live_started = str(_lock_started or "") if _lock_alive else ""
            st = [parsed.get(n, {}).get("status", "PENDING") for n in range(STEP_COUNT)]
            # 纠偏(2026-09): 只有"当前确有进程且在跑的就是本类目"才算 RUNNING;
            # 否则历史遗留的 RUNNING(上次被中断/强杀未写结束)按 INTERRUPTED 展示,
            # 与 ②看板(status_snapshot)口径一致 —— 否则类目中心会一直显示"运行中"。
            # 2026-09-16: 判定改为**与运行台共用** `_residual_running`(存活进程含 `.run.lock`
            # 里的 CLI/孤儿进程), 并逐步骤比较"开始时刻 vs 存活进程启动时刻", 两端结果一致。
            for _n in range(STEP_COUNT):
                if st[_n] != "RUNNING":
                    continue
                if _residual_running(_alive,
                                     _live_started,
                                     str(parsed.get(_n, {}).get("start") or "")):
                    st[_n] = "INTERRUPTED"
            done = sum(1 for s in st if s in ("DONE", "SKIP"))
            running = [n for n, s in enumerate(st) if s == "RUNNING"]
            failed = [n for n, s in enumerate(st) if s in ("FAILED", "INTERRUPTED")]
            current = ""
            if running:
                current = f"步骤{running[0]:02d} 运行中"
            elif failed:
                current = f"卡在步骤{failed[0]:02d}({st[failed[0]]})"
            elif done >= STEP_COUNT:
                current = "已完成"
            else:
                nxt = next((n for n, s in enumerate(st) if s == "PENDING"), STEP_COUNT)
                current = f"待续步骤{nxt:02d}"
            # ★ 2026-11-12: 归属与共享（类目中心展示「创建人 / 创建时间 / 共享给谁」）
            try:
                from webui import auth as _auth
                _row = _auth.owner_of(cat) or {}
                if not _row:
                    # 2026-11-13: 老类目没有归属记录 —— 用目录创建时间回填，避免「创建时间」永远为空
                    try:
                        _ct = float((_droot / cat).stat().st_ctime)
                    except (OSError, AttributeError, TypeError, ValueError):
                        _ct = 0.0
                    _auth.set_owner(cat, "admin", _ct)
                    _row = _auth.owner_of(cat) or {}
                _owner = str(_row.get("owner") or "") or "admin"
                _created = (datetime.fromtimestamp(float(_row["created_at"])).strftime("%Y-%m-%d %H:%M:%S")
                            if _row.get("created_at") else "")
                _shares = [{"username": s["username"], "level": s["level"]} for s in _auth.shares(cat)]
            except Exception:
                _owner, _created, _shares = "", "", []
            info.update({
                "done": done, "total": STEP_COUNT,
                "running": [f"{n:02d}" for n in running],
                "failed": [f"{n:02d}" for n in failed],
                "current": current,
                "can_resume": done < STEP_COUNT or bool(running),
                # 累计执行时长(2026-09-15): 与 ②流程状态 的 ⏱ / 日志完成行同口径,
                # 供类目中心列表新增的「累计运行时长」列显示(同一数字, 不再是两套)。
                # 2026-09-16: 传"本类目存活进程的启动时刻" —— 让"真在跑"的步骤按 now 计、
                # 只有残留步骤才截断到日志活动时刻(与运行台 ② 的判定完全一致, 见 _cat_cumulative_sec)。
                "dur_sec": _cat_cumulative_sec(
                    monitor_dir(cat, _droot), cat,
                    live_started=_live_started,
                ),
                # 本轮 LLM 费用合计(2026-09-28): 类目中心「累计运行时长 · 本轮费用」第二行用。
                # 只要合计(per-step 明细留给运行台 ②), 且 cat_usage 内部按账本指纹缓存 ——
                # 本接口每 12s 静默刷一次 × 12 个类目, 不缓存会把看板拖卡(见 cat_usage 注释)。
                "usage": usage_lite(cat_usage(monitor_dir(cat, _droot))),
                # 整合报告(HTML)状态(2026-10-09): 类目中心那行的「📊 整合报告」按钮据此显示
                # ("缺哪一份" / "可生成" / "已生成 + 时间")。
                "integrated": integrated_report_status(cat, _droot),
                "owner": _owner, "created_at": _created, "shares": _shares,
                # ★ 2026-11-12: 正在跑时是谁启动的（admin 能看到所有类目，用它区分操作人）
                "operator": str(_rec.get("user") or ""),
            })
            items.append(info)
        items.sort(key=lambda x: x.get("run_time", ""), reverse=True)
        return {"ok": True, "items": items}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = parse_qs(parsed.query)
        try:
            user = self._authorize(path)
            if user is None: return
            if path == "/login":
                self._serve_auth_page("login.html")
            elif path in ("/accounts", "/account"):
                self._serve_auth_page("accounts.html")
            elif path == "/readonly":
                self._serve_auth_page("readonly.html")
            elif path == "/oplog":
                self._serve_auth_page("oplog.html")
            elif path == "/api/auth/me":
                self._json({"ok": True, **user})
            elif path == "/api/auth/users":
                from webui import auth
                self._json({"ok": True, "users": auth.list_users()})
            elif path in ("/api/auth/audit", "/api/oplog"):
                # ★ 2026-11-12: 操作日志 —— admin 看全部；其他账号只能看自己的
                from webui import auth
                def _p(k):
                    v = (q.get(k) or [None])[0]
                    return str(v).strip() if v not in (None, "") else None
                me = getattr(self, "_user", None) or {}
                actor, scope = _p("actor"), "all"
                if me.get("role") != "admin":
                    actor, scope = str(me.get("name") or ""), "self"
                self._json({"ok": True, "scope": scope,
                            "items": auth.list_audit(limit=_p("limit") or 200, actor=actor,
                                                     cat=_p("cat"), days=_p("days"), q=_p("q"))})
            elif path == "/" or path == "/index.html":
                self._serve_html()
            elif path == "/api/bootstrap":
                self._json(self._api_bootstrap())
            elif path == "/api/configs":
                self._json(self._api_configs())
            elif path == "/api/log":
                self._json(self._api_log(q))
            elif path == "/api/config":
                self._json(self._api_config_get(q.get("file", ["config.yaml"])[0]))
            elif path == "/api/files":
                self._json(self._api_files(q))
            elif path == "/api/step-files":
                self._json(self._api_step_files(q))
            elif path == "/api/integrated-report":
                self._json(self._api_integrated(q))
            elif path == "/api/integrated-report/view":
                self._serve_integrated_report(q)
            elif path == "/api/integrated-report/progress":
                self._json(self._api_integrated_progress(q))
            elif path == "/api/integrated-report/download":
                self._api_integrated_download(q)
            elif path == "/api/download":
                res = self._api_download(q)
                if res is not None:
                    self._json(res, 404 if res.get("error", "").startswith("文件不存在") else 400)
            elif path == "/api/status":
                self._json(self._api_status(q, path))
            elif path == "/api/step15-review":
                self._json(self._api_step15_review(q))
            elif path == "/api/step15-review/pdf":
                self._api_step15_pdf(q)
            elif path == "/api/step15-settings":
                self._json(self._api_step15_settings(q))
            elif path == "/api/keyword-lists":
                cfg = self._status_cfg(q.get("file", [None])[0])
                panel = keyword_lists_panel(cfg["CAT"], cfg["DATA_ROOT"])
                from core import keyword_confirmation as kc
                root = cat_root_dir(cfg["CAT"], cfg["DATA_ROOT"])
                gate = kc.read(root)
                active = RUNNER.active_for(cfg["CAT"]) or _lock_live_started(cfg["CAT"], cfg["DATA_ROOT"])[0]
                if gate and gate.get("status") == "confirmed" and gate.get("confirmed_words") and panel.get("ok") and not active:
                    if kc.revision(kc.current(cfg["CAT"], cfg["DATA_ROOT"])) != gate.get("confirmed_revision"):
                        gate = kc.pending(root, gate["confirmed_words"])
                if gate is None and panel.get("ok") and not active:
                    from core.paths import STEP_DIR_NAMES
                    from core.state import RunState
                    from steps.ids import MONITOR_STEP
                    monitor = root / STEP_DIR_NAMES[MONITOR_STEP]
                    state = RunState(str(monitor / "step-status.txt"), str(monitor / "step-duration.csv"))
                    if state.status_of(8) == "DONE" and not state.is_finished(9):
                        gate = kc.pending(root, kc.current(cfg["CAT"], cfg["DATA_ROOT"]))
                panel["confirmation"] = gate
                if panel.get("ok"):
                    panel["confirmation_revision"] = kc.revision(kc.current(cfg["CAT"], cfg["DATA_ROOT"]))
                self._json(panel)
            elif path == "/api/attr-spec":
                self._json(self._api_attr_spec(q))
            elif path == "/step18-panel.js" or path == "/step18-panel.css":
                asset = WEBUI_DIR / path.lstrip("/")
                self._send(200, asset.read_bytes(), "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
            elif path == "/api/std-workspace":
                self._json(self._api_std_workspace(q))
            elif path == "/api/std-workspace/export-status":
                from tools.attr_stability.export_state import export_status
                self._json({"ok": True, **export_status(self._std_workspace((q.get("file") or [None])[0]).root)})
            elif path == "/api/std-workspace/job":
                self._json(self._api_std_workspace_job(q))
            elif path == "/api/std-workspace/jobs":
                from webui.step18_api import list_jobs
                self._json({"ok": True, "jobs": list_jobs(self._std_workspace((q.get("file") or [None])[0]))})
            elif path == "/api/std-vocab":
                self._json(self._api_std_vocab(q))
            elif path == "/api/std-vocab/rollback/history":
                self._json(self._api_std_vocab_rollback_history(q))
            elif path == "/api/history":
                self._json(self._api_history())
            elif path == "/api/category/shared":
                self._json(self._api_category_shared(q))
            else:
                self._json({"ok": False, "error": f"未知接口: {path}"}, 404)
        except Forbidden as exc:
            self._json({"ok": False, "error": str(exc)}, 403)     # 已登录但无权访问该类目
        except UserFacingError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)     # 用户可见拒绝(非服务端故障)
        except ValueError as exc:
            # 参数类错误 → 400(2026-09-15 修: 原 do_GET 没有这一支, 非法参数(如 step=abc)会落到
            # 下面的兜底返回 500 "ValueError: …", 前端看着像服务端崩了; do_POST 一直有这支)
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            if is_client_gone(exc):
                log_client_abort(f"GET {path}", exc)
                return
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            user = self._authorize(path, post=True)
            if user is None: return
            if not path.startswith("/api/auth/"):
                self._log_operation(user, path)
            if path == "/api/auth/login":
                from webui import auth
                body = self._read_body()
                token = auth.login(body.get("username"), body.get("password"))
                if not token: self._auth_response({"ok": False, "error": "账号或密码错误，或尝试次数过多，请稍后再试"}, 401)
                else: self._auth_response({"ok": True}, cookie=token)
            elif path == "/api/auth/logout":
                from webui import auth
                auth.logout(self._session_token())
                self._auth_response({"ok": True}, cookie="")
            elif path == "/api/auth/password":
                from webui import auth
                body = self._read_body()
                auth.valid_password(body.get("password"))
                check = auth.login(user["name"], body.get("current_password"))
                if not check:
                    self._auth_response({"ok": False, "error": "当前密码错误或尝试次数过多"}, 403); return
                auth.logout(check)
                auth.update_user(user["name"], actor=user["name"], password=body.get("password"))
                self._auth_response({"ok": True}, cookie="")
            elif path in ("/api/auth/users/create", "/api/auth/users/update"):
                from webui import auth
                body = self._read_body()
                check = auth.login(user["name"], body.get("admin_password"))
                if not check:
                    self._auth_response({"ok": False, "error": "管理员密码错误或尝试次数过多"}, 403); return
                auth.logout(check)
                if path.endswith("/create"):
                    auth.create_user(body.get("username"), body.get("password"), body.get("role"),
                                     actor=user["name"], categories=body.get("categories"))
                else:
                    auth.update_user(body.get("username"), actor=user["name"], role=body.get("role"),
                                     enabled=body.get("enabled"), password=body.get("password"),
                                     categories=body.get("categories"))
                self._json({"ok": True})
            elif path == "/api/run":
                self._json(self._api_run(self._read_body()))
            elif path == "/api/step15-review/confirm":
                self._json(self._api_step15_review_confirm(self._read_body()))
            elif path == "/api/step15-settings/save":
                self._json(self._api_step15_settings_save(self._read_body()))
            elif path == "/api/stop":
                self._json(self._api_stop(self._read_body()))

            elif path == "/api/config/save":
                _r = self._api_config_save(self._read_body())
                invalidate_config_cache()      # 配置清单(下拉)立即刷新
                self._json(_r)
            elif path == "/api/reset-step":
                self._json(self._api_reset_step(self._read_body()))
            elif path == "/api/archive":
                self._json(self._api_archive(self._read_body()))
            elif path == "/api/integrated-report/build":
                self._json(self._api_integrated_build(self._read_body()))
            elif path == "/api/keyword-lists/save":
                body = self._read_body()
                cfg = self._status_cfg(str(body.get("config_file") or "config.yaml"))
                kind = str(body.get("list") or "core")
                self._keyword_edit_guard(cfg, capture_baseline=kind in ("core", "longtail"))
                self._json(save_keyword_lists(cfg["CAT"], cfg["DATA_ROOT"],
                                              kind,
                                              str(body.get("mode") or "auto"),
                                              body.get("words")))
            elif path == "/api/keyword-lists/stop":
                body = self._read_body()
                cfg = self._status_cfg(str(body.get("config_file") or "config.yaml"))
                self._keyword_edit_guard(cfg, capture_baseline=True)
                self._json(save_keyword_stop(cfg["CAT"], cfg["DATA_ROOT"],
                                             bool(body.get("stopped"))))
            elif path == "/api/keyword-lists/confirm":
                body = self._read_body()
                cfg = self._status_cfg(str(body.get("config_file") or "config.yaml"))
                from core import keyword_confirmation as kc
                gate = kc.read(cat_root_dir(cfg["CAT"], cfg["DATA_ROOT"]))
                now = kc.current(cfg["CAT"], cfg["DATA_ROOT"])
                if not gate or gate.get("status") != "pending":
                    raise UserFacingError("当前没有待确认的关键词清单，请刷新页面。")
                if now["stopped"] or kc.revision(now) != body.get("revision"):
                    raise UserFacingError("关键词清单已变化或处于人工停止状态，请刷新后重新确认。")
                body["mode"] = "confirm_keywords"
                self._json(self._api_run(body))
            elif path == "/api/category/create":
                _r = self._api_category_create(self._read_body())
                invalidate_config_cache()      # 新类目立刻出现在下拉里
                # ★ 2026-11-12: 记归属（创建人 + 创建时间），供「创建人」列与共享权限判断
                try:
                    from webui import auth as _auth
                    _newcat = str((_r or {}).get("cat") or "").strip()
                    if _newcat and not _auth.owner_of(_newcat):
                        _auth.set_owner(_newcat, str((getattr(self, "_user", None) or {}).get("name") or "admin"))
                except Exception:
                    pass
                self._json(_r)
            elif path == "/api/category/share":
                self._json(self._api_category_share(self._read_body()))
            elif path == "/api/month-fallback-confirm":
                self._json(self._api_month_fallback_confirm(self._read_body()))
            elif path == "/api/attr-spec/save":
                self._json(self._api_attr_spec_save(self._read_body()))
            elif path == "/api/attr-spec/confirm":
                self._json(self._api_attr_spec_confirm(self._read_body()))
            elif path == "/api/attr-exclude":
                self._json(self._api_attr_exclude(self._read_body()))
            elif path == "/api/attr-candidate":
                self._json(self._api_attr_candidate(self._read_body()))
            elif path.startswith("/api/std-workspace/"):
                self._json(self._api_std_workspace_action(path.rsplit("/", 1)[1], self._read_body()))
            elif path == "/api/std-vocab/regenerate":
                self._json(self._api_std_vocab_regenerate(self._read_body()))
            elif path == "/api/std-vocab/chat":
                # ★ 2026-10-22 新增: 对话式词表收敛（用户提问 → AI 回复 + 更新词表）
                self._json(self._api_std_vocab_chat(self._read_body()))
            elif path == "/api/std-vocab/save":
                self._json(self._api_std_vocab_save(self._read_body()))
            elif path == "/api/std-vocab/chat-history":
                # ★ 2026-10-28: 把前端对话（含重判过程消息）落盘，重开弹窗不再丢
                self._json(self._api_std_vocab_chat_history(self._read_body()))
            elif path == "/api/std-vocab/reannotate":
                self._json(self._api_std_vocab_reannotate(self._read_body()))
            elif path == "/api/std-vocab/prompts":
                self._json(self._api_std_vocab_prompts(self._read_body()))
            elif path == "/api/std-vocab/rollback":
                self._json(self._api_std_vocab_rollback(self._read_body()))
            else:
                self._json({"ok": False, "error": f"未知接口: {path}"}, 404)
        except Forbidden as exc:
            self._json({"ok": False, "error": str(exc)}, 403)     # 已登录但无权访问该类目
        except UserFacingError as exc:
            # 用户可见拒绝(同类目重复启动/与在跑类目冲突/维护命令撞车) → 409, 原文返回;
            # 原先是 500 + "RuntimeError: …", 前端弹出来像服务端崩了(2026-09-15 修)
            self._json({"ok": False, "error": str(exc)}, 409)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            if is_client_gone(exc):
                log_client_abort(f"POST {path}", exc)
                return
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _api_category_shared(self, q) -> dict:
        """GET /api/category/shared?cat= → 归属 + 共享清单（可见即可读）。"""
        cat = str((q.get("cat") or [""])[0] or "").strip()
        if not cat:
            raise ValueError("缺少 cat")
        self._require_cat(cat)
        from webui import auth
        row = auth.owner_of(cat) or {}
        created = ""
        if row.get("created_at"):
            try:
                created = datetime.fromtimestamp(float(row["created_at"])).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                created = ""
        return {"ok": True, "cat": cat, "owner": str(row.get("owner") or "") or "admin",
                "created_at": created, "shares": auth.shares(cat)}

    def _api_category_share(self, body: dict) -> dict:
        """POST /api/category/share {cat,username,action:add|remove,level:view|run}。

        仅 admin 或该类目创建人可操作；撤销只影响**下次访问**，不打断正在运行的进程。
        """
        from webui import auth
        cat = str(body.get("cat") or "").strip()
        username = str(body.get("username") or "").strip()
        action = str(body.get("action") or "add").strip().lower()
        level = str(body.get("level") or "view").strip().lower()
        if not cat or not username:
            raise ValueError("缺少 cat 或 username")
        if level not in auth.SHARE_LEVELS:
            raise ValueError("共享级别必须是 view 或 run")
        me = getattr(self, "_user", None) or {}
        name = str(me.get("name") or "")
        row = auth.owner_of(cat) or {}
        is_owner = str(row.get("owner") or "").lower() == name.lower()
        if me.get("role") != "admin" and not is_owner:
            raise Forbidden("只有管理员或该类目创建人可以共享/撤销")
        if not any(str(u.get("name") or "").lower() == username.lower() for u in auth.list_users()):
            raise ValueError("账号不存在: %s" % username)
        if action == "remove":
            auth.unshare(cat, username, name)
        else:
            auth.share(cat, username, level, name)
        return {"ok": True, "cat": cat, "shares": auth.shares(cat)}

    def _api_category_create(self, body: dict) -> dict:
        """创建类目并以所选市场、月份和账号覆盖复制源中的对应设置。"""
        from core.category_config import REGION_PROFILE, is_placeholder_cat

        cat = str(body.get("cat") or "").strip()
        if not cat:
            raise ValueError("缺少类目名 CAT")
        if re.search(r"[<>:\"/\\|?*\x00-\x1f]", cat) or cat.startswith(".") or cat in (".", ".."):
            raise ValueError(f"类目名含非法字符: {cat}")
        if is_placeholder_cat(cat):
            raise ValueError("类目名是占位/模板名, 请填真实类目(如 xxx-US-202609)")
        market_code = str(body.get("market_code") or "").strip().lower()
        profile = REGION_PROFILE.get(market_code)
        if not profile or not profile.get("site") or not profile.get("market"):
            raise ValueError("请选择支持的市场")
        period_mode = str(body.get("period_mode") or "fixed_month").strip()
        if period_mode not in ("fixed_month", "recent30"):
            raise ValueError("请选择固定月份或最近30天")
        if period_mode == "recent30":
            now = datetime.now()
            year, month = str(now.year), f"{now.month:02d}"
            trend_year = now.year - (1 if now.month == 1 else 0)
            trend_month = f"{trend_year}{12 if now.month == 1 else now.month - 1:02d}"
        else:
            year = str(body.get("year") or "").strip()
            month = str(body.get("month") or "").strip()
            if not re.fullmatch(r"20\d{2}", year) or not re.fullmatch(r"(?:0?[1-9]|1[0-2])", month):
                raise ValueError("请选择有效的年份和月份")
            trend_month = f"{year}{int(month):02d}"
        account = str(body.get("ss_account") or "").strip()
        password = str(body.get("ss_password") or "").strip()
        if not account or not password:
            raise ValueError("请填写卖家精灵账号和密码")
        if re.search(r"[\x00-\x1f\x7f]", account + password):
            raise ValueError("卖家精灵账号和密码不能包含控制字符")
        list_url = str(body.get("list_url") or "").strip()
        if list_url:
            from steps.step00_bestsellers import Step00Bestsellers
            problem = Step00Bestsellers._validate_bs_url(list_url)
            if problem:
                raise ValueError(f"榜单 URL 格式不正确: {problem}")
            parsed_url = urlparse(list_url)
            allowed_hosts = {profile["market"], f"www.{profile['market']}"}
            if parsed_url.netloc.lower() not in allowed_hosts:
                raise ValueError(f"榜单 URL 站点与所选市场不一致，请使用 {profile['market']}")
        # 兼容用户输入旧式完整目录名，统一采用当前选中的市场与月份。
        cat = re.sub(r"-(?:US|USA|UK|GB|DE|FR|IT|ES|CA|JP|MX|BR|AU|IN|AE)-20\d{4}(?:\d{2})?(?:-RECENT30)?$", "", cat, flags=re.I).strip()
        if not cat:
            raise ValueError("请填写类目名称")
        month = f"{int(month):02d}"
        date_suffix = now.strftime("%Y%m%d") if period_mode == "recent30" else f"{year}{month}"
        cat = f"{cat}-{market_code.upper()}-{date_suffix}" + ("-RECENT30" if period_mode == "recent30" else "")
        data_root = PROJECT_ROOT / "data"
        target = data_root / cat / "config.yaml"
        if target.exists():
            raise ValueError(f"该类目配置已存在: data/{cat}/config.yaml(可直接选中使用)")
        # 复制源: copy_from(根/类目/快照均可解析)或根模板
        src = PROJECT_ROOT / "config.yaml"
        cf = str(body.get("copy_from") or "").strip()
        if cf:
            try:
                src = safe_config_path(cf)
            except ValueError:
                src = PROJECT_ROOT / "config.yaml"
        text = src.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines()
        patch = {
            "CAT": cat, "LIST_URLS": list_url,
            "SITE": profile["site"], "MARKET": profile["market"],
            "TRAFFIC_MONTH": "最近30天" if period_mode == "recent30" else f"{year}-{month}",
            "COMPETITOR_MONTH": "最近30天" if period_mode == "recent30" else f"{year}-{month}",
            "TREND_MONTH": trend_month, "SS_ACTIVE_ACCOUNT": "auto",
        }
        out_lines = patch_yaml_lines(lines, patch)
        # 清除旧主账号后保留备用账号，避免复制源的 is_main 覆盖本次输入。
        from steps._ss_accounts import _parse_accounts
        inherited = _parse_accounts(load_yaml_file(src))
        backups = [item for item in inherited
                   if not item["is_main"] and item["account"].lower() != account.lower()]
        accounts = [{"account": account, "password": password, "note": "主账号", "is_main": True}, *backups]
        block = ["SS_ACCOUNTS:"]
        for item in accounts:
            block.extend([
                f"  - account: {json.dumps(item['account'], ensure_ascii=False)}",
                f"    password: {json.dumps(item['password'], ensure_ascii=False)}",
                f"    note: {json.dumps(item['note'], ensure_ascii=False)}",
                f"    is_main: {'true' if item['is_main'] else 'false'}",
            ])
        out_lines = replace_yaml_block(out_lines, "SS_ACCOUNTS", block)
        for key, value in (("SS_ACCOUNT", account), ("SS_PASSWORD", password)):
            out_lines = [line for line in out_lines if not re.match(rf"^{key}:", line)]
            out_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(out_lines) + "\n")
        except FileExistsError:
            raise ValueError(f"该类目配置已存在: data/{cat}/config.yaml(可直接选中使用)") from None
        push_cat(cat, f"新建类目: {cat} -> data/{cat}/config.yaml(复制自 {src.name})")
        return {"ok": True, "file": f"data/{cat}/config.yaml", "cat": cat}

    def _api_month_fallback_confirm(self, body: dict) -> dict:
        """网页弹窗确认"目标月缺失→改写最近30天"(2026-09-30): 仅用户点同意后调用。

        不改则配置保持原值; 本接口改动不备份(用户要求), 但会写入控制台日志。
        """
        key = str(body.get("key") or "").strip()
        file = str(body.get("config_file") or "").strip()
        month = str(body.get("month") or "").strip()
        if key not in ("TRAFFIC_MONTH", "COMPETITOR_MONTH"):
            raise ValueError("仅支持 TRAFFIC_MONTH / COMPETITOR_MONTH")
        if not file:
            raise ValueError("缺少 config_file")
        path = safe_config_path(file)
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        out_lines = patch_yaml_lines(lines, {key: "最近30天"})
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        load_yaml_file(path)   # 校验改写后可解析
        # 按该配置的类目路由(月份回退发生在本类目运行时 → 进本类目缓冲; 不广播到别类目)
        _mf_cat = str(load_yaml_file(path).get("CAT", "") or "").strip()
        push_cat(_mf_cat, f"[配置事件] 用户同意: {file} 的 {key} 改写为「最近30天」(原 {month or '?'}; "
                          f"不备份, 改动已记录) —— 请重新运行对应步骤")
        return {"ok": True, "key": key, "value": "最近30天", "file": file}

    # ---- 实现: 属性集规格(步骤16)
    def _attr_spec_resolve(self, file_q: str | None) -> tuple[str, Path]:
        """解析配置 → (CAT, <CAT>-attribute-spec.json 路径)。缺 CAT 抛 ValueError。

        ★ 接线（2026-10-10）：定义层走 `catalog.resolve_spec_path` —— **自己的目录优先，
          自己目录没有则从共享沉淀层（`_catalog/<类目>/`）继承**。迁移后面板仍能找到。
        """
        cfg = self._status_cfg(file_q)
        cat = str(cfg.get("CAT", "") or "").strip()
        if not cat:
            raise ValueError("配置缺少 CAT(类目名), 请先选择有效类目")
        base = cat_root_dir(cat, str(cfg.get("DATA_ROOT", "data") or "data"))
        try:
            from tools.attr_stability.catalog import resolve_spec_path
            return cat, resolve_spec_path(base.parent, cat)
        except Exception:                                        # noqa: BLE001 — 兜底原行为
            return cat, base / f"{cat}-attribute-spec.json"

    @staticmethod
    def _attr_spec_load(path: Path) -> dict | None:
        try:
            spec = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            return spec if isinstance(spec, dict) else None
        except Exception:
            return None

    def _commit_attr_spec(self, file_q: str | None, spec: dict, expected_revision=None) -> dict:
        from tools.attr_stability.spec_io import commit_attr_spec
        cfg = self._status_cfg(file_q)
        root = cat_root_dir(str(cfg.get("CAT") or ""), str(cfg.get("DATA_ROOT") or "data"))
        return commit_attr_spec(root, spec, expected_revision=expected_revision)

    def _spec_add_attribute(self, file_q: str | None, name: str,
                            hint: str = "", expected_revision=None) -> tuple[bool, list[str]]:
        """把一个属性**直接加进 spec**（已存在则只补空 hint）。返回 (是否新增, 现有属性名表)。

        ★★ 方案 C（2026-10-12 用户口径）：**spec 是唯一真值** —— 【采纳候选】【强制加回】
          以前都写 config 的 `ATTR_KEYS`（靠步骤16 再"加回来"），现在**直写 spec**。
          这样不再需要那条隐式通道，也不会出现"写了没人读"的静默失效。

        保存后把 `status` 回落 `pending_confirm`（人工动过属性集 → 停① 必须重确认，与
        `_api_attr_spec_save` 同口径）。
        """
        _cat, path = self._attr_spec_resolve(file_q)
        spec = self._attr_spec_load(path) if path.is_file() else None
        if spec is None or not isinstance(spec.get("attributes"), list):
            raise ValueError("尚无属性集规格，请先运行步骤16 生成后再加属性")
        from tools.attr_stability.spec_io import spec_revision, validate_attr_spec
        validate_attr_spec(spec)
        revision = spec_revision(spec) if expected_revision is None else expected_revision
        attrs = [a for a in spec["attributes"] if isinstance(a, dict)]
        from tools.attr_stability.feature_groups import iter_attribute_refs
        for a in attrs + iter_attribute_refs(attrs):
            if str(a.get("name") or "").strip() == name:
                if hint and not str(a.get("hint") or "").strip():
                    a["hint"] = hint
                spec["status"] = ATTR_SPEC_PENDING
                self._commit_attr_spec(file_q, spec, revision)
                return False, [str(x.get("name")).strip() for x in attrs]
        attrs.append({"name": name, "hint": hint, "source": "manual",
                      "runs": 1, "modified": True})
        spec["attributes"] = attrs
        spec["status"] = ATTR_SPEC_PENDING
        self._commit_attr_spec(file_q, spec, revision)
        return True, [str(x.get("name")).strip() for x in attrs]

    def _api_attr_spec(self, q: dict) -> dict:
        """读取步骤16 属性集规格(不存在则 exists=False + 空清单)。"""
        cat, path = self._attr_spec_resolve(q.get("file", [None])[0])
        from tools.attr_stability.spec_io import load_attr_spec, spec_revision
        spec = load_attr_spec(path, allow_empty=True) if path.exists() else None
        attrs_raw = spec.get("attributes") if spec else None
        attrs: list[dict] = []
        if isinstance(attrs_raw, list):
            for a in attrs_raw:
                if not isinstance(a, dict) or not str(a.get("name") or "").strip():
                    continue
                attrs.append({
                    "name": str(a.get("name") or "").strip(),
                    "hint": str(a.get("hint") or "").strip(),
                    "source": str(a.get("source") or "").strip(),
                    "modified": bool(a.get("modified")),
                    "runs": a.get("runs"),
                    "is_new": bool(a.get("is_new")),
                    # ★ 2026-10-26: 疑似幻觉（高价段文案提及率 0%）——只标记，不自动删
                    "疑似幻觉": bool(a.get("疑似幻觉")),
                    "疑似幻觉原因": str(a.get("疑似幻觉原因") or ""),
                })
                if isinstance(a.get("group_members"), list):
                    attrs[-1]["group_members"] = [{"name": c.get("name", ""), "hint": c.get("hint", ""),
                                                     "is_new": c.get("is_new") is True}
                                                    for c in a["group_members"] if isinstance(c, dict)]
                    attrs[-1]["group_reason"] = str(a.get("group_reason") or "")
        # ★ 属性 → 出现过的价格段(供前端按「维度 + 价格段」二次分组), 无清单则不返回
        bands_map: dict[str, list] = {}
        band_order: list[str] = []
        coverage: dict = {}
        mention_meta: dict = {}
        side = path.parent / "16_产品属性标注" / "属性-价格段.json"
        if side.exists():
            try:
                import json as _json
                sd = _json.loads(side.read_text(encoding="utf-8-sig"))
                bands_map = {str(k): [str(x) for x in (v or [])]
                             for k, v in (sd.get("属性价格段") or {}).items()}
                band_order = [str(x) for x in (sd.get("价格段顺序") or [])]
                coverage = sd.get("覆盖率") or {}
                mention_meta = sd.get("文案提及率") or {}
            except (OSError, ValueError):
                bands_map, band_order, coverage, mention_meta = {}, [], {}, {}
            # ★ 2026-10-29 用户口径: 属性改名后价格段会"消失"（侧车按属性名当 key）。
            #   这里按 属性改名.json 做**回退**：新名查不到就用旧名的数据补上（取并集）。
            try:
                _mf = side.parent / "属性改名.json"
                _rm = (_json.loads(_mf.read_text(encoding="utf-8-sig")).get("映射") or {}) \
                    if _mf.exists() else {}
                for _new, _olds in _rm.items():
                    _new = str(_new)
                    if bands_map.get(_new):
                        continue
                    _acc = []
                    for _o in (_olds or []):
                        _acc += list(bands_map.get(str(_o)) or [])
                    if _acc:
                        _uniq = {b for b in _acc}
                        bands_map[_new] = ([b for b in band_order if b in _uniq]
                                           + sorted(_uniq - set(band_order)))
                    for _band, _m in (coverage or {}).items():
                        if not isinstance(_m, dict) or _new in _m:
                            continue
                        for _o in (_olds or []):
                            if _o in _m:
                                _m[_new] = _m[str(_o)]
                                break
            except Exception:  # noqa: BLE001
                pass
        for a in attrs:
            a["bands"] = bands_map.get(a["name"], [])
        return {
            "ok": True,
            "cat": cat,
            "exists": bool(spec),
            "status": str(spec.get("status") or "") if spec else "",
            "revision": spec_revision(spec),
            "attributes": attrs,
            "band_order": band_order,
            # ★ 2026-10-25: 覆盖率矩阵 = **文案提及率**（不是功能差异化，见步骤16 注释）
            "coverage": coverage,
            "mention_meta": mention_meta,
            # ★ 「已排除属性」常驻可见（2026-10-10）：不藏起来，否则过两周就忘了
            #   自己排除过什么，而 AI 又一直在提议它（那正是"排除可能过时"的信号）。
            "excluded": self._excluded_list(q.get("file", [None])[0]),
            # ★ 「候选属性」（2026-10-12 用户口径）：步骤16 抽到、但 `runs < 阈值`
            #   被挡在 spec 之外的属性 —— 原先只写在 `属性集待确认清单.json`，
            #   **Web 里看不到** → 人会漏掉"值得加的新维度"。现在常驻显示 + 可采纳/忽略。
            "candidates": self._candidates_list(q.get("file", [None])[0]),
            # ★ 「抽取遍数」+ 自动推导的票数门槛（2026-10-12 用户口径）：
            #   这两个键是**一个比例的两半**，所以界面上**只给一个输入框**（抽取遍数），
            #   门槛**只读显示**、由 N 推导 —— 物理上不可能"只调一个"。
            #   数值口径**只从 step16 取**（单一真值），前端不自己算。
            "spec_runs": self._spec_runs_info(q.get("file", [None])[0]),
            # ★ 步骤17「交付值一致率**提示**门槛」（2026-10-12）：**只提示、不阻断**。
            #   ⚠ 之前 step17 文件头与 Web 提示都写"低于则阻断" —— 与代码不符（代码只 log 警告）。
            #     界面上必须说清，否则人会以为是个硬闸门而不敢调。
            "stab_min_rate": self._stab_min_rate_info(q.get("file", [None])[0]),
            # ★ 2026-11-12: 属性「合并 / 删除」审查清单（步骤16 运行时生成）
            "review": self._merge_review(path),
        }

    @staticmethod
    def _merge_review(spec_path: Path) -> dict:
        """读 `<CAT>/16_产品属性标注/属性合并与删除审查.json`（没有就返回空）。"""
        f = spec_path.parent / "16_产品属性标注" / "属性合并与删除审查.json"
        if not f.is_file():
            return {}
        try:
            import json as _json
            d = _json.loads(f.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _stab_min_rate_info(self, file_q: str | None) -> dict:
        """步骤17 一致率**提示**门槛的当前值（口径只从 step17 的默认值取，单一真值）。"""
        return {"value": 0.0, "default": 0.0, "overridden": False,
                "blocking": False, "off": True}
    def _spec_runs_info(self, file_q: str | None) -> dict:
        """与步骤16 执行端共用抽样模式、轮数及门槛，兼容旧价格段配置。"""
        from steps.step16_attribute_annotation import spec_extraction_settings
        return spec_extraction_settings(self._cat_cfg_values(file_q))

    def _candidates_list(self, file_q: str | None) -> list[dict]:
        """读「属性集待确认清单」（步骤16 产出）里的候选属性。

        产物：`<cat_root>/16_产品属性标注/属性集待确认清单.json`
          `{cat, 阈值, generated_at, items: [{属性, 出现次数, 说明}]}`
        读不到（没跑过步骤16 / 没触发阈值）→ 返回 `[]`（UI 自动隐藏该区域）。
        """
        try:
            cfg = self._status_cfg(file_q)
            cat = str(cfg.get("CAT", "") or "").strip()
            base = cat_root_dir(cat, str(cfg.get("DATA_ROOT", "data") or "data"))
        except Exception:                                        # noqa: BLE001
            return []
        p = base / "16_产品属性标注" / "属性集待确认清单.json"
        if not p.is_file():
            return []
        try:
            o = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            return []
        # 已决的（采纳=已在 spec / 忽略=已在不要清单）不再列为候选 →
        # 点完【采纳】/【忽略】立刻从面板消失，且**不动 step16 的产物文件**（下一轮它会自己重写）。
        # ★ 方案 C（2026-10-12）："采纳"的落点已从 `ATTR_KEYS` 改成 **spec**，
        #   所以这里按 **spec 的属性名** ∪ `ATTR_EXCLUDE` 判定"已决"。
        _cat, _sp = self._attr_spec_resolve(file_q)
        _spec = self._attr_spec_load(_sp) if _sp.is_file() else None
        decided = {str(a.get("name") or "").strip()
                   for a in ((_spec or {}).get("attributes") or []) if isinstance(a, dict)}
        from tools.attr_stability.catalog import parse_name_list
        decided |= set(parse_name_list(self._cat_cfg_values(file_q).get("ATTR_EXCLUDE")))
        out: list[dict] = []
        for it in (o.get("items") or []) if isinstance(o, dict) else []:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("属性") or "").strip()
            if not nm or nm in decided:
                continue
            out.append({"name": nm,
                        "runs": it.get("出现次数"),
                        "hint": str(it.get("说明") or "")[:180]})
        return out

    def _api_attr_candidate(self, body: dict) -> dict:
        """步骤16 **候选属性**（`runs < 阈值`、未进 spec）的**采纳 / 忽略**（2026-10-12 用户口径）。

        为什么要它：候选原先只躺在 `16_产品属性标注/属性集待确认清单.json` 里，
        **Web 上根本看不到** → 一个真正值得加的新维度会被无声漏掉。现在面板常驻显示 + 一键决。

        · `adopt`  **直接加进 spec**（方案 C，2026-10-12：spec 是唯一真值），
                   **并**从 `ATTR_EXCLUDE` 移除 —— 不要清单是唯一删除通道，两边不能同时有。
        · `ignore` 写 `ATTR_EXCLUDE`（下轮 AI 再提议也照样被丢弃，且进 `_excluded_proposals.json` 留痕）。
        """
        action = str(body.get("action") or "").strip()
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("属性名不能为空")
        if action not in ("adopt", "ignore"):
            raise ValueError("action 必须是 adopt / ignore")
        cfg_file = str(body.get("config_file") or "").strip() or None
        cfg = self._status_cfg(cfg_file)
        cat = str(cfg.get("CAT", "") or "").strip()
        if not cat:
            raise ValueError("配置缺少 CAT(类目名), 请先选择有效类目")
        from tools.attr_stability.catalog import parse_name_list
        # ⚠ 必须读**配置文件本身**（`_status_cfg` 只有 CAT/DATA_ROOT，读 ATTR_* 恒空 →
        #   写回时会把整份清单抹掉）。
        exc = parse_name_list(self._cat_cfg_values(cfg_file).get("ATTR_EXCLUDE"))
        note = ""
        keys: list[str] = []
        if action == "adopt":
            # ★ 方案 C：落点是 **spec**（不再写 `ATTR_KEYS` —— 那个键已移除、没人读了）
            hint = ""
            for c in self._candidates_list(cfg_file):
                if c.get("name") == name:
                    hint = str(c.get("hint") or "")
                    break
            _added, keys = self._spec_add_attribute(cfg_file, name, hint, body.get("expected_revision"))
            if name in exc:
                exc = [x for x in exc if x != name]
                note = "已同时移出「不要清单」（它是唯一的删除通道，两边不能同时有）"
            self._patch_cat_config(cfg_file, {"ATTR_EXCLUDE": ";".join(exc)})
        else:
            if name not in exc:
                exc.append(name)
            self._patch_cat_config(cfg_file, {"ATTR_EXCLUDE": ";".join(exc)})
        push_cat(cat, "候选属性%s: %s%s"
                 % ("采纳(已加进属性集)" if action == "adopt" else "忽略(写入不要清单)",
                    name, ("；" + note) if note else ""))
        return {"ok": True, "action": action, "name": name, "note": note,
                "spec_attrs": keys, "excluded": self._excluded_list(cfg_file),
                "candidates": self._candidates_list(cfg_file)}

    def _excluded_list(self, file_q: str | None) -> list[dict]:
        """读「不要清单」（两级），返回带来源的条目。

        规则（用户口径 2026-10-09/10）：范围默认**本市场**（写运行层 `config.yaml`），
        要全类目排除则写共享层 `_catalog/<类目>/_catalog.json` 的 `排除属性`。
        这里把两级的**来源分开列出来**，人一眼能看出"这条是本市场不要，还是全类目不要"。
        """
        out: list[dict] = []
        try:
            cfg = self._status_cfg(file_q)
            cat = str(cfg.get("CAT", "") or "").strip()
            base = cat_root_dir(cat, str(cfg.get("DATA_ROOT", "data") or "data"))
        except Exception:                                        # noqa: BLE001
            return out
        # ★★ 修复（2026-10-12）：原来读 `cfg.get("ATTR_EXCLUDE")`（`cfg = _status_cfg` **只有
        #   {CAT, DATA_ROOT}**）→ **运行层条目永远是空的**，UI 上「已排除属性」只显示共享层那几条，
        #   人会以为自己的删除没生效。必须读**配置文件本身**。
        from tools.attr_stability.catalog import parse_name_list
        names = parse_name_list(self._cat_cfg_values(file_q).get("ATTR_EXCLUDE"))
        for n in names:
            out.append({"属性": n, "来源": "本市场", "范围": "config.yaml"})
        try:
            from tools.attr_stability.catalog import catalog_meta, parse_cat
            meta = catalog_meta(base.parent, parse_cat(cat)["类目"])
            for x in (meta.get("排除属性") or []):
                nm = str(x.get("属性") if isinstance(x, dict) else x).strip()
                if nm and not any(o["属性"] == nm for o in out):
                    out.append({"属性": nm, "来源": "全类目", "范围": "_catalog.json"})
        except (ImportError, OSError, ValueError):
            # ★★ 修复（审核中10）：宽 `except: pass` 会把全类目排除**静默消失**（假阴性——
            #   UI 显示"（无）"）。收窄到"环境/数据"类异常；编程错误让它冒出来。
            pass
        return out

    def _api_attr_exclude(self, body: dict) -> dict:
        """「不要清单」的增删（动作 `add` / `remove`）。

        **`remove` = 恢复**（用户口径）：移除条目 = **交回 AI 决定**（下轮步骤16
        会按数据重新判断它该不该有）。想**确定性加回**，用【强制加回】——
        它会从清单移除 **并直接加进属性集**（方案 C 起，不再走 `ATTR_KEYS`）。
        ⚠ 移除**只从清单里删**，**不动任何沉淀文件**：排除期间词表/判据都还在，
          一恢复立刻又能用（这就是"保留但过滤"而不是"删掉"的价值）。
        """
        action = str(body.get("action") or "").strip()
        name = str(body.get("name") or "").strip()
        scope = str(body.get("scope") or "mkt").strip()
        if not name:
            raise ValueError("属性名不能为空")
        if action not in ("add", "remove"):
            raise ValueError("action 必须是 add / remove")
        cfg_file = str(body.get("config_file") or "").strip() or None
        cfg = self._status_cfg(cfg_file)
        cat = str(cfg.get("CAT", "") or "").strip()
        base = cat_root_dir(cat, str(cfg.get("DATA_ROOT", "data") or "data"))

        if action == "remove" and bool(body.get("force")) and "expected_revision" in body:
            from tools.attr_stability.spec_io import load_attr_spec, spec_revision
            _cat, spec_path = self._attr_spec_resolve(cfg_file)
            current = load_attr_spec(spec_path)
            if body["expected_revision"] != spec_revision(current):
                raise ValueError("属性集已被其他操作更新，请重新加载后再恢复")

        if scope == "catalog":                                   # 全类目级
            from tools.attr_stability.catalog import catalog_dir_of, catalog_meta, parse_cat
            meta = catalog_meta(base.parent, parse_cat(cat)["类目"])
            cur = [str(x.get("属性") if isinstance(x, dict) else x).strip()
                   for x in (meta.get("排除属性") or [])]
            cur = [x for x in cur if x]
            if action == "add" and name not in cur:
                cur.append(name)
            if action == "remove":
                cur = [x for x in cur if x != name]
            meta["排除属性"] = cur
            p = catalog_dir_of(base.parent, parse_cat(cat)["类目"]) / "_catalog.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:                                                    # 本市场级（默认）
            # ★★ 修复（2026-10-12）：原来读 `cfg.get("ATTR_EXCLUDE")`，而 `cfg = _status_cfg(...)`
            #   **只有 {CAT, DATA_ROOT}** → 读到的一律是空 → 写回 `";".join([新条目])`
            #   **把原有清单整份抹掉**（真数据丢失，且 UI 上看起来"只是加了一条"）。
            #   必须读**配置文件本身**。
            from tools.attr_stability.catalog import parse_name_list
            cur = parse_name_list(self._cat_cfg_values(cfg_file).get("ATTR_EXCLUDE"))
            if action == "add" and name not in cur:
                cur.append(name)
            if action == "remove":
                cur = [x for x in cur if x != name]
            self._patch_cat_config(cfg_file, {"ATTR_EXCLUDE": ";".join(cur)})

        # ★ 「强制加回」：从不要清单移除 **并 直接加进 spec**（方案 C，2026-10-12）。
        #   以前是写 `ATTR_KEYS` 靠步骤16"再加回来"；现在 spec 是唯一真值、且步骤16
        #   **只增不减**（它不会再丢掉这个属性）→ 直写 spec 即可，隐式通道彻底不需要了。
        forced = ""
        if action == "remove" and bool(body.get("force")):
            try:
                _added, _names = self._spec_add_attribute(cfg_file, name, expected_revision=body.get("expected_revision"))
                forced = ("；已加进属性集（%s）" % ("新增" if _added else "本就在"))
            except ValueError as exc:                     # 还没有 spec（没跑过步骤16）
                forced = "；⚠ 没能加进属性集: %s" % exc
        push_cat(cat, "「不要清单」%s: %s%s"
                 % ("新增" if action == "add" else "移除(恢复)", name, forced))
        return {"ok": True, "action": action, "name": name, "scope": scope,
                "forced": forced.lstrip("；"),
                "excluded": self._excluded_list(cfg_file)}

    def _cat_config_path(self, file_q: str | None) -> Path:
        """该运行的 `config.yaml` **真实路径**（读值 / 写值都用它，别再各写一套）。

        ⚠ **不要拿 `_status_cfg` 的返回值去读业务键**：它**只返回 `{CAT, DATA_ROOT}`**，
          **不含 `ATTR_KEYS`/`ATTR_EXCLUDE`/`ATTR_DESC` 等**。用它读这些键会**恒为空**，
          再写回时就把整份清单**抹掉**（实测踩过：读空 → 只写新条目 → 旧的都没了）。
        """
        cfg = self._status_cfg(file_q)
        cat = str(cfg.get("CAT", "") or "").strip()
        data_root = str(cfg.get("DATA_ROOT", "data") or "data")
        if cat:
            return cat_root_dir(cat, data_root) / "config.yaml"
        return Path(safe_config_path(str(file_q or "config.yaml")))

    def _cat_cfg_values(self, file_q: str | None) -> dict:
        """读该运行 `config.yaml` 的**完整键值**（含 `ATTR_*`）。读不到 → `{}`。"""
        try:
            p = self._cat_config_path(file_q)
            return load_yaml_file(p) if p.is_file() else {}
        except Exception:                                        # noqa: BLE001
            return {}

    def _patch_cat_config(self, config_file: str | None, patch: dict) -> str:
        """把少量键**安全地**写进类目 `config.yaml`，返回人话说明（失败抛 `UserFacingError`）。

        复用 `_api_config_save` 那条经过实战的路径，不自己造写盘逻辑：
          校验 → 备份 → 写盘 → **再校验** → 失败回滚到**字节级原样**。
        （真事故教训：旧顺序"先备份→写盘→再校验"在校验失败时文件已被写坏。）
        """
        from core.config_backup import make_backup as _mk_backup

        # ★★ 修复（审核高1，2026-10-10）：`config_file` 为 None 时，原来写成
        #   `safe_config_path("config.yaml")` → **仓库根** `config.yaml`，
        #   而**读**走的是 `_status_cfg`（跟随运行类目）→ **读写不同文件**：
        #   "不要清单"静默不生效 + 污染仓库根配置（那是模板/默认配置）。
        #   现在按 `_status_cfg` 的 CAT/DATA_ROOT 解析到**真实类目** config。
        # ★★ 再修（2026-10-10）：抽成 `_cat_config_path`，**读写共用一条解析**。
        # ★★ 又修（2026-10-12）：上一版用 `category_config_path(data_root, cat)`
        #   = `Path(data_root)/cat/config.yaml` —— **不做 PROJECT_ROOT 解析**，
        #   而**读侧** `_excluded_list` 用 `cat_root_dir(cat, data_root)`（**会**解析到项目根）
        #   → 只有 `CWD == 项目根` 时两者才指向同一个文件（`start_webui.bat` 的 `cd /d`
        #   恰好挡住了这个分歧；换个启动方式就重现原 bug）。
        path = self._cat_config_path(config_file)
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        plain = [ln.rstrip("\r\n") for ln in text.splitlines(keepends=True)]
        new_lines = patch_yaml_lines(plain, patch)
        new_text = "\n".join(new_lines) + "\n"
        guard = yaml_save_guard(new_text)
        if guard:
            raise UserFacingError(guard)
        _mk_backup(path, reason="写入「不要清单」前自动备份",
                   log=lambda m: None, changed_to=new_text)
        original = path.read_bytes()
        path.write_text(new_text, encoding="utf-8")
        try:
            load_yaml_file(path)
        except Exception:                                        # noqa: BLE001
            path.write_bytes(original)                           # 字节级回滚
            raise
        return "已写入 config.yaml"

    def _api_attr_spec_save(self, body: dict) -> dict:
        """保存人工编辑后的属性集(增/删/改名/改说明), 状态回落到待确认。"""
        cat, path = self._attr_spec_resolve(str(body.get("config_file") or "").strip() or None)
        raw = body.get("attributes")
        if not isinstance(raw, list):
            raise ValueError("attributes 必须是属性列表")
        # ★★ 修复（审核高2，2026-10-10）：空列表会让 `removed = 全部旧属性` →
        #   一次空 POST 把**每个属性都写进「不要清单」**，下轮步骤16 全部丢弃、要人工逐条恢复。
        #   空属性集几乎不可能是本意（清空该逐个删除并确认），直接拒绝。
        if not raw:
            raise ValueError("属性集不能为空（如需清空请逐个删除并确认，误清空会把全部属性写进不要清单）")
        from tools.attr_stability.spec_io import load_attr_spec, spec_revision
        previous_spec = load_attr_spec(path, allow_empty=True) if path.exists() else None
        if previous_spec is not None and not isinstance(body.get("expected_revision"), str):
            raise ValueError("保存已有属性集必须携带版本，请刷新页面后再保存")
        prev_by_name: dict[str, dict] = {}
        prev_top_names: list[str] = []
        if previous_spec is not None:
            prev_by_name = {a["name"]: a for a in previous_spec["attributes"]}
            prev_top_names = list(prev_by_name)
            from tools.attr_stability.feature_groups import iter_attribute_refs
            for child in iter_attribute_refs(previous_spec["attributes"]):
                prev_by_name.setdefault(child["name"], child)
        attrs: list[dict] = []
        seen: set[str] = set()
        for it in raw:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                raise ValueError("属性名不能为空")
            if name in seen:
                raise ValueError(f"属性名重复: {name}")
            seen.add(name)
            hint = str(it.get("hint") or "").strip()
            prev = prev_by_name.get(name)
            if prev is not None:
                # hint_short(步骤16 精简说明)只在 hint 未变时保留; hint 变了则清空, 下次重新总结
                hint_short = str(prev.get("hint_short") or "").strip() \
                    if str(prev.get("hint") or "").strip() == hint else ""
                entry = dict(prev)
                if isinstance(prev.get("group_members"), list):
                    supplied = it.get("group_members")
                    old_children = prev["group_members"]
                    if (not isinstance(supplied, list) or len(supplied) != len(old_children)
                            or any(not isinstance(c, dict) for c in supplied)
                            or [c.get("name") for c in supplied] != [c.get("name") for c in old_children]):
                        raise ValueError(f"功能组 {name} 的子属性已变化，请刷新后再保存")
                    children = []
                    for old_child, new_child in zip(old_children, supplied):
                        child = dict(old_child)
                        child_hint = str(new_child.get("hint") or "").strip()
                        if child_hint != str(old_child.get("hint") or "").strip():
                            child["hint"] = child_hint
                            child["modified"] = True
                            child.pop("hint_short", None)
                        children.append(child)
                    entry["group_members"] = children
                entry.update({
                    "name": name,
                    "hint": hint,
                    "source": str(prev.get("source") or "manual"),
                    "runs": prev.get("runs"),
                    "modified": bool(prev.get("modified"))
                                or (str(prev.get("hint") or "").strip() != hint),
                })
                entry.pop("hint_short", None)
                if hint_short:
                    entry["hint_short"] = hint_short
                attrs.append(entry)
            else:
                attrs.append({"name": name, "hint": hint, "source": "manual",
                              "runs": 1, "modified": True})
        spec = dict(previous_spec or {})
        spec.update({"cat": cat, "status": ATTR_SPEC_PENDING, "attributes": attrs})
        committed = self._commit_attr_spec(
            str(body.get("config_file") or "").strip() or None, spec,
            body.get("expected_revision", spec_revision(previous_spec)))
        # ★★ **Web 删除 = 自动加入「不要清单」**（2026-10-09 用户口径）
        #   为什么必须自动（2026-10-09 用户口径）：人工删掉的属性只管一轮的话，
        #   下一轮步骤16 会按数据重新抽出来（实测 `厚度` 被删后又回来）。
        #   "我删了 = 我不要它" → 保存时**自动**写进 `ATTR_EXCLUDE`，不用再点一次。
        #   **范围默认本市场**（写进本运行的 config.yaml）；要全类目排除，另在共享层加。
        #   恢复：Web「已排除」区域移除条目即可（【强制加回】= 移除 + 直接加进属性集）。
        removed = [n for n in prev_top_names if n not in seen]
        excluded_note = ""
        if removed:
            # 2026-10-19 用户口径: **不再自动写「不要清单」**。原因: 每次抽取的属性名不同,
            # 按精确名自动排除不可靠(这轮叫"是否带负离子", 下轮可能抽成"是否负离子功能")。
            # 改为: 删除只当轮生效; 要持久排除由人工在「已排除属性」里手动加(按典型取值认准同一个维度)。
            excluded_note = ("；已删除 %s（**仅本轮生效**；如需持久排除，请在下方「已排除属性」手动加入"
                            "——每次抽取的属性名可能不同，请按典型取值认准同一维度）" % "、".join(removed))
        rules_sync = "; rules.json 对齐: 新增 %s / 删除 %s" % (committed["added"], committed["removed"])
        push_cat(cat, f"属性集规格已保存: {len(attrs)} 个属性(待人工确认){rules_sync}{excluded_note}")
        return {"ok": True, "cat": cat, "count": len(attrs), "status": ATTR_SPEC_PENDING,
                "rules_sync": rules_sync.lstrip("; "), "revision": committed["revision"],
                "excluded": removed, "excluded_note": excluded_note.lstrip("；")}

    def _api_attr_spec_confirm(self, body: dict) -> dict:
        """人工确认属性集(status → confirmed), 作为进入步骤17 的前置。"""
        cat, path = self._attr_spec_resolve(str(body.get("config_file") or "").strip() or None)
        spec = self._attr_spec_load(path) if path.is_file() else None
        if spec is None or not isinstance(spec.get("attributes"), list):
            raise ValueError("尚无属性集规格, 请先运行步骤16 生成后再确认")
        if not spec["attributes"]:
            raise ValueError("属性集为空, 无法确认(请先补充至少一个属性)")
        if not isinstance(body.get("expected_revision"), str):
            raise ValueError("确认属性集必须携带版本，请刷新页面后再确认")
        from tools.attr_stability.spec_io import spec_revision
        previous_revision = spec_revision(spec)
        spec["status"] = ATTR_SPEC_CONFIRMED
        committed = self._commit_attr_spec(str(body.get("config_file") or "").strip() or None, spec,
                                           body.get("expected_revision", previous_revision))
        push_cat(cat, f"属性集规格已确认: {len(spec['attributes'])} 个属性")
        # ★ 2026-10-21: **确认即沉淀** —— 把(规范名 + 别名)写进全局词库并重挖规则。
        #   这是"零人工维护"的关键一步: 确认一次, 该概念从此跨类目、跨运行固定, 不用再手写规则。
        try:
            from datetime import datetime

            from tools.attr_stability.synonym_layer import record_spec
            from tools.attr_stability.feature_groups import expand_attributes
            _rec = record_spec(str(path.parent), cat, expand_attributes(spec["attributes"]),
                               now=datetime.now().isoformat(timespec="seconds"))
            if _rec.get("ok"):
                push_cat(cat, "全局词库已更新: %d 规范名 · 新增 %d 别名 · 挖出 %d 条规则"
                         % (_rec.get("规范名", 0), _rec.get("别名新增", 0), _rec.get("规则", 0)))
            else:
                push_cat(cat, "⚠ 全局词库沉淀失败(不影响确认): %s" % _rec.get("reason"))
        except Exception as exc:  # noqa: BLE001 — 沉淀失败不影响确认动作本身
            push_cat(cat, "⚠ 全局词库沉淀异常(不影响确认): %s" % exc)
        return {"ok": True, "cat": cat, "status": ATTR_SPEC_CONFIRMED,
                "count": len(spec["attributes"]), "revision": committed["revision"]}

    def _std_workspace(self, config_file):
        from tools.attr_stability.step18_workspace import Workspace
        from tools.attr_stability.catalog import resolve_spec_path
        cat, data_root = self._std_vocab_cat(config_file)
        safe_cat_name(cat)
        root = cat_root_dir(cat, data_root).resolve()
        spec = resolve_spec_path(root.parent, cat)
        return Workspace(root, spec_path=spec, rules_path=self._std_rules_path(cat, data_root))

    def _api_std_workspace(self, query):
        from webui.step18_api import latest_job
        file = (query.get("file") or [None])[0]
        workspace = self._std_workspace(file)
        try:
            result = _std_workspace_summary(workspace)
        except ValueError as exc:
            # A missing upstream is an empty panel, not an endless load error.
            if not workspace.pointer.exists():
                return {"ok": True, "ready": False, "error": str(exc), "attributes": [], "history": []}
            raise
        include_job = (query.get("include_job") or ["1"])[0] != "0"
        return {**result, "ready": True,
                **({"job": latest_job(workspace)} if include_job else {})}

    def _api_std_workspace_job(self, query):
        from webui.step18_api import job_status
        workspace = self._std_workspace((query.get("file") or [None])[0])
        return job_status(workspace, (query.get("id") or [""])[0])

    def _api_std_workspace_action(self, action, body):
        from webui.step18_api import start_preview
        file = str(body.get("config_file") or "config.yaml")
        workspace = self._std_workspace(file)
        expected = body.get("expected_version")
        if action == "preview-page":
            from webui.step18_api import preview_page
            return preview_page(workspace, body.get("job_id"), page=body.get("page", 0),
                                term=body.get("term", ""), selection_rules=body.get("selection_rules"))
        if action == "cancel-job":
            from webui.step18_api import cancel_job
            return cancel_job(workspace, body.get("job_id"))
        lock_alive, _, lock_unknown = _lock_live_started(workspace.cat, str(workspace.root.parent))
        if RUNNER.active_for(workspace.cat) or lock_alive or lock_unknown:
            raise UserFacingError("当前类目流水线正在运行，请结束后再操作步骤18")
        if not isinstance(expected, str) or not expected:
            raise ValueError("缺少当前版本，请重新加载步骤18")
        if action == "retry-job":
            from webui.step18_api import retry_job
            return retry_job(workspace, body.get("job_id"), expected, load_yaml_file(safe_config_path(file)))
        if action == "preview":
            return start_preview(workspace, body, load_yaml_file(safe_config_path(file)))
        if action == "audit":
            from webui.step18_api import start_audit
            return start_audit(workspace, body, load_yaml_file(safe_config_path(file)))
        if action == "row-info":
            current = workspace.open()
            workspace._fresh(current, expected)
            row_id = str(body.get("row_id") or "").strip()
            row = next((r for r in current["rows"] if r["id"] == row_id), None)
            if row is None:
                raise ValueError("商品行不存在或已被删除")
            return {"ok": True, "row_id": row_id, "title": str(row["input"].get("标题") or ""),
                    "asin": str(row["input"].get("变体ASIN") or ""),
                    "attribute_count": len(row["attributes"])}
        if action == "delete-row":
            workspace.delete_row(body.get("row_id"), expected)
            return {**workspace.summary(), "ready": True}
        if action == "delete-attribute":
            workspace.delete_attribute(body.get("attribute"), expected)
            return {**workspace.summary(), "ready": True}
        if action == "value-rows-info":
            current = workspace.open()
            workspace._fresh(current, expected)
            rows = workspace.matching_value_rows(current, body.get("attribute"), body.get("values") if "values" in body else body.get("value"))
            return {"ok": True, "count": len(rows), "total": len(current["rows"]),
                    "examples": [{"row_id": r["id"], "title": str(r["input"].get("标题") or "")}
                                 for r in rows[:5]]}
        if action == "delete-value-rows":
            workspace.delete_rows_by_value(body.get("attribute"), body.get("values") if "values" in body else body.get("value"), expected)
            return {**workspace.summary(), "ready": True}
        if action == "apply":
            apply_started = time.perf_counter()
            applied = workspace.apply(body.get("proposal_id"), expected, selected_cells=body.get("selected_cells"),
                                      selection_rules=body.get("selection_rules"))
            apply_ms = round((time.perf_counter() - apply_started) * 1000, 1)
            from webui.step18_api import mark_applied
            try:
                mark_applied(workspace, body.get("proposal_id"), applied["operation"].get("omitted_cells", 0),
                             job_id=applied["operation"].get("job_id"))
            except (OSError, ValueError):
                pass  # The version is already committed; never report it as failed.
            if body.get("partial") is True:
                if any(p["type"] in ("row", "attribute") for p in applied["operation"].get("patches", [])):
                    refresh_started = time.perf_counter()
                    result = workspace.summary(state=applied)
                    return {**result, "ready": True, "rows_changed": True,
                            "timings_ms": {"apply": apply_ms, "refresh": round((time.perf_counter() - refresh_started) * 1000, 1)}}
                names = {p["attribute"] for p in applied["operation"].get("patches", [])}
                try:
                    refresh_started = time.perf_counter()
                    result = workspace.summary(state=applied, attribute_names=names)
                    return {**result, "ready": True, "partial": True,
                            "timings_ms": {"apply": apply_ms, "refresh": round((time.perf_counter() - refresh_started) * 1000, 1)}}
                except Exception:
                    # A committed version must never be presented as a failed apply.
                    return {"ok": True, "version": applied["version"], "refresh_required": True}
        elif action == "restore-preview":
            result = workspace.restore_proposal(body.get("version"), selective=body.get("selective") is True,
                                                expected=expected)
            return {"ok": True, **result}
        elif action == "refresh-input":
            workspace.refresh(expected)
        elif action == "export":
            from webui.step18_api import reserve_export
            with reserve_export(workspace):
                return workspace.export(expected)
        else:
            raise ValueError("未知步骤18操作")
        return {**workspace.summary(), "ready": True}

    # ---- 实现: 步骤18 标准化词表(每属性粒度阈值 + 重新生成 + 写回)
    def _std_vocab_cat(self, file_q: str | None) -> tuple[str, str]:
        cfg = self._status_cfg(file_q)
        cat = str(cfg.get("CAT", "") or "").strip()
        if not cat:
            raise ValueError("配置缺少 CAT(类目名), 请先选择有效类目")
        return cat, str(cfg.get("DATA_ROOT", "data") or "data")

    @staticmethod
    def _std_rules_path(cat: str, data_root: str, *, writable: bool = False):
        from tools.attr_stability.spec import resolve_rules_file
        path = resolve_rules_file(str(cat_root_dir(cat, data_root)), writable=writable)
        return Path(path) if path else None

    # ---- 词表聚类历史(多轮迭代): 18_标准化/词表聚类历史.json
    #   结构: {属性: [{轮次, 时间, 口径建议, 规范值, 归并, 已保存}]}
    #   「L1 全部口径建议 + L2 最近 5 轮完整结果」由 tools.onboard._vocab_history_block 拼进 prompt。
    #   ★★ **本市场专用，不共享**（2026-10-20 用户口径）：
    #      路径固定取自 `cat_root_dir()` = `data/<CAT>/`，**刻意不走** catalog.resolve_definition_file
    #      （那会把 `词表聚类报告.json` 解析到共享层 `_catalog/<类目>/`）；本文件也不以
    #      `-attribute-spec.json` / `.rules.json` / `.learned-map.json` / `词表聚类报告.json` 结尾，
    #      所以即使被通用解析器扫到也不会被当成定义层文件 → 每个市场各自一份历史，互不污染。
    def _std_vocab_history_path(self, cat: str, data_root: str) -> Path:
        return cat_root_dir(cat, data_root) / "18_标准化" / "词表聚类历史.json"

    @staticmethod
    def _load_std_vocab_history(path: Path) -> dict:
        try:
            if path.is_file():
                d = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(d, dict):
                    return d
        except Exception:
            pass
        return {}

    @staticmethod
    def _save_std_vocab_history(path: Path, hist: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _reannot_prompt(attr: str, hint: str, uniq: list, req: str,
                        rows: list, *, with_rows: bool = True) -> str:
        """重判提示词：字段说明 + 用户要求 + 唯一值（参考）+ 输出格式 + 商品资料。

        ★ 2026-10-27 用户口径：**不要**「只能从下面这些值里选一个」这种硬约束，
          让 LLM 按「字段说明 + 用户要求」自己判断；词表/唯一值只当参考。
          一律用字符串拼接（hint 里可能含 `%`，用 % 格式化会抛 TypeError）。
        """
        lines = []
        for r in rows:
            lines.append(json.dumps({
                "xl_row": r.get("xl_row"),
                "标题": str(r.get("标题") or "")[:800],
                "五点描述": str(r.get("五点描述") or "")[:800],
                "详细参数": str(r.get("详细参数") or "")[:800],
                # 图片链接也一起给（文字模式当文本线索；真看图走 vision 模式）
                "图片链接": str(r.get("图片链接") or r.get("主图") or "")[:500],
            }, ensure_ascii=False))
        _rule = _reannot_rule(hint, uniq, req)
        head = ("你是商品属性「" + attr + "」的复核标注器。下面是 " + str(len(rows))
                + " 行商品资料（标题 / 五点描述 / 详细参数 / 图片链接）。\n" + _rule
                + "严格输出 JSON：{\"rows\":[{\"xl_row\":行号,\"值\":\"...\","
                  "\"依据\":\"原文摘录(≤30字)\",\"来源\":\"标题/五点描述/详细参数/图片链接\"}]}，"
                  "不要输出其它文字。")
        if not with_rows:
            return head
        return head + "\n\n商品资料：\n" + "\n".join(lines)

    def _std_vocab_rows(self, cat: str, data_root: str) -> dict:
        stats = {}
        sp = cat_root_dir(cat, data_root) / "17_属性标注" / "唯一值统计.json"
        if sp.is_file():
            try:
                stats = json.loads(sp.read_text(encoding="utf-8-sig")).get("属性唯一值") or {}
            except Exception:
                stats = {}
        rules = {}
        rp = self._std_rules_path(cat, data_root)
        if rp:
            try:
                rules = json.loads(rp.read_text(encoding="utf-8-sig"))
            except Exception:
                rules = {}
        attr_map = {}
        for a in (rules.get("attributes") or []):
            if isinstance(a, dict) and a.get("name"):
                params = a.get("params") if isinstance(a.get("params"), dict) else {}
                attr_map[str(a["name"])] = {
                    "规范值": params.get("values") or a.get("values") or [],
                    # 归并口径改成**人工自然语言建议**（数字粒度已删，2026-10-08）
                    "口径建议": str(params.get("vocab_hint") or ""),
                }
        # 读词表聚类报告(若存在): 附上「原值→规范值」归并映射, 供人工审核
        report_map: dict[str, list] = {}
        rep_p = cat_root_dir(cat, data_root) / "18_标准化" / "词表聚类报告.json"
        if rep_p.is_file():
            try:
                rep = json.loads(rep_p.read_text(encoding="utf-8-sig"))
                for a in (rep.get("attributes") or []):
                    if isinstance(a, dict) and a.get("属性"):
                        report_map[str(a["属性"])] = a.get("归并") or []
            except Exception:
                report_map = {}
        # ★ 2026-10-28 用户口径: 面板要基于**「文本标注 + 视觉补充」合并后的那张表**统计，
        #   而不是只看文本结果（视觉补的 16000+ 格原先只在步骤18 的内存里合并，界面看不到）。
        #   合并规则：交付值为空/未明确 且 视觉补充有值 → 取视觉值。
        undef_map: dict[str, int] = {}
        empty_map: dict[str, int] = {}
        deliv_map: dict[str, dict[str, int]] = {}
        _vision_fill: dict[tuple[int, str], str] = {}
        _vp = cat_root_dir(cat, data_root) / "17_属性标注" / "视觉补充.json"
        if _vp.is_file():
            try:
                _vd = json.loads(_vp.read_text(encoding="utf-8-sig"))
                for _k, _v in (_vd.get("filled") or {}).items():
                    _rid_s, _, _a = str(_k).partition("|")
                    try:
                        _rid = int(_rid_s)
                    except (TypeError, ValueError):
                        continue
                    _vv = str(_v or "").strip()
                    if _a and _vv:
                        _vision_fill[(_rid, _a)] = _vv
            except Exception:  # noqa: BLE001
                _vision_fill = {}
        stab_p = cat_root_dir(cat, data_root) / "17_属性标注" / "稳定性结果.json"
        if stab_p.is_file():
            try:
                _sd = json.loads(stab_p.read_text(encoding="utf-8-sig"))
                for _d in (_sd.get("deliverable") or []):
                    try:
                        _rid = int(_d.get("xl_row"))
                    except (TypeError, ValueError):
                        _rid = None
                    for _k, _v in ((_d.get("attributes") or {})).items():
                        if _k.endswith("_来源") or _k.endswith("_依据"):
                            continue
                        _sv = str(_v or "").strip()
                        if not _sv or _sv == "未明确":
                            _fill = _vision_fill.get((_rid, _k)) if _rid is not None else None
                            if _fill:
                                _sv = _fill
                        if _sv == "未明确":
                            undef_map[_k] = undef_map.get(_k, 0) + 1
                        elif not _sv:
                            empty_map[_k] = empty_map.get(_k, 0) + 1
                        else:
                            _m = deliv_map.setdefault(_k, {})
                            _m[_sv] = _m.get(_sv, 0) + 1
            except Exception:  # noqa: BLE001
                undef_map, empty_map, deliv_map = {}, {}, {}
        # ★ 多轮历史: 每属性的历史轮次(供 UI 显示「第 N 轮」与历史建议)
        hist = self._load_std_vocab_history(self._std_vocab_history_path(cat, data_root))
        attrs = []
        for attr, vals in stats.items():
            cur = attr_map.get(attr, {})
            rounds = hist.get(attr) or []
            # ★ 2026-10-27: 取**最完整的那一轮对话**（各轮「对话」是累积的，最多的一轮即全量）
            _best_hist = []
            for _r in rounds:
                _d = _r.get("对话") or []
                if len(_d) > len(_best_hist):
                    _best_hist = _d
            _hist_msgs = [{"角色": str(m.get("角色") or ""), "内容": str(m.get("内容") or "")}
                          for m in _best_hist if isinstance(m, dict)]
            # 老轮次没有 对话 字段 → 用「口径建议 + 回复」补
            if not _hist_msgs:
                for _r in rounds:
                    _hint = str(_r.get("口径建议") or "").strip()
                    _reply = str(_r.get("回复") or "").strip()
                    if _hint:
                        _hist_msgs.append({"角色": "用户", "内容": _hint})
                    if _reply:
                        _hist_msgs.append({"角色": "AI", "内容": _reply})
            # ★ 2026-10-28: 把「未明确」并进唯一值（待重判行数），否则用户看不到
            if undef_map.get(attr):
                vals = list(vals) + [{"值": "未明确", "次数": undef_map[attr]}]
            # ★ 2026-10-27 用户口径: **已从词表里删除的值，不再出现在唯一值里**
            #   （规范值非空时才过滤；数值类没有词表，保持原样展示）
            _spec_now = set(str(x).strip() for x in (cur.get("规范值") or []) if str(x).strip())
            if _spec_now:
                _merged_ok = set()
                for _m in (report_map.get(attr) or []):
                    if not isinstance(_m, dict):
                        continue
                    _in = str(_m.get("归入") or "").strip()
                    if _in in _spec_now:
                        _merged_ok.add(str(_m.get("原值") or "").strip())
                _vals_shown = [x for x in vals
                               if str(x.get("值") or "").strip() == "未明确"
                               or str(x.get("值") or "").strip() in _spec_now
                               or str(x.get("值") or "").strip() in _merged_ok]
            else:
                _vals_shown = vals
            attrs.append({"属性": attr, "唯一值": _vals_shown,
                          "规范值": cur.get("规范值") or [],
                          "口径建议": cur.get("口径建议", ""),
                          "归并": report_map.get(attr) or [],
                          # ★ 2026-10-28: 两遍平票(空) 的行数 —— 供「重判」勾选
                          "空值数": empty_map.get(attr, 0),
                          "未明确数": undef_map.get(attr, 0),
                          # ★ 2026-10-28: 交付里存在、但不在规范值里的值 → 步骤18 会整行剔除
                          "词表外": [{"值": k, "次数": n}
                                     for k, n in sorted(deliv_map.get(attr, {}).items(),
                                                        key=lambda kv: -kv[1])
                                     if k not in _spec_now],
                          # ★ 2026-10-28: 每个值的实际行数（chips 只显示还有行的值）
                          "分布": dict(deliv_map.get(attr, {})),
                          "轮次": len(rounds),
                          "历史": [str(r.get("口径建议") or "") for r in rounds
                                   if str(r.get("口径建议") or "").strip()],
                          "历史对话": _hist_msgs})
        return {"ok": True, "cat": cat, "attributes": attrs}

    def _api_std_vocab(self, q: dict) -> dict:
        cat, droot = self._std_vocab_cat(q.get("file", [None])[0])
        return self._std_vocab_rows(cat, droot)

    def _api_std_vocab_regenerate(self, body: dict) -> dict:
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        attr = str(body.get("属性") or "").strip()
        if not attr:
            raise ValueError("缺少 属性 名")
        # 归并口径改成**人工自然语言建议**（数字粒度已删，2026-10-08 用户口径）
        hint = str(body.get("口径建议") or "").strip()
        rows = self._std_vocab_rows(cat, droot)
        vals = next((r["唯一值"] for r in rows["attributes"] if r["属性"] == attr), None)
        if not vals:
            raise ValueError(f"属性「{attr}」无唯一值统计(请先运行步骤17)")
        values = [(str(v.get("值")), int(v.get("次数", 1))) for v in vals]
        # ★ 多轮迭代: 读该属性历史轮次，L1(全部口径建议) + L2(最近 5 轮完整结果) 拼进 prompt
        hist_path = self._std_vocab_history_path(cat, droot)
        hist = self._load_std_vocab_history(hist_path)
        rounds = list(hist.get(attr) or [])
        from core.config import Config as _Config
        from core.config import apply_env_overrides
        from core.llm import LLMClient
        from tools.attr_stability.onboard import (
            build_vocab_cluster_prompt, parse_vocab_cluster, parse_vocab_cluster_map,
        )
        yaml_vals = load_yaml_file(safe_config_path(str(body.get("config_file") or "config.yaml")))
        # ★ 2026-10-22 修 bug: 必须走 `apply_env_overrides` —— 密钥(LLM_API_KEY / VISION_API_KEY)
        #   通常只存在**环境变量**里, 配置文件里没有。原来直接 `_Config(yaml_vals)` →
        #   客户端拿不到密钥 → 报 "LLM 密钥未配置" → 「按建议重新生成」点了一直失败(表现为"没反应")。
        llm = _meter_legacy_vocab_client(LLMClient(_Config(apply_env_overrides(yaml_vals))), cat_root_dir(cat, droot), 18)
        raw = llm.chat_json(build_vocab_cluster_prompt(attr, values, hint, history=rounds),
                            temperature=0.1, max_tokens=8000)
        spec_vals = parse_vocab_cluster(raw, attr)
        merged = parse_vocab_cluster_map(raw, attr)
        # 追加本轮（标「未保存」；点「保存词表」后才置已保存）
        rounds.append({
            "轮次": len(rounds) + 1,
            "时间": datetime.now().isoformat(timespec="seconds"),
            "口径建议": hint,
            "规范值": spec_vals,
            "归并": merged,
            "已保存": False,
        })
        hist[attr] = rounds
        self._save_std_vocab_history(hist_path, hist)
        return {"ok": True, "属性": attr, "口径建议": hint,
                "规范值": spec_vals, "归并": merged, "轮次": len(rounds)}

    def _api_std_vocab_chat(self, body: dict) -> dict:
        """对话式词表收敛（2026-10-22）。

        与 `/regenerate` 的区别：那个是"一次性写口径建议 → 出结果"；
        这个是**多轮问答**——用户在弹窗里说一句、AI 回一段解释 + 更新后的词表，
        可以继续追问（"为什么把 X 并进去了""把 Y 拆开"）。

        入参:
          config_file / 属性 / 消息 / 对话([{角色,内容}]) / 规范值(可选中继当前状态)
        出参:
          {ok, 回复, 规范值, 归并, 轮次}
        """
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        attr = str(body.get("属性") or "").strip()
        msg = str(body.get("消息") or "").strip()
        if not attr:
            raise ValueError("缺少 属性 名")
        if not msg:
            raise ValueError("消息不能为空")
        rows = self._std_vocab_rows(cat, droot)
        vals = next((r["唯一值"] for r in rows["attributes"] if r["属性"] == attr), None)
        if not vals:
            raise ValueError("属性「%s」无唯一值统计(请先运行步骤17)" % attr)
        values = [(str(v.get("值")), int(v.get("次数", 1))) for v in vals]
        cur_spec = [str(v).strip() for v in (body.get("规范值") or []) if str(v).strip()]
        convo = [m for m in (body.get("对话") or []) if isinstance(m, dict)]
        # 当前这条消息并入对话（前端若已放进去也别重复）
        if not (convo and str(convo[-1].get("内容") or "").strip() == msg):
            convo = convo + [{"角色": "用户", "内容": msg}]

        hist_path = self._std_vocab_history_path(cat, droot)
        hist = self._load_std_vocab_history(hist_path)
        rounds = list(hist.get(attr) or [])

        from core.config import Config as _Config
        from core.config import apply_env_overrides
        from core.llm import LLMClient
        from tools.attr_stability.onboard import (
            build_vocab_chat_prompt, parse_vocab_cluster, parse_vocab_cluster_map,
        )
        # ★ 2026-10-27 用户口径: 对话里也带「其他属性的词表」和「原始商品资料样本」
        other_attrs = {}
        for _r in (rows.get("attributes") or []):
            _nm = _r.get("属性")
            if _nm and _nm != attr:
                other_attrs[_nm] = list(_r.get("规范值") or [])[:8]
        sample_rows = []
        try:
            _root = cat_root_dir(cat, droot)
            _sp = os.path.join(str(_root), "17_属性标注", "input.json")
            with open(_sp, encoding="utf-8-sig") as _fh:
                _inp = json.load(_fh)
            for _r in (_inp.get("rows") or [])[:3]:
                # 用户口径: 不截断，原文全传
                sample_rows.append({k: str(_r.get(k) or "")
                                    for k in ("标题", "五点描述", "详细参数")})
        except Exception:  # noqa: BLE001 — 样本缺失不影响对话
            sample_rows = []
        # ★ 2026-10-27: 当前**交付值**分布（含「未明确」）。
        #   唯一值统计不含未明确，AI 会误判「没有可重判的行」。
        deliv_dist = {}
        try:
            _sd = json.loads((cat_root_dir(cat, droot) / "17_属性标注" / "稳定性结果.json")
                             .read_text(encoding="utf-8-sig"))
            for _d in (_sd.get("deliverable") or []):
                _v = str(((_d.get("attributes") or {}).get(attr)) or "").strip() or "(空)"
                deliv_dist[_v] = deliv_dist.get(_v, 0) + 1
        except Exception:  # noqa: BLE001 — 没有交付结果时跳过
            deliv_dist = {}
        yaml_vals = load_yaml_file(safe_config_path(str(body.get("config_file") or "config.yaml")))
        llm = _meter_legacy_vocab_client(LLMClient(_Config(apply_env_overrides(yaml_vals))), cat_root_dir(cat, droot), 18)
        raw = llm.chat_json(build_vocab_chat_prompt(
            attr, values, convo, cur_spec, rounds,
            other_attrs=other_attrs, sample_rows=sample_rows, deliv_dist=deliv_dist),
            temperature=0.2, max_tokens=8000)
        reply = str((raw or {}).get("回复") or "").strip()
        spec_vals = parse_vocab_cluster(raw, attr)
        merged = parse_vocab_cluster_map(raw, attr)
        # ★ 2026-10-27 用户口径: 动作 / 读取方式 / 筛选值 都由 AI 自己判断
        _action = str((raw or {}).get("动作") or "").strip().lower()
        _action = _action if _action in ("converge", "reannotate") else "converge"
        _read = str((raw or {}).get("读取方式") or "").strip().lower()
        _read = _read if _read in ("text", "vision", "both") else "text"
        _flt = str((raw or {}).get("筛选值") or "").strip()
        # ★ 2026-10-27: 跨属性/多属性——AI 认为几个属性要一起改时放进 多属性
        _multi = {}
        _m = (raw or {}).get("多属性")
        if isinstance(_m, dict):
            for _k, _v in _m.items():
                _k = str(_k or "").strip()
                if not _k or _k == attr:
                    continue
                _vs = ([str(x).strip() for x in (_v or []) if str(x).strip()]
                       if isinstance(_v, list) else [])
                if _vs:
                    _multi[_k] = _vs
        if not reply:
            reply = "已按你的要求调整，共 %d 个规范值。" % len(spec_vals)
        # 落一轮历史（对话式也记，便于回看）
        rounds.append({
            "轮次": len(rounds) + 1,
            "时间": datetime.now().isoformat(timespec="seconds"),
            "口径建议": msg,
            "回复": reply,
            # ★ 2026-10-27: 必须把**本轮 AI 回复**也写进 对话，否则回看只剩用户提问
            "对话": (convo + [{"角色": "AI", "内容": reply}])[-200:],
            "规范值": spec_vals,
            "归并": merged,
            "动作": _action, "读取方式": _read, "筛选值": _flt, "多属性": _multi,
            "已保存": False,
        })
        hist[attr] = rounds
        self._save_std_vocab_history(hist_path, hist)
        return {"ok": True, "属性": attr, "回复": reply,
                "规范值": spec_vals, "归并": merged, "轮次": len(rounds),
                "动作": _action, "读取方式": _read, "筛选值": _flt, "多属性": _multi}

    def _api_std_vocab_chat_history(self, body: dict) -> dict:
        """把前端当前对话（含重判过程消息）落盘到该属性最后一轮，供下次回看。"""
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        attr = str(body.get("属性") or "").strip()
        if not attr:
            raise ValueError("缺少 属性")
        convo = body.get("对话")
        if not isinstance(convo, list):
            raise ValueError("对话 必须是消息列表")
        msgs = []
        for m in convo[-200:]:
            if not isinstance(m, dict):
                continue
            role = "用户" if str(m.get("角色") or "") == "用户" else "AI"
            content = str(m.get("内容") or "")
            if content.strip():
                msgs.append({"角色": role, "内容": content})
        hist_path = self._std_vocab_history_path(cat, droot)
        hist = self._load_std_vocab_history(hist_path)
        rounds = hist.get(attr) or []
        if not rounds:
            rounds.append({"轮次": 1, "时间": datetime.now().isoformat(timespec="seconds"),
                           "口径建议": "", "回复": "", "对话": [], "规范值": [], "归并": []})
        rounds[-1]["对话"] = msgs
        hist[attr] = rounds
        self._save_std_vocab_history(hist_path, hist)
        return {"ok": True, "属性": attr, "消息数": len(msgs), "轮次": len(rounds)}

    def _api_std_vocab_save(self, body: dict) -> dict:
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        attrs = body.get("attributes")
        if not isinstance(attrs, list) or any(not isinstance(it, dict) for it in attrs):
            raise ValueError("attributes 必须是属性对象列表")
        rp = self._std_rules_path(cat, droot, writable=True)
        if rp is None:
            raise ValueError("找不到 rules.json(*.rules.json)")
        rules = json.loads(rp.read_text(encoding="utf-8-sig"))
        # ★ 2026-10-28 用户口径: 把「同义归并」写进 params.aliases ——
        #   步骤18 的机械收敛读 spec['aliases']，这样对话里定的 A→B 才真正生效；
        #   否则归并映射只停在界面上，标准化时还得让 LLM 重新猜一遍。
        _alias_report: dict[str, dict] = {}
        try:
            _rpp = cat_root_dir(cat, droot) / "18_标准化" / "词表聚类报告.json"
            if _rpp.is_file():
                for _a in (json.loads(_rpp.read_text(encoding="utf-8-sig"))
                           .get("attributes") or []):
                    if not isinstance(_a, dict) or not _a.get("属性"):
                        continue
                    _m = {}
                    for _x in (_a.get("归并") or []):
                        if isinstance(_x, dict):
                            _o = str(_x.get("原值") or "").strip()
                            _t = str(_x.get("归入") or "").strip()
                            if _o and _t:
                                _m[_o] = _t
                    _alias_report[str(_a["属性"])] = _m
        except Exception:  # noqa: BLE001
            _alias_report = {}
        _alias_chat: dict[str, tuple] = {}
        try:
            _hh = self._load_std_vocab_history(
                self._std_vocab_history_path(cat, droot))
            for _nm, _rs in (_hh or {}).items():
                if not isinstance(_rs, list) or not _rs:
                    continue
                _last = _rs[-1] if isinstance(_rs[-1], dict) else {}
                _m = {}
                for _x in (_last.get("归并") or []):
                    if isinstance(_x, dict):
                        _o = str(_x.get("原值") or "").strip()
                        _t = str(_x.get("归入") or "").strip()
                        if _o and _t:
                            _m[_o] = _t
                if _m:
                    # 只有「AI 提议的规范值 == 用户这次保存的规范值」才认为归并被采纳，
                    # 否则那段归并可能来自被否掉的旧提议，套上去会改错数据。
                    _spec = {str(v).strip() for v in (_last.get("规范值") or [])
                             if str(v).strip()}
                    _alias_chat[_nm] = (_m, _spec)
        except Exception:  # noqa: BLE001
            _alias_chat = {}
        # ★ 2026-10-28 用户口径: 词表收敛也存快照，支持回滚
        _vocab_snap = {}
        for _it in attrs:
            _nm = str(_it.get("属性") or "").strip()
            if not _nm:
                continue
            for _a in (rules.get("attributes") or []):
                if str(_a.get("name") or "") == _nm:
                    _p = _a.get("params") if isinstance(_a.get("params"), dict) else {}
                    _vocab_snap[_nm] = list(_p.get("values") or _a.get("values") or [])
                    break
        for it in attrs:
            name = str(it.get("属性") or "").strip()
            # ★ 去重（多值串拆开后可能产生重复：如「微波加热、烤箱烘烤、日晒」拆出 3 个单值）
            _seen = set()
            vals = []
            for v in (it.get("规范值") or []):
                sv = str(v).strip()
                if sv and sv not in _seen:
                    _seen.add(sv)
                    vals.append(sv)
            hint = str(it.get("口径建议") or "").strip()
            for a in (rules.get("attributes") or []):
                if str(a.get("name") or "") != name:
                    continue
                params = a.get("params") if isinstance(a.get("params"), dict) else None
                # ★ 2026-10-28: 归并 → params.aliases（供步骤18 机械收敛直接套用）
                _al = {}
                _chat_item = _alias_chat.get(name)
                _chat_map = {}
                if isinstance(_chat_item, tuple) and len(_chat_item) == 2:
                    _cm, _cs = _chat_item
                    if _cs and _cs == set(vals):      # AI 提议的规范值被原样采纳
                        _chat_map = _cm
                _val_set = set(vals)
                _is_yesno = _val_set == {"是", "否"}
                for _src in (_alias_report.get(name) or {}, _chat_map):
                    for _o, _t in _src.items():
                        if not _o or _o == _t or _t not in _val_set:
                            continue
                        # ★ 多值/组合值一律不写别名（按成分查表，整串匹配不上；
                        #   成分映射成组合串会拼出嵌套垃圾值）。
                        if any(_c in _o for _c in _MULTI_SEPS) or \
                                any(_c in _t for _c in _MULTI_SEPS):
                            continue
                        # ★ 哨兵不能当别名源（`未明确 → 某值` 会把所有未明确行硬改成该值）
                        if _o in ("未明确", "未知", "不适用", "待确认", "无", ""):
                            continue
                        # ★ 原值本身就是合法值时不写别名：收敛器先查词表，
                        #   别名永远轮不到，写了只会误导人。
                        if _o in _val_set:
                            continue
                        # ★ 是/否 类不允许互转（语义反转，改错代价极高）
                        if _is_yesno:
                            continue
                        # ★ 环形映射（A→B 且 B→A）会让值来回翻，直接跳过
                        if (_src.get(_t) or "") == _o:
                            continue
                        _al[_o] = _t
                if params is not None:
                    params["values"] = vals
                    params.pop("excluded", None)
                    if _al:
                        params["aliases"] = _al
                    else:
                        params.pop("aliases", None)
                    if hint:
                        params["vocab_hint"] = hint
                elif "values" in a:
                    a["values"] = vals
                    if hint:
                        a["vocab_hint"] = hint
        # ★★ 「保存即确认」（2026-10-09，方案 A 的「停②」）
        #   链路：16 提取属性 →[停①]确认属性集 → 17 标注+产词表 →[停②]确认词表 → 18 应用词表
        #   步骤17 结束时把 `vocab_status` 置为 pending_confirm，步骤18 会拒绝运行；
        #   人工在这里**调整并保存**，就表示"这版词表我审过了" → 置为 confirmed。
        #   为什么用"保存即确认"而不新加一个按钮：保存这个动作本身语义就是"我改完了、认可了"，
        #   再加一步确认只是多一次点击，还得处理"改了没确认"的中间态。
        #
        # ★ 确认范围 = **全部属性**（用户口径 2026-10-09）：
        #   状态是 rules.json 上的**一个全局标记**，不是逐属性确认 ——
        #   所以"审过了"覆盖**整份词表**，包括本次没在 `body.attributes` 里出现的属性。
        #   这也意味着：请求体只带变动的属性也照样算全部确认（这是刻意的，不是漏洞）。
        rules["vocab_status"] = "confirmed"
        # ★ 2026-10-28 用户口径: 每次词表改动都留**文件级备份**（可按时间点恢复历史版本）
        try:
            _stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            _bak = rp.with_name(rp.name + ".bak-" + _stamp)
            _bak.write_bytes(rp.read_bytes())
            _olds = sorted(rp.parent.glob(rp.name + ".bak-*"))
            for _o in _olds[:-20]:
                try:
                    _o.unlink()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 — 备份失败不影响保存
            pass
        _total = len([a for a in (rules.get("attributes") or [])
                      if (a.get("params") or {}).get("values") or a.get("values")])
        rp.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
        # ★ 2026-10-28: 词表收敛也写进操作历史（类型=vocab，可回滚）
        try:
            _hp = cat_root_dir(cat, droot) / "17_属性标注" / "重判历史.json"
            _hist = (json.loads(_hp.read_text(encoding="utf-8-sig"))
                     if _hp.exists() else {}) or {}
            _t = datetime.now().isoformat(timespec="seconds")
            for _nm, _vals in _vocab_snap.items():
                _hist.setdefault(_nm, []).append({
                    "类型": "vocab", "时间": _t, "要求": "保存词表",
                    "原规范值": list(_vals),
                })
            _hp.parent.mkdir(parents=True, exist_ok=True)
            _hp.write_text(json.dumps(_hist, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        except Exception:  # noqa: BLE001 — 快照失败不影响保存
            pass
        # ★ 多轮历史: 把该属性最新一轮标「已保存」，并把保存的规范值同步进历史(保留 regenerate 的归并)
        hist_path = self._std_vocab_history_path(cat, droot)
        hist = self._load_std_vocab_history(hist_path)
        for it in attrs:
            name = str(it.get("属性") or "").strip()
            vals = [str(v).strip() for v in (it.get("规范值") or []) if str(v).strip()]
            hint = str(it.get("口径建议") or "").strip()
            rounds = list(hist.get(name) or [])
            if rounds:
                rounds[-1] = dict(rounds[-1], 规范值=vals, 已保存=True)
                if hint:
                    rounds[-1]["口径建议"] = hint
            else:
                rounds.append({"轮次": 1, "时间": datetime.now().isoformat(timespec="seconds"),
                               "口径建议": hint, "规范值": vals, "归并": [], "已保存": True})
            hist[name] = rounds
        self._save_std_vocab_history(hist_path, hist)
        push_cat(cat, "标准化词表已保存并确认（**全部 %d 个属性**，可运行步骤18）" % _total)
        return {"ok": True, "cat": cat, "saved": len(attrs),
                "vocab_status": "confirmed", "覆盖属性数": _total}

    def _api_std_vocab_prompts(self, body: dict) -> dict:
        """一键为**每个属性**生成重判提示词（字段说明 + 用户要求 + 唯一值 + 格式 + 样本行）

        用户口径 2026-10-27: 想先看看每个属性的提示词长什么样，再决定跑不跑。
        """
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        root = cat_root_dir(cat, droot)
        req = str(body.get("requirement") or "").strip()
        uniq_map = {}
        try:
            _ud = json.loads((root / "17_属性标注" / "唯一值统计.json")
                            .read_text(encoding="utf-8-sig"))
            uniq_map = _ud.get("属性唯一值") or {}
        except Exception:  # noqa: BLE001
            uniq_map = {}
        hint_map = {}
        try:
            _spec = json.loads((root / ("%s-attribute-spec.json" % root.name))
                              .read_text(encoding="utf-8-sig"))
            for _a in (_spec.get("attributes") or []):
                hint_map[str(_a.get("name") or "")] = str(
                    _a.get("hint") or _a.get("hint_short") or "")
        except Exception:  # noqa: BLE001
            hint_map = {}
        sample = []
        try:
            _rows = json.loads((root / "17_属性标注" / "input.json")
                              .read_text(encoding="utf-8-sig")).get("rows") or []
            sample = _rows[:3]
        except Exception:  # noqa: BLE001
            sample = []
        want = [str(x) for x in (body.get("attrs") or []) if str(x).strip()]
        out = []
        for attr in (want or sorted(uniq_map)):
            uniq = [str(x.get("值") or "").strip()
                    for x in (uniq_map.get(attr) or [])
                    if str(x.get("值") or "").strip()]
            # ★ 用户口径: 预览**不要商品资料**（只给规则 + 格式）
            out.append({
                "属性": attr,
                "提示词": self._reannot_prompt(
                    attr, hint_map.get(attr, ""), uniq, req, [], with_rows=False),
            })
        return {"ok": True, "cat": cat, "prompts": out, "样本行数": len(sample)}

    def _api_std_vocab_reannotate(self, body: dict) -> dict:
        """按当前词表**逐行重新判定**某个属性（读 标题/五点/详细参数），写回 稳定性结果.json。

        用户口径 2026-10-27: 在 Web「对话」里让 AI 校验并重跑标注。
          · 分批（12 行/批）→ 不超 token；
          · 每行带 xl_row → 精确对应回原表；
          · 只改该属性的 值/来源/依据，其它属性不动；
          · 写回 deliverable + cells + 唯一值统计.json。
        """
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        attr = str(body.get("属性") or "").strip()
        if not attr:
            raise ValueError("缺少 属性")
        root = cat_root_dir(cat, droot)
        rp = self._std_rules_path(cat, droot, writable=True)
        if rp is None:
            raise ValueError("找不到 rules.json")
        rules = json.loads(rp.read_text(encoding="utf-8-sig"))
        # ★ 2026-10-27: 重判提示词不再依赖词表（词表只当参考/可空）——
        #   所以不再因为「属性没有词表」而拒绝重判。
        vocab = []
        for a in (rules.get("attributes") or []):
            if str(a.get("name") or "") == attr:
                vocab = [str(v).strip() for v in ((a.get("params") or {}).get("values") or [])]
                break
        inp_p = root / "17_属性标注" / "input.json"
        stab_p = root / "17_属性标注" / "稳定性结果.json"
        if not inp_p.exists() or not stab_p.exists():
            raise ValueError("缺少 17_属性标注/input.json 或 稳定性结果.json")
        rows = json.loads(inp_p.read_text(encoding="utf-8-sig")).get("rows") or []
        stab = json.loads(stab_p.read_text(encoding="utf-8-sig"))
        deliv = {d.get("xl_row"): d for d in (stab.get("deliverable") or [])}
        todo = [r for r in rows if r.get("xl_row") in deliv]
        # ★ 只重判满足「该属性 = 某值」的行；**支持多个值**（用 , ; 、 / | 分隔）
        _filt_raw = str(body.get("filter_value") or "").strip()
        _filt_set = {x.strip() for x in re.split(r"[,;、/|]+", _filt_raw) if x.strip()} \
            if _filt_raw else set()
        # ★ 2026-10-28: 「(空)」这类写法指向**空值行**（两遍平票/未判定）
        _EMPTY_TOKENS = {"(空)", "（空）", "空", "空值", "未判定", "<空>", "空白"}
        _filt_set = {"" if x in _EMPTY_TOKENS else x for x in _filt_set}
        if _filt_set:
            todo = [r for r in todo
                    if str(((deliv.get(r.get("xl_row")) or {}).get("attributes") or {})
                           .get(attr) or "").strip() in _filt_set]
            if not todo:
                raise ValueError("没有「%s ∈ {%s}」的行可重判"
                                 % (attr, "、".join(sorted(_filt_set))))
        if not todo:
            raise ValueError("没有可复核的行")

        # ★ 判据用**该属性自己的判定说明(hint)**，不写死「技术/原理」这种维度词。
        _hint = ""
        try:
            _sp = root / ("%s-attribute-spec.json" % root.name)
            if _sp.exists():
                _spec = json.loads(_sp.read_text(encoding="utf-8-sig"))
                for _a in (_spec.get("attributes") or []):
                    if str(_a.get("name") or "") == attr:
                        _hint = str(_a.get("hint") or _a.get("hint_short") or "").strip()
                        break
        except Exception:  # noqa: BLE001 — hint 缺失不影响重判
            _hint = ""
        # 唯一值（只作参考，**不是限定范围**）与用户这次的要求
        _uniq = []
        try:
            _up = root / "17_属性标注" / "唯一值统计.json"
            if _up.exists():
                _ud = json.loads(_up.read_text(encoding="utf-8-sig"))
                _uniq = [str(x.get("值") or "").strip()
                         for x in ((_ud.get("属性唯一值") or {}).get(attr) or [])
                         if str(x.get("值") or "").strip()]
                # ★ 已从词表删掉的值不再当参考（规范值非空时过滤）
                if vocab:
                    _spec_now = set(vocab)
                    _uniq = [v for v in _uniq if v in _spec_now]
        except Exception:  # noqa: BLE001
            _uniq = []
        _req = str(body.get("requirement") or "").strip()
        # ★ 2026-10-27 用户口径: 只重判「该属性 = 某个值」的那部分行
        #   例：材质=金属 → 只想进一步细分到底是什么金属
        if _filt_set:
            _req = ("这些行的「" + attr + "」目前标成「"
                    + "、".join(sorted(_filt_set)) + "」。"
                    "请**逐行重新判定**：判断每行到底属于哪个更具体的值"
                    "（例：金属 → 不锈钢 / 铝合金 / 铁 / 铜…，必须依据原文证据）；"
                    "上述几个取值**都要处理**，不要只处理其中一个。\n"
                    + _req)

        from core.config import Config as _Config
        from core.config import apply_env_overrides
        from core.llm import LLMClient
        yaml_vals = load_yaml_file(safe_config_path(str(body.get("config_file") or "config.yaml")))
        llm = _meter_legacy_vocab_client(LLMClient(_Config(apply_env_overrides(yaml_vals))), root, 17)

        # ★ 2026-10-27 用户口径: 重判方式可选「文字 / 图片 / 文字+图片」
        mode = str(body.get("mode") or "text").strip().lower()
        if mode not in ("text", "vision", "both"):
            mode = "text"
        BATCH = 12
        updated: dict = {}
        # 无依据被降级为「未明确」的行（用于日志告知用户）
        _downgrade: list = []

        def _img_urls(row):
            import re as _re
            return _re.findall(r"https?://[^\s,;|\"']+",
                               str(row.get("图片链接") or row.get("主图") or ""))

        # ★ 用户口径 2026-10-27: 不再把词表外的值压成「未明确」——由 LLM 自己判
        def _norm_val(v):
            return str(v or "").strip()

        # ★ 2026-10-27 用户口径: 每次重判**前**先快照该属性的原值，
        #   支持多轮重判 + 随时回滚（历史存 17_属性标注/重判历史.json）
        _hp = root / "17_属性标注" / "重判历史.json"
        try:
            _hist = json.loads(_hp.read_text(encoding="utf-8-sig")) or {}
        except Exception:  # noqa: BLE001
            _hist = {}
        _prev = {}
        for _d in (stab.get("deliverable") or []):
            try:
                _rid = int(_d.get("xl_row"))
            except (TypeError, ValueError):
                continue
            _prev[str(_rid)] = str(((_d.get("attributes") or {}).get(attr)) or "").strip()
        _opt = str(body.get("操作类型") or "reannotate").strip().lower()
        if _opt not in ("reannotate", "cross"):
            _opt = "reannotate"
        _hist.setdefault(attr, []).append({
            "类型": _opt,
            "时间": datetime.now().isoformat(timespec="seconds"),
            "要求": _req[:300],
            "模式": mode,
            "重判行数": len(todo),
            "原值": _prev,
        })
        _hp.write_text(json.dumps(_hist, ensure_ascii=False, indent=2), encoding="utf-8")

        # —— 文字（批量）——
        if mode in ("text", "both"):
            _ntext = (len(todo) + BATCH - 1) // BATCH
            for i in range(0, len(todo), BATCH):
                part = todo[i:i + BATCH]
                push_cat(cat, "重判进度（文字）%d/%d 批" % (i // BATCH + 1, _ntext))
                # 统一走 _reannot_prompt：字段说明 + 用户要求 + 唯一值参考 + 格式 + 资料
                prompt = self._reannot_prompt(attr, _hint, _uniq, _req, part)
                try:
                    raw = llm.chat_json(prompt, temperature=0.0, max_tokens=4000)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError("重新标注第 %d 批失败: %s" % (i // BATCH + 1, exc))
                _row_by = {int(x["xl_row"]): x for x in part if x.get("xl_row") is not None}
                for it in ((raw.get("rows") if isinstance(raw, dict) else None) or []):
                    try:
                        rid = int(it.get("xl_row"))
                    except (TypeError, ValueError):
                        continue
                    _src_row = _row_by.get(rid) or {}
                    # ★ 文字模式：依据必须是**原文里真的出现过**的话（不许"未提及/推测"式推断）
                    _texts = {
                        "标题": _src_row.get("标题") or "",
                        "五点描述": _src_row.get("五点描述") or "",
                        "详细参数": _src_row.get("详细参数") or "",
                    }
                    updated[rid] = _gate_basis(
                        _norm_val(it.get("值")),
                        it.get("依据"), it.get("来源"), _downgrade, _texts)

        # —— 图片（视觉，逐行；both 模式只补「文字判不出」的行）——
        if mode in ("vision", "both"):
            if mode == "vision":
                _need = list(todo)
            else:
                _need = [r for r in todo
                         if _norm_val((updated.get(r.get("xl_row")) or {}).get("值"))
                         in ("未明确", "")]
            _nv = len(_need)
            if _nv:
                push_cat(cat, "视觉重判开始：共 %d 行（每行的**全部图片**，逐行调用，较慢）" % _nv)
            for _vi, r in enumerate(_need, 1):
                if _vi == 1 or _vi % 5 == 0 or _vi == _nv:
                    push_cat(cat, "重判进度（图片）%d/%d 行" % (_vi, _nv))
                us = _img_urls(r)
                if not us:
                    continue
                # 与文字模式同一套判据（字段说明 + 用户要求 + 唯一值参考），只是输入换成图
                prompt = (
                    "这是商品图，请只看图判断属性「" + attr + "」的值。\n"
                    + _reannot_rule(_hint, _uniq, _req)
                    + "严格输出 JSON：{\"值\":\"...\",\"依据\":\"<看图依据,≤30字>\","
                      "\"来源\":\"图片视觉\"}"
                )
                try:
                    # ★ 2026-10-27 用户口径: **每张图都要看**，不再只取前 3 张
                    # ★ 2026-10-28: 默认 high —— low 会把图压成缩略图，
                    #   材质/轮子/水箱形态这类细节根本看不清，模型只能回「未明确」。
                    _detail = str(body.get("图片细节") or "high").strip().lower()
                    if _detail not in ("low", "high", "auto"):
                        _detail = "high"
                    # 2026-10-29: 400 时按 全部→4→2→1 张降级重试（个别图会让整单被拒）
                    _out = None
                    _last_exc = None
                    for _tier in (us, us[:4], us[:2], us[:1]):
                        if not _tier:
                            break
                        try:
                            _out = llm.vision(prompt, _tier, temperature=0.0,
                                              max_tokens=1000, detail=_detail,
                                              max_images=None)
                            break
                        except Exception as _ve:  # noqa: BLE001
                            _m = str(_ve)
                            if "400" not in _m and "Bad Request" not in _m:
                                raise
                            _last_exc = _ve
                    if _out is None:
                        raise _last_exc or RuntimeError("vision failed")
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(_out, dict):
                    continue
                _val = _norm_val(_out.get("值"))
                if _val:
                    updated[r.get("xl_row")] = _gate_basis(
                        _val, _out.get("依据"), "图片视觉", _downgrade)


        changed = []
        for rid, res in updated.items():
            d = deliv.get(rid)
            if not d:
                continue
            at = d.setdefault("attributes", {})
            old = str(at.get(attr) or "").strip()
            if old != res["值"]:
                changed.append((rid, old, res["值"]))
            at[attr] = res["值"]
            at[attr + "_来源"] = res["来源"]
            at[attr + "_依据"] = res["依据"]
        cells = stab.get("cells") or {}
        _now = _hist[attr][-1].get("时间") or ""
        for rid, res in updated.items():
            _key = "%d|attr.%s" % (rid, attr)
            _old = cells.get(_key) if isinstance(cells.get(_key), dict) else {}
            # ★ 2026-10-28: 保留原有投票记录（a1/a2 的提名），只追加本次重判的提名；
            #   原先这里整条替换，一重判就把两遍标注的证据抹掉了。
            _vals = [it for it in (_old.get("values") or []) if isinstance(it, dict)]
            if not any(str(it.get("value")) == res["值"]
                       and "reannotate" in (it.get("by") or []) for it in _vals):
                _vals = _vals + [{"value": res["值"], "by": ["reannotate"]}]
            _prev_mode = str(_old.get("mode") or "")
            # ★ 2026-10-28 用户口径: 值变了要记录，**原始值必须一直保留**
            _orig = _old.get("原始值")
            if _orig is None:
                _orig = _prev_mode
            _changes = list(_old.get("变更记录") or [])
            if _prev_mode != res["值"]:
                _changes.append({"时间": _now, "原值": _prev_mode, "新值": res["值"]})
            cells[_key] = {
                "mode": res["值"], "agree": True,
                "values": _vals,
                "n_votes": max(int(_old.get("n_votes") or 1), 1),
                "kind": _old.get("kind") or "value",
                "needs_review": False, "no_majority": False,
                "原始值": _orig,
                "变更记录": _changes[-20:],
            }
            # ★ 2026-10-28: 来源/依据也写成 cells（步骤18 的交付表从 cells 投影这两列）
            for _suf, _sv in (("_来源", res.get("来源") or ""),
                              ("_依据", res.get("依据") or "")):
                _sv = str(_sv).strip()
                if not _sv:
                    continue
                _sk = "%d|attr.%s%s" % (rid, attr, _suf)
                _sc = cells.get(_sk) if isinstance(cells.get(_sk), dict) else {}
                _svals = [it for it in (_sc.get("values") or []) if isinstance(it, dict)]
                if not any(str(it.get("value")) == _sv
                           and "reannotate" in (it.get("by") or []) for it in _svals):
                    _svals = _svals + [{"value": _sv, "by": ["reannotate"]}]
                cells[_sk] = {"mode": _sv, "agree": True, "values": _svals,
                              "n_votes": max(int(_sc.get("n_votes") or 1), 1),
                              "kind": _sc.get("kind") or "free",
                              "needs_review": False, "no_majority": False}
        stab["cells"] = cells
        stab_p.write_text(json.dumps(stab, ensure_ascii=False, indent=2), encoding="utf-8")
        # 重算唯一值统计（只改该属性）
        try:
            sp = root / "17_属性标注" / "唯一值统计.json"
            st = json.loads(sp.read_text(encoding="utf-8-sig"))
            import collections
            c = collections.Counter()
            for d in (stab.get("deliverable") or []):
                v = str((d.get("attributes") or {}).get(attr) or "").strip()
                if v and v != "未明确":
                    c[v] += 1
            # ★ 2026-10-27 用户口径: **不要覆盖「原始唯一值」**
            #   重判后的值单独存到 `重判后唯一值`，这样原始/变动后能对比。
            st.setdefault("重判后唯一值", {})[attr] = [
                {"值": k, "次数": n} for k, n in c.most_common()]
            sp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        push_cat(cat, "已重新标注「%s」%d 行（变更 %d 行）"
                 % (attr, len(updated), len(changed)))
        if _downgrade:
            push_cat(cat, "⚠「%s」有 %d 行**没给出依据/来源**，已按口径打回「未明确」"
                     % (attr, len(_downgrade)))
        # ★ 2026-10-27 用户口径: 在对话里展示**值级变动**（原值 → 改成了哪些值）
        _vd: dict = {}
        for _r, _old, _new in changed:
            _k = _old or "(空)"
            _vd.setdefault(_k, {})
            _nk = _new or "(空)"
            _vd[_k][_nk] = _vd[_k].get(_nk, 0) + 1
        _value_diff = []
        for _old in sorted(_vd, key=lambda k: -sum(_vd[k].values())):
            _items = sorted(_vd[_old].items(), key=lambda kv: -kv[1])
            _value_diff.append({
                "原值": _old,
                "新值": [{"值": k, "行数": n} for k, n in _items],
            })
        # ★ 2026-10-28 用户口径: 把「这次改了什么」写回操作历史，回滚页要能看见
        try:
            _hp2 = root / "17_属性标注" / "重判历史.json"
            _h2 = json.loads(_hp2.read_text(encoding="utf-8-sig")) or {}
            _rs = _h2.get(attr) or []
            if _rs:
                _rs[-1]["值变更"] = _value_diff
                _rs[-1]["变更行数"] = len(changed)
                _rs[-1]["复核行数"] = len(updated)
                _hp2.write_text(json.dumps(_h2, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:  # noqa: BLE001 — 记录失败不影响结果
            pass
        return {"ok": True, "cat": cat, "属性": attr, "复核行数": len(updated),
                "变更行数": len(changed),
                "值变更": _value_diff,
                "变更样例": [{"xl_row": r, "原值": o, "新值": n}
                             for r, o, n in changed[:20]]}

    def _api_std_vocab_rollback_history(self, q: dict) -> dict:
        """列出某属性的重判历史（每次重判前的快照），供回滚。"""
        cat, droot = self._std_vocab_cat((q.get("file") or [None])[0])
        attr = str((q.get("attr") or [""])[0]).strip()
        if not attr:
            raise ValueError("缺少 attr")
        root = cat_root_dir(cat, droot)
        hp = root / "17_属性标注" / "重判历史.json"
        try:
            hist = json.loads(hp.read_text(encoding="utf-8-sig")) or {}
        except Exception:  # noqa: BLE001
            hist = {}
        runs = hist.get(attr) or []
        return {"ok": True, "属性": attr, "runs": [
            {"index": _i,
             "类型": str(r.get("类型") or "reannotate"),
             "时间": r.get("时间"), "要求": r.get("要求"),
             "模式": r.get("模式"), "重判行数": r.get("重判行数"),
             "快照行数": len(r.get("原值") or {}),
             "快照规范值": len(r.get("原规范值") or []),
             "值变更": r.get("值变更") or [],
             "变更行数": r.get("变更行数") or 0,
             "复核行数": r.get("复核行数") or 0}
            for _i, r in enumerate(runs)]}

    def _api_std_vocab_rollback(self, body: dict) -> dict:
        """回滚到某次重判**之前**的快照（恢复该属性的行值 + cells）。"""
        cat, droot = self._std_vocab_cat(str(body.get("config_file") or "").strip() or None)
        attr = str(body.get("属性") or "").strip()
        if not attr:
            raise ValueError("缺少 属性")
        root = cat_root_dir(cat, droot)
        hp = root / "17_属性标注" / "重判历史.json"
        if not hp.exists():
            raise ValueError("没有重判历史")
        hist = json.loads(hp.read_text(encoding="utf-8-sig")) or {}
        runs = hist.get(attr) or []
        if not runs:
            raise ValueError("属性「%s」没有重判历史" % attr)
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            idx = len(runs) - 1
        if idx < 0 or idx >= len(runs):
            raise ValueError("index 越界（0~%d）" % (len(runs) - 1))
        snap = runs[idx]
        # ★ 2026-10-28: 类型=vocab → 恢复 rules.json 的规范值；其它类型恢复行值
        if str(snap.get("类型") or "") == "vocab":
            rp = self._std_rules_path(cat, droot, writable=True)
            if rp is None:
                raise ValueError("找不到 rules.json")
            rules = json.loads(rp.read_text(encoding="utf-8-sig"))
            vals = [str(x).strip() for x in (snap.get("原规范值") or [])
                    if str(x).strip()]
            for a in (rules.get("attributes") or []):
                if str(a.get("name") or "") != attr:
                    continue
                if isinstance(a.get("params"), dict):
                    a["params"]["values"] = vals
                else:
                    a["values"] = vals
                break
            rp.write_text(json.dumps(rules, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            push_cat(cat, "已回滚「%s」词表到 %s 的快照（%d 个规范值）"
                     % (attr, snap.get("时间"), len(vals)))
            return {"ok": True, "cat": cat, "属性": attr, "类型": "vocab",
                    "回滚到": snap.get("时间"), "规范值": vals}
        prev = snap.get("原值") or {}
        stab_p = root / "17_属性标注" / "稳定性结果.json"
        stab = json.loads(stab_p.read_text(encoding="utf-8-sig"))
        deliv = {d.get("xl_row"): d for d in (stab.get("deliverable") or [])}
        n = 0
        for rid_s, val in prev.items():
            try:
                rid = int(rid_s)
            except (TypeError, ValueError):
                continue
            d = deliv.get(rid)
            if not d:
                continue
            at = d.setdefault("attributes", {})
            at[attr] = "" if val is None else str(val)
            n += 1
        cells = stab.get("cells") or {}
        for rid_s, val in prev.items():
            try:
                rid = int(rid_s)
            except (TypeError, ValueError):
                continue
            v = "" if val is None else str(val)
            _key = "%d|attr.%s" % (rid, attr)
            _old = cells.get(_key) if isinstance(cells.get(_key), dict) else {}
            # ★ 2026-10-28: 保留原有投票记录，只覆盖 mode；否则回滚一次就把
            #   a1/a2 的提名全抹成 "rollback"，之后再也没法回溯是模型判的还是人改的。
            _vals = [it for it in (_old.get("values") or []) if isinstance(it, dict)]
            if not any(str(it.get("value")) == v and "rollback" in (it.get("by") or [])
                       for it in _vals):
                _vals = _vals + [{"value": v, "by": ["rollback"]}]
            _prev_mode = str(_old.get("mode") or "")
            _orig = _old.get("原始值")
            if _orig is None:
                _orig = _prev_mode
            _changes = list(_old.get("变更记录") or [])
            if _prev_mode != v:
                _changes.append({"时间": snap.get("时间") or "", "原值": _prev_mode,
                                 "新值": v, "来源": "rollback"})
            cells[_key] = {
                "mode": v,
                "agree": bool(_old.get("agree", True)),
                "values": _vals,
                "n_votes": max(int(_old.get("n_votes") or 1), 1),
                "kind": _old.get("kind") or "value",
                "needs_review": False,
                "no_majority": False,
                "原始值": _orig,
                "变更记录": _changes[-20:],
            }
        stab["cells"] = cells
        stab_p.write_text(json.dumps(stab, ensure_ascii=False, indent=2), encoding="utf-8")
        push_cat(cat, "已回滚「%s」到 %s 的快照（%d 行）"
                 % (attr, snap.get("时间"), n))
        return {"ok": True, "cat": cat, "属性": attr, "回滚到": snap.get("时间"),
                "回滚行数": n}

    # ---- 页面
    def _serve_html(self):
        if not HTML_FILE.exists():
            self._send(404, b"index.html not found")
            return
        body = HTML_FILE.read_bytes()
        self._send(200, body, "text/html; charset=utf-8", headers={"X-Frame-Options": "DENY"})

    # ---- 实现: bootstrap/configs/config
    def _api_stop(self, body: dict | None = None) -> dict:
        """中断运行(多类目并行, 2026-09-15)。

        body 可带 `config_file`(前端传当前选中类目) → **只停该类目**;
        不带 → 旧行为兼容: 只有一个在跑就停它, 多个在跑则全停。
        """
        body = body or {}
        cat = ""
        f = str(body.get("config_file") or body.get("file") or "").strip()
        if f:
            try:
                vals = load_yaml_file(safe_config_path(f))
                cat = str(vals.get("CAT", "") or "").strip()
            except Exception:  # noqa: BLE001
                cat = ""
        cat = cat or str(body.get("cat") or "").strip()
        # ★ 2026-11-12: 非 admin 不能"全停"，也不能停未授权类目
        if self._allowed_cats() is not None:
            if not cat:
                raise Forbidden("请指定要中断的类目（当前账号无「全部中断」权限）")
            self._require_run(cat)
        return RUNNER.stop(cat or None)

    def _api_bootstrap(self) -> dict:
        py_ver = sys.version.split()[0]
        # ★ 2026-11-12: 类目清单与运行中类目都按白名单过滤（admin 不过滤）
        allowed = self._allowed_cats()
        cfgs = self._scan_configs()
        if allowed is not None:
            cfgs = [c for c in cfgs if str(c.get("cat") or "").strip() in allowed]
        runs = [r for r in RUNNER.snapshots() if r.get("active")]
        if allowed is not None:
            runs = [r for r in runs if str(r.get("cat") or "").strip() in allowed]
        return {
            "ok": True,
            "project_root": str(PROJECT_ROOT),
            "project_name": PROJECT_ROOT.name,
            "python": sys.executable,
            "python_version": py_ver,
            "webui_version": WEBUI_VERSION,
            "server_time": now_str(),
            "step_count": STEP_COUNT,
            "configs": [c["name"] for c in cfgs],
            # 多类目并行(2026-09-15): 当前正在跑的类目清单(前端显示"并行运行中"并可分别停止)
            "runs": [{"cat": r.get("cat"), "key": r.get("key"), "pid": r.get("pid"),
                      "label": r.get("label"), "started_at": r.get("started_at"),
                      "active": bool(r.get("active")),
                      # ★ 2026-11-12: 谁启动的（admin 看全部类目时能知道是谁在操作）
                      "user": str(r.get("user") or "")}
                     for r in runs],
        }

    def _config_files_cached(self) -> list:
        """文件清单缓存 —— 按**目录结构指纹**判定, 不用时间 TTL。

        2026-10-09 性能实测: `_config_files()` 内部要 list_category_configs() 逐个解析
        16 个类目 config.yaml(取 CAT 判占位), 冷态 ≈1.3s; bootstrap / configs / history
        各调一次 → 首屏白等 1~2 秒(实测 bootstrap 1.4~2.4s)。
        这里先用**便宜的目录列举**(glob + data/ 一级目录名)算结构指纹:
          · 结构没变 → 直接复用上次的 (路径, 显示名) 清单(热态 ≈1~3ms, 与闲置多久无关);
          · 新建/删除/改名类目 → 指纹变化 → 立即重扫(配合写配置入口的显式失效, 不会过期)。
        """
        names = tuple(str(p) for p in sorted(PROJECT_ROOT.glob("*.yaml")))
        cfgdir = PROJECT_ROOT / "configs"
        cnames = tuple(str(p) for p in sorted(cfgdir.glob("*.yaml"))) if cfgdir.is_dir() else ()
        try:
            catdirs = []
            for d in (PROJECT_ROOT / "data").iterdir():
                if not d.is_dir() or d.name == "archive" or d.name.startswith("."):
                    continue
                try:
                    st = (d / "config.yaml").stat()
                    catdirs.append((d.name, st.st_mtime_ns, st.st_size))
                except FileNotFoundError:
                    catdirs.append((d.name, None, None))
            catdirs = tuple(sorted(catdirs))
        except OSError:
            catdirs = ()
        # 指纹里带上 PROJECT_ROOT(2026-10-09): 单测会把 PROJECT_ROOT 指到临时目录,
        # 不带它就吃到上一个根的缓存(实测 3 个 /api/history 用例因此串数据)。
        sig = (str(PROJECT_ROOT), names, cnames, catdirs)
        hit = _SCAN_CONFIGS_CACHE.get("files")
        if hit and hit[0] == sig:
            self._config_files_incomplete = False
            return hit[1]
        with _SCAN_LOCK:                       # 单飞: 并发请求只让第一个做全量扫描
            hit = _SCAN_CONFIGS_CACHE.get("files")
            if hit and hit[0] == sig:
                self._config_files_incomplete = False
                return hit[1]
            files = self._config_files()
            if files and not getattr(self, "_config_files_incomplete", False):
                _SCAN_CONFIGS_CACHE["files"] = (sig, files)
            else:
                _SCAN_CONFIGS_CACHE.pop("files", None)
            return files

    def _scan_configs(self) -> list:
        """扫描所有可选配置(项目根 *.yaml / configs/*.yaml / data/<CAT>/config.yaml)。

        性能(2026-10-09): 本函数对**每个**配置做 YAML 解析 + 占位判定 + 目录一致性检查,
        16 个类目冷态实测 0.8~1.3s; /api/bootstrap 与 /api/configs 各调一次 → 首屏要多等约 1.8s。
        这里做两层缓存: 文件清单看目录结构指纹(见 _config_files_cached), 结果项看
        **文件内容指纹**(mtime_ns+size) + 结构指纹 —— 配置一改立刻反映, 无需时间 TTL。
        """
        files = self._config_files_cached()
        try:
            sig = (_SCAN_CONFIGS_CACHE.get("files", (None,))[0],
                   tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p, _ in files))
        except OSError:
            sig = None                      # stat 失败(文件刚被删/改名) → 不走缓存
        if not files or getattr(self, "_config_files_incomplete", False):
            sig = None
        if sig is not None:
            hit = _SCAN_CONFIGS_CACHE.get("v")
            if hit and hit[0] == sig:
                return hit[1]
        with _SCAN_LOCK:                       # 单飞(第二次检查在锁内, 排队者直接吃结果)
            if sig is not None:
                hit = _SCAN_CONFIGS_CACHE.get("v")
                if hit and hit[0] == sig:
                    return hit[1]
            items = []
            for p, name in files:
                self._add_config_item(items, p, name)
            if sig is not None and items:
                _SCAN_CONFIGS_CACHE["v"] = (sig, items)
            return items

    def _config_files(self) -> list:
        """(路径, 下拉显示名) 清单: 根模板 + configs/ + 各类目 data/<CAT>/config.yaml。"""
        self._config_files_incomplete = False
        out: list[tuple[Path, str]] = [(p, p.name) for p in sorted(PROJECT_ROOT.glob("*.yaml"))]
        cfgdir = PROJECT_ROOT / "configs"
        if cfgdir.is_dir():   # 月度/类目配置统一放 configs/(2026-09)
            out += [(p, f"configs/{p.name}") for p in sorted(cfgdir.glob("*.yaml"))]
        # 类目配置模型(2026-09): data/<CAT>/config.yaml 是每类目的当前配置, 也列入下拉
        try:
            from core.category_config import list_category_configs as _lcc
            out += [(cfg, f"data/{cfg.parent.name}/config.yaml")
                    for cfg in _lcc(PROJECT_ROOT / "data")]
        except Exception as exc:
            print(f"[webui] 类目配置扫描降级: {type(exc).__name__}")
            try:
                for d in sorted((PROJECT_ROOT / "data").iterdir()):
                    if not d.is_dir() or d.name == "archive" or d.name.startswith("."):
                        continue
                    cfg = d / "config.yaml"
                    if not cfg.is_file():
                        continue
                    try:
                        cat = str(load_yaml_file(cfg).get("CAT") or "").strip()
                    except Exception:
                        self._config_files_incomplete = True
                        continue
                    if cat and not _is_placeholder_cat_fallback(cat):
                        out.append((cfg, f"data/{d.name}/config.yaml"))
            except Exception as fallback_exc:
                self._config_files_incomplete = True
                print(f"[webui] 类目配置兜底扫描失败: {type(fallback_exc).__name__}")
        return out

    def _add_config_item(self, items: list, p: Path, name: str) -> None:
        try:
            vals = load_yaml_file(p)
        except Exception:
            vals = {}
        # 占位/模板 CAT(如 your-category-US-YYYYMM、无 CAT)不进入运行台列表(2026-09)
        try:
            from core.category_config import is_placeholder_cat as _ph
        except Exception:
            _ph = _is_placeholder_cat_fallback
        if _ph(str(vals.get("CAT", "") or "").strip()):
            return
        # 防呆(2026-09-13): 标注"CAT 与目录名不一致"的错位配置(前端显示 ⚠ 便于识别, 运行/保存会阻断)
        mismatch = False
        try:
            from core.category_config import find_data_cat_dir as _fd
            found = _fd(p)
            cat_now = str(vals.get("CAT", "") or "").strip()
            mismatch = bool(found and cat_now and cat_now != found[1])
        except Exception:
            mismatch = False
        items.append({
            "name": name,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "cat": vals.get("CAT", ""),
            "dry_run": bool(vals.get("DRY_RUN", False)),
            "has_dry_run_key": "DRY_RUN" in vals,
            "dir": p.parent.name,
            "cat_dir_mismatch": mismatch,
            # data_root 随条目一起返回(2026-10-09): 类目中心 /api/history 复用本缓存,
            # 不必再解析一次 YAML(那是该接口原来最大的开销)
            "data_root": str(vals.get("DATA_ROOT", "") or "") or "data",
        })

    def _api_configs(self) -> dict:
        # ★ 2026-11-12: 非 admin 只返回白名单内的类目（只有 admin 能看到全部）
        allowed = self._allowed_cats()
        items = self._scan_configs()
        if allowed is not None:
            items = [it for it in items if str(it.get("cat") or "").strip() in allowed]
        return {"ok": True, "configs": items}

    def _api_log(self, q: dict) -> dict:
        """实时日志接口(2026-09-08): file 定位类目, full=1 返回 pipeline-progress.log
        全部行(行数保护 5 万, 超长只留尾段并标记 truncated); 默认与状态快照一致取尾 200。"""
        file = str(q.get("file", [""])[0] or "").strip() or "config.yaml"
        full = str(q.get("full", ["0"])[0] or "0").strip().lower() in ("1", "true", "yes")
        try:
            cfg = self._status_cfg(file)
        except Exception:
            cfg = {}
        cat = str(cfg.get("CAT", "") or "")
        root = str(cfg.get("DATA_ROOT", "data") or "data")
        mdir = monitor_dir(cat, root) if cat else None
        if not mdir:
            return {"ok": True, "file_log": [], "truncated": False}
        p = mdir / "pipeline-progress.log"
        if not p.is_file():
            return {"ok": True, "file_log": [], "truncated": False}
        if full:
            try:
                lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            except OSError:
                return {"ok": True, "file_log": [], "truncated": False}
            truncated = len(lines) > 50000
            if truncated:
                lines = lines[-50000:]
            return {
                "ok": True,
                "file_log": parse_log_lines(lines),
                "truncated": truncated,
                "total": len(lines),
            }
        return {"ok": True, "file_log": parse_log_lines(tail_text_lines(p, n=300))[-200:],
                "truncated": False}

    def _api_config_get(self, file: str) -> dict:
        path = safe_config_path(file)
        vals = load_yaml_file(path)
        # ★ 2026-11-12: 类目白名单 —— 非 admin 不能读未授权类目的配置（含密钥）
        self._require_cat(str(vals.get("CAT", "") or "").strip())
        scalars = {}
        for k, v in vals.items():
            if v is None or isinstance(v, (str, bool, int, float)):
                scalars[k] = v
        return {
            "ok": True,
            "file": file,
            "abs_path": str(path),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "values": scalars,
            # 列表型键单独返回(前端专用编辑器); 键缺失为 null
            "lists": {"SS_ACCOUNTS": parse_ss_accounts(vals.get("SS_ACCOUNTS"))},
            "raw": path.read_text(encoding="utf-8-sig", errors="replace"),
        }

    # ---- 实现: 启动运行
    def _build_run_config(self, base_path: Path, overrides: dict) -> Path:
        """生成临时有效配置: 基础配置 + 本次覆盖(行级改写保留注释)。"""
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        text = base_path.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines(keepends=True)
        plain = [ln.rstrip("\r\n") for ln in lines]
        existing = load_yaml_file(base_path)
        coerced = {}
        for k, v in overrides.items():
            if v is None:
                continue
            if isinstance(v, str):
                coerced[k] = coerce_by_existing(k, v, existing)
            else:
                coerced[k] = v
        new_lines = patch_yaml_lines(plain, coerced)
        # 快照命名只保留真源名(2026-09-08): 剥掉链式 run-<ts>- 前缀——源若为上一份
        # 快照时旧逻辑会拼出 run-新ts-run-旧ts-… 无限套娃(曾链 10 层到 259 字符;
        # 且启动中断后界面会残留"指向不存在快照"的幽灵名, 再次启动报 FileNotFoundError)
        stem = re.sub(r"^(?:run-\d{8}-\d{6}-)+", "", base_path.stem)
        # ⚠️ 快照名必须**每个类目唯一 + 每次唯一**(2026-09-15, 多类目并行):
        # 真实类目配置都叫 `data/<CAT>/config.yaml`(stem 全是 "config"), 而 `.webui/runs` 是
        # **全局共享**目录 —— 只按"时间戳秒 + stem"命名时, 同一秒内启动的两个类目会算出**同一个**
        # `run-<ts>-config.yaml`, 后者直接覆盖前者; 若先启动的子进程还没读到配置, 它会加载
        # **另一个类目**的配置 → 把产物写进对方的数据目录(实测复现过, 就是"数据互相污染")。
        cat = str(existing.get("CAT", "") or "").strip()
        tag = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cat)[:40] or "cat"
        conf = RUN_DIR / (f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}-{stem}"
                          f"-{uuid.uuid4().hex[:6]}.yaml")
        conf.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        # 校验临时配置可解析
        load_yaml_file(conf)
        return conf

    def _effective_vals(self, conf: Path) -> dict:
        vals = load_yaml_file(conf)
        dr = vals.get("DRY_RUN", False)
        if isinstance(dr, str):
            dr = str(dr).strip().lower() in ("1", "true", "yes", "on")
        return {
            "CAT": str(vals.get("CAT", "") or "").strip(),
            "DATA_ROOT": str(vals.get("DATA_ROOT", "data") or "data"),
            "DRY_RUN": bool(dr),
        }

    def _keyword_edit_guard(self, cfg: dict, *, capture_baseline: bool) -> None:
        cat, root = cfg["CAT"], cfg["DATA_ROOT"]
        if RUNNER.active_for(cat) or _lock_live_started(cat, str(root))[0]:
            raise UserFacingError("该类目正在运行，请等运行结束后再修改关键词清单。")
        if not capture_baseline:
            return
        from core import keyword_confirmation as kc
        from core.paths import STEP_DIR_NAMES
        from core.state import RunState
        from steps.ids import MONITOR_STEP
        cat_dir = cat_root_dir(cat, root)
        gate = kc.read(cat_dir)
        if gate and gate.get("status") == "pending":
            return
        monitor = cat_dir / STEP_DIR_NAMES[MONITOR_STEP]
        state = RunState(str(monitor / "step-status.txt"), str(monitor / "step-duration.csv"))
        if state.status_of(8) == "DONE":
            baseline = (gate or {}).get("confirmed_words") or kc.current(cat, root)
            kc.pending(cat_dir, baseline)

    def _api_run(self, body: dict) -> dict:
        # 串行完成分配、冲突检查和进程登记，避免两个请求同时拿到尚未监听的同一端口。
        with _RUN_START_LOCK:
            return self._api_run_locked(body)

    def _api_run_locked(self, body: dict) -> dict:
        file = str(body.get("config_file") or "config.yaml")
        base = safe_config_path(file)
        mode = str(body.get("mode") or "resume")  # resume | from | step
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("overrides 必须是对象")

        # A closed report dialog is not confirmation. Reject before starting a
        # process or allocating a browser; permit rerunning step 15 to repair a
        # missing/old report and permit earlier steps to run independently.
        if mode not in ("confirm_keywords",):
            from core.step15_review import pending as report_pending
            gate_cfg = self._status_cfg(file)
            gate_cat = str(gate_cfg.get("CAT") or "").strip()
            gate_root = str(gate_cfg.get("DATA_ROOT") or "data")
            if gate_cat:
                self._require_run(gate_cat)
                _, parsed = _hist_status_snapshot(monitor_dir(gate_cat, gate_root))
                status15 = str(parsed.get(15, {}).get("status") or "PENDING")
                target_after = mode == "resume" or (mode == "step" and int(body.get("step", 0)) > 15) or (mode == "from" and int(body.get("from_step", 0)) > 15)
                if target_after and status15 == "DONE" and report_pending(cat_root_dir(gate_cat, gate_root)):
                    raise UserFacingError("步骤15选品报告尚未确认，请打开报告并点击‘确认报告并继续’；如报告无法读取，请先单独重新运行步骤15")

        # 本次显式覆盖 > 类目配置 > 自动分配。旧逻辑只看 overrides，
        # 将类目配置中的固定端口误覆盖成另一个空闲端口。
        _cdp_override = str(overrides.get("SS_CDP_URL") or "").strip()
        _cdp_base = str(load_yaml_file(base).get("SS_CDP_URL") or "").strip()
        _explicit_auto = _cdp_override.lower() == "auto"
        if not _cdp_override and _cdp_base and _cdp_base.lower() != "auto":
            # 前端可能传空值；不让空值覆盖基础配置的有效端口。
            overrides = dict(overrides)
            overrides.pop("SS_CDP_URL", None)
        _needs_auto = _explicit_auto or (
            not _cdp_override and _cdp_base.lower() in ("", "auto")
        )
        if _needs_auto:
            try:
                from core.ss_lock import pick_port, normalize_cdp_url
                _me = str((getattr(self, "_user", None) or {}).get("name") or "")
                _reserved = set()
                _same_user_ports = set()
                for _run in RUNNER.snapshots():
                    if _run.get("active"):
                        _addr = normalize_cdp_url(str(_run_config_values(_run).get("SS_CDP_URL") or ""))
                        if _addr:
                            try:
                                _running_port = int(_addr.rsplit(":", 1)[1])
                                if str(_run.get('user') or '').strip().casefold() == _me.strip().casefold() and _me:
                                    _same_user_ports.add(_running_port)
                                else:
                                    _reserved.add(_running_port)
                            except (IndexError, ValueError):
                                pass
                from core.profile_isolate import user_ss_profile_path
                _profile = str(user_ss_profile_path(_me)) if _me else ''
                if len(_same_user_ports) > 1 or (_same_user_ports & _reserved):
                    raise RuntimeError('当前工作台用户的浏览器端口状态冲突，请先检查在跑任务')
                if _same_user_ports:
                    # The first run owns the reservation even while Chrome starts.
                    _port = next(iter(_same_user_ports))
                else:
                    _port = (pick_port(_me, exclude_ports=_reserved, profile_dir=_profile)
                             if _profile else pick_port(_me, exclude_ports=_reserved))
                overrides = dict(overrides or {})
                overrides["SS_CDP_URL"] = "http://127.0.0.1:%d" % _port
            except Exception as _exc:
                raise UserFacingError(f"CDP 端口自动分配失败，无法安全启动: {_exc}") from _exc
        conf = self._build_run_config(base, overrides)
        eff = self._effective_vals(conf)
        if not eff["CAT"]:
            raise ValueError(f"配置缺少 CAT(类目名): {file}")
        # ★ 2026-11-12: 类目白名单 —— 非 admin 不能启动未授权的类目
        self._require_run(str(eff["CAT"]))
        # 守门(多类目并行, 2026-09-15): **按类目**判重 —— 同一个类目只允许一条流水线,
        # 不同类目放行(这正是"同时跑多个类目"要的)。
        _cat_key = str(eff["CAT"] or "").strip()
        _data_root = str(eff.get("DATA_ROOT", "data") or "data")
        _effective_config = load_yaml_file(conf)
        _others = run_start_guard(_cat_key, _data_root, effective_config=_effective_config)
        # 并行资源提示(不阻断): 卖家精灵浏览器按**资源**互斥(不同端口=真并行; 同端口=排队);
        # 亚马逊抓取会同时占本机 IP(步骤0/1/10 并发时更易触发风控) —— 如实提示, 便于用户取舍。
        # 2026-09-16: 这些提示**先攒起来**, 等 RUNNER.start() 建好本类目缓冲后**只写进本类目**
        # (原先直接 RING.push → mirror 广播进所有在跑类目的控制台, 造成"看 FR 却见到 UK 的消息")。
        _pre_notes: list[tuple[str, str]] = []
        for _note in ss_parallel_notes(_cat_key, _data_root, _others, effective_config=_effective_config):
            _pre_notes.append((f"⚠ 并行提示: {_note}", "WARN"))
        if _others:
            _pre_notes.append(("◆ 并行运行: 已有 "
                               + ", ".join(str(r.get("cat") or r.get("label")) for r in _others)
                               + f" 在跑; 本类目将与之并行(卖家精灵浏览器按端口互斥; 亚马逊抓取共用本机 IP, "
                                 f"步骤0/1/10 并发时风控概率更高)", "WARN"))
        # 防呆(2026-09-13): 类目配置里的 CAT 必须等于其所在目录名, 否则运行会读写另一个类目目录
        # (并可能把本文件内容覆盖到那个类目的配置上)。检查的是**用户选中的文件** base。
        from core.category_config import check_cat_dir_consistency as _check_cat_dir
        _mismatch = _check_cat_dir(base, eff["CAT"], eff["DATA_ROOT"])
        if _mismatch:
            raise ValueError(_mismatch)
        # 站点口径提示(2026-09-14): 只告警不阻断(换站点是正常操作), 提示进 ③实时日志
        for _w in site_market_warnings(load_yaml_file(base)):
            _pre_notes.append((f"⚠ {_w}", "WARN"))

        py = [sys.executable, "-u", "-X", "utf8", "main.py"]
        # main.py 的 run/step 会以 --dry-run 缺省值(False)覆盖配置文件的 DRY_RUN,
        # 因此配置/覆盖判定为离线时必须显式传 --dry-run, 否则会以真实模式运行。
        # 注意: --dry-run 属于子命令参数, 必须放在 run/step 之后。
        dry_flag = ["--dry-run"] if eff["DRY_RUN"] else []
        if mode == "confirm_keywords":
            revision = str(body.get("revision") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise ValueError("关键词确认版本无效，请刷新后重试")
            cmd = py + ["confirm-keywords", "--revision", revision] + dry_flag + ["--config", str(conf)]
            label = "确认关键词并续跑"
        elif mode == "step":
            step = int(body.get("step", 0))
            if not 0 <= step < STEP_COUNT:
                raise ValueError(f"单步执行步骤号必须在 0-{STEP_COUNT - 1}")
            cmd = py + ["step", str(step)] + dry_flag + ["--config", str(conf)]
            label = f"单步执行 步骤{step}"
        elif mode == "from":
            n = int(body.get("from_step", 0))
            if not 0 <= n < STEP_COUNT:
                raise ValueError(f"起始步骤必须在 0-{STEP_COUNT - 1}")
            cmd = py + ["run", "--from", str(n)] + dry_flag + ["--config", str(conf)]
            label = f"从步骤{n}开始(断点续跑)"
        else:  # resume / run
            cmd = py + ["run"] + dry_flag + ["--config", str(conf)]
            label = "全流程运行(自动断点续跑)"
        # 配置随类目(2026-09): 把本轮"生效配置"同步到 data/<CAT>/config.yaml(实时更新/读取),
        # 并让子进程以该类目配置为源(PIPELINE_CONFIG_SOURCE), 供月份回退等写回该类目文件。
        #
        # ⚠️ 2026-09-15 修(用户报障"配置中心里怎么变成了 Chopping Boards-UK-202608 DRY?"):
        # **带临时覆盖(overrides)时绝不写回类目配置**。`overrides` 是"本轮临时选择"(前端
        # 「模式」= dry-run/真实的单选), 写回去就变成**永久配置**, 而且这条路径**不生成备份**、
        # 用户毫无察觉 —— 实测: 一次 `{DRY_RUN: true}` 的测试运行把真实类目
        # `data/Cat Fountains-US-202608/config.yaml` 的 DRY_RUN 永久改成了 true(6 个历史备份
        # 全是 false, 当前文件却是 true 且无对应备份), 配置中心因此给它打上 `DRY` 标。
        # 现在: 有 overrides → 只把 PIPELINE_CONFIG_SOURCE 指向类目配置(月份回退等"该写回的键"
        # 照常写回), 但不动文件内容; 无 overrides → 照旧同步(内容本来就一致)。
        percat = conf
        try:
            from core.category_config import category_config_path, sync_category_config
            if eff.get("CAT"):
                if overrides:
                    percat = category_config_path(eff["DATA_ROOT"], eff["CAT"])
                    _pre_notes.append(("◆ 本轮带临时覆盖(" + ", ".join(sorted(map(str, overrides)))
                                       + f"): 只作用于本次运行, **不写回**类目配置 {Path(percat).name}",
                                       "WARN"))
                else:
                    percat = sync_category_config(conf, eff["DATA_ROOT"], eff["CAT"])
        except Exception:
            pass
        try:
            from webui import auth as _run_auth
            _category_owner = str((_run_auth.owner_of(eff['CAT']) or {}).get('owner') or '')
        except Exception:
            _category_owner = ''
        info = RUNNER.start(cmd, label, file, conf,
                            eff["CAT"], eff["DATA_ROOT"], mode,
                            int(body.get("from_step", 0) or 0), overrides,
                            extra_env={"PIPELINE_CONFIG_SOURCE": str(percat),
                                       "PIPELINE_CATEGORY_NAME": str(eff["CAT"]),
                                       "PIPELINE_CATEGORY_OWNER": _category_owner,
                                       "PIPELINE_WORKBENCH_USER": str((getattr(self, "_user", None) or {}).get("name") or "")},
                            user=str((getattr(self, "_user", None) or {}).get("name") or ""))
        info.pop("effective_conf", None)
        info["dry_run"] = eff["DRY_RUN"]
        # 提示在本类目缓冲建好之后**只写本类目**(见上面 _pre_notes 的说明)
        for _t, _lv in _pre_notes:
            push_cat(eff["CAT"], _t, level=_lv)
        return {"ok": True, "run": info}

    # ---- 实现: 配置保存
    def _api_config_save(self, body: dict) -> dict:
        file = str(body.get("config_file") or "config.yaml")
        path = safe_config_path(file)
        values = body.get("values") or {}
        lists = body.get("lists") or {}
        if not isinstance(values, dict) or not isinstance(lists, dict):
            raise ValueError("values/lists 必须是对象")
        if {"ATTR_SPEC_RUNS", "ATTR_STAB_MIN_RATE"} & set(values):
            raise ValueError("抽取遍数和一致率提示门槛已固定，不可写入配置")
        if not values and not lists:
            raise ValueError("没有可保存的字段")
        existing = load_yaml_file(path)
        # ★ 2026-11-12: 类目白名单 —— 非 admin 不能改未授权类目的配置（含密钥）
        self._require_run(str(existing.get("CAT", "") or "").strip())
        coerced = {}
        for k, v in values.items():
            if v is None:
                continue
            if isinstance(v, str):
                coerced[k] = coerce_by_existing(k, v, existing)
            else:
                coerced[k] = v

        # 校验列表键(目前仅 SS_ACCOUNTS)
        accts = None
        if "SS_ACCOUNTS" in lists:
            accts = lists["SS_ACCOUNTS"]
            if accts is not None:
                if not isinstance(accts, list):
                    raise ValueError("SS_ACCOUNTS 必须是账号列表")
                for it in accts:
                    if not isinstance(it, dict) or not str(it.get("account", "") or "").strip():
                        raise ValueError("SS_ACCOUNTS 每项需包含非空 account(账号)")

        # 计算真正变化的键(2026-09 修复: 逐键语义比对, 不再把 POST 上来的全部键都算作"已改")
        # —— 只对比"即将写入的值"与"当前文件里解析出的值", 避免只改 1 项却提示改动 N 项。
        changed = [k for k, v in coerced.items() if existing.get(k) != v]
        if accts is not None:
            old_accts = parse_ss_accounts(existing.get("SS_ACCOUNTS"))
            if old_accts != parse_ss_accounts(accts):
                changed.append("SS_ACCOUNTS")
        # 2026-09-16: 配置保存的消息**按该类目路由**(原先走全局 RING → mirror 广播进所有在跑类目的
        # 控制台: 保存 UK 配置时正在看 FR 的人也会看到 "配置保存 …UK…" 这类行)。
        cfg_cat = str(coerced.get("CAT") or existing.get("CAT") or "").strip()
        if not changed:
            push_cat(cfg_cat, f"配置保存 {file}: 内容无实际改动, 未生成新备份(仍为原数据)")
            out = {"ok": True, "changed": [], "backup": None, "unchanged": True}
            warn = site_market_warnings(existing)
            if warn:
                out["warnings"] = warn
                for w in warn:
                    push_cat(cfg_cat, f"⚠ {w}", level="WARN")
            return out

        # 构建改写后的内容(上面已确认有改动; 无改动会提前返回, 不写盘、不备份)
        # 入参层面先拦"必须单行的键带了换行"(2026-09-29: 粘两行的 COOKIE 曾把配置写坏)
        bad_val = multiline_value_error(coerced)
        if bad_val:
            push_cat(str(coerced.get("CAT") or existing.get("CAT") or ""), f"✗ {bad_val}", level="WARN")
            raise UserFacingError(bad_val)
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if coerced:
            lines = patch_yaml_lines(lines, coerced)
        if accts is not None:
            lines = replace_yaml_block(lines, "SS_ACCOUNTS", render_ss_accounts(accts))
        new_text = "\n".join(lines) + "\n"

        # 防呆(2026-09-13): 改 CAT 会让"文件位置"与"配置身份"错位 —— 保存前校验, 不一致就阻断;
        # 前端会带 allow_move=true 再试一次, 此时把配置(连同备份目录)搬到 data/<新CAT>/。
        from core.category_config import (
            CAT_DIR_MISMATCH_TOKEN as _CAT_TOKEN,
            category_config_path as _cat_cfg_path,
            check_cat_dir_consistency as _check_cat_dir,
            find_data_cat_dir as _find_data_cat_dir,
        )
        new_cat = ""
        for ln in lines:
            if ln.startswith("CAT:"):
                new_cat = ln.split(":", 1)[1].split("#", 1)[0].strip().strip('"').strip("'")
                break
        mismatch = _check_cat_dir(path, new_cat, existing.get("DATA_ROOT") or "data")
        moved_to = None
        if mismatch and not body.get("allow_move"):
            raise ValueError(mismatch)
        if mismatch and body.get("allow_move"):
            found = _find_data_cat_dir(path, existing.get("DATA_ROOT") or "data")
            if found is None:
                raise ValueError(mismatch)
            root, _dir_name = found
            target = _cat_cfg_path(root, new_cat)
            if target.is_file():
                raise ValueError(
                    f"{_CAT_TOKEN} 目标配置已存在, 不能自动迁移: {target}。"
                    f" 请确认是否要覆盖, 或改为在 webui 里直接编辑该类目配置。")
            target.parent.mkdir(parents=True, exist_ok=True)
            # 备份目录跟着类目走(目标无备份目录时才整体搬, 避免与已有历史混在一起)
            src_bdir = path.parent / "config-backups"
            dst_bdir = target.parent / "config-backups"
            if src_bdir.is_dir() and not dst_bdir.exists():
                try:
                    shutil.move(str(src_bdir), str(dst_bdir))
                except OSError as exc:  # noqa: BLE001
                    push_cat(cfg_cat or new_cat, f"[配置迁移] 备份目录搬迁失败(已跳过): {exc}",
                             level="WARN")
            shutil.move(str(path), str(target))
            path = target
            moved_to = f"data/{target.parent.name}/config.yaml"
            push_cat(cfg_cat or new_cat,
                     f"[配置迁移] CAT={new_cat} 与目录不一致 -> 已把配置迁移到 {moved_to}")

        from core.config_backup import make_backup as _mk_backup
        _cfg_cat2 = cfg_cat or new_cat
        # ---- ① **先校验, 再备份, 再写盘**(2026-09-29 修复) ------------------------------
        # 旧顺序是"先备份 → 先写盘 → 再校验": 校验失败时接口老老实实报"保存失败", 但**文件已经
        # 被写坏**(真事故: COOKIE 粘成两行 → YAML ScannerError → 配置不可用, 用户手里只有一句报错)。
        # 现在校验不过**一个字节都不写**, 并给出可操作提示(见 `yaml_save_guard`)。
        guard = yaml_save_guard(new_text)
        if guard:
            push_cat(_cfg_cat2, f"✗ {guard.splitlines()[0]}", level="WARN")
            raise UserFacingError(guard)
        bak = _mk_backup(path, reason="webui 保存前自动备份",
                         log=lambda m: push_cat(_cfg_cat2, m), changed_to=new_text)
        original_bytes = path.read_bytes()            # 兜底回滚用**字节级原样**(备份文件带说明头, 不适合直接回灌)
        path.write_text(new_text, encoding="utf-8")
        # ---- ② 写盘后再校验一次(兜底) + 失败自动回滚 ----------------------------------
        # 回滚优先用内存里的原始字节(与保存前**逐字节一致** —— 备份文件头部有 5 行说明注释,
        # 直接拷回来会让配置多出注释); 内存回滚失败才退而用备份文件。
        try:
            load_yaml_file(path)
        except Exception as exc:  # noqa: BLE001 — 兜底: 落盘/编码异常时不能让配置留在坏状态
            rollback = ""
            try:
                path.write_bytes(original_bytes)
                rollback = ", 已回滚为保存前内容(逐字节一致)"
            except OSError as wbe:  # noqa: BLE001
                rollback = f", 内存回滚失败({wbe})"
            if "已回滚" not in rollback and bak is not None and Path(bak).is_file():
                try:
                    shutil.copyfile(str(bak), str(path))
                    rollback = f", 已回滚为保存前内容(来自备份 {Path(bak).name}, 会带上备份说明头)"
                except OSError as rbe:  # noqa: BLE001
                    rollback += f", 且备份回滚也失败({rbe}) —— 请手工恢复"
            msg = f"保存后校验失败({exc}){rollback}"
            push_cat(_cfg_cat2, f"✗ {msg}", level="WARN")
            raise UserFacingError(msg)
        bak_txt = f" (备份: {bak.name})" if bak else " (备份未生成: 见上方备份事件日志)"
        push_cat(_cfg_cat2, f"已保存配置 {moved_to or file} → 更新 {len(changed)} 项{bak_txt}")
        # 保存后自动按保留策略清理旧备份(2026-09)
        try:
            from core.config_backup import default_prune_logger as _prune_log
            from core.config_backup import prune as _prune_backups
            _prune_backups(log=_prune_log())
        except Exception:
            pass
        out = {"ok": True, "changed": changed, "backup": bak.name if bak else None}
        if moved_to:
            out["moved_to"] = moved_to
        # 站点口径提示(2026-09-14): CAT 后缀 / SITE / MARKET 三者互相矛盾时明确告警(**不阻断保存**)
        warn = site_market_warnings(load_yaml_file(path))
        if warn:
            out["warnings"] = warn
            for w in warn:
                push_cat(_cfg_cat2, f"⚠ {w}", level="WARN")
        return out


    # ---- 实现: 维护命令(reset-step / archive), 前台等待
    def _run_main_short(self, body: dict, sub: str, label: str, step: int | None = None) -> dict:
        file = str(body.get("config_file") or "config.yaml")
        base = safe_config_path(file)
        # 多类目并行(2026-09-15): 维护命令只跟**同一个类目**的运行互斥
        # (重置/归档 B 类目不该被正在跑的 A 类目挡住; 但同类目并发会互删产物, 必须拦)。
        try:
            _cat = str(load_yaml_file(base).get("CAT", "") or "").strip()
        except Exception:  # noqa: BLE001
            _cat = ""
        # ★ 2026-11-12: 类目白名单 —— 重置/归档等维护命令同样受限于授权类目
        self._require_run(_cat)
        if _cat and RUNNER.active_for(_cat):
            raise UserFacingError(f"类目「{_cat}」正在运行, 不能同时执行维护命令: {label}")
        if not _cat and RUNNER.active:
            raise UserFacingError("已有流水线正在运行, 不能同时执行维护命令")
        conf = base  # 维护命令直接用原配置文件(不写临时文件)
        py = [sys.executable, "-u", "-X", "utf8", "main.py", sub]
        if step is not None:
            py.append(str(step))
        py += ["--config", str(conf)]
        # 2026-09-16: 维护命令是**按类目**的执行 → 输出只进该类目的缓冲(没有则进服务器级缓冲),
        # 绝不广播到别的在跑类目(否则用户会在 FR 的运行台里看到 UK 的重置输出, 实测 18 行)。
        _ring = cat_ring(_cat)
        push_cat(_cat, f"▶ [{label}] 执行: {' '.join(py)}")
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        try:
            res = subprocess.run(
                py, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            push_cat(_cat, f"■ [{label}] 执行超时(300s)", level="ERROR")
            return {"ok": False, "error": "执行超时(300s)"}
        lines = (res.stdout or "").splitlines()
        for ln in lines[-40:]:
            push_console_line(ln, ring=_ring or RING)
        push_cat(_cat, f"■ [{label}] 结束 code={res.returncode}")
        return {"ok": True, "exit_code": res.returncode,
                "output": "\n".join(lines[-60:])}

    def _api_reset_step(self, body: dict) -> dict:
        n = int(body.get("step", 0))
        if not 0 <= n < STEP_COUNT:
            raise ValueError(f"步骤号必须在 0-{STEP_COUNT - 1}")
        return self._run_main_short(body, "reset-step", f"重置步骤{n}-19", step=n)

    def _api_archive(self, body: dict) -> dict:
        return self._run_main_short(body, "archive", "手动完整快照")

    # ---- 状态使用的有效配置: **永远以所请求的配置文件为准**
    def _status_cfg(self, file_q: str | None) -> dict:
        """按所请求配置文件解析 CAT/DATA_ROOT(status/files/download/keyword-lists 共用)。

        **规则(2026-09-14 修复"类目中心切类目后半页跟着别的类目")**:
          ① **显式传 file** 且解析出非空 CAT → 就用它(不管此刻有没有任务在跑、跑的是哪个类目;
             根/占位模板也一样 —— 它们本身不是可运行类目, 显示空看板比"串到运行类目"更不易误判);
          ② **未传 file**(诊断/监控调用, 如 `curl /api/status`) → 跟随正在运行的类目;
          ③ 传了 file 但解析不出(文件不存在/配置缺 CAT) → 同样回落运行类目。

        原实现是"运行中默认跟随运行类目, 只有带 browse=1 才按 file" —— 而 🏠类目中心
        「打开运行台」走的 `enterRunWith` 不带 browse, 于是同一个页面里
        ①运行控制(配置面板用 selFile)显示所选类目, ②运行状态/③实时日志/⑤产物/⑥关键词清单
        却显示正在运行的那个类目(实测: 请求 Dog Food Storage 返回 Grilling 的步骤与日志);
        更危险的是写路径 `/api/keyword-lists/save` 当时也不带 browse, 运行中保存会**写进别的类目**。
        现在 file 唯一决定类目 → 六个模块永远同一个类目, 读写路径也一致。
        """
        name = file_q or "config.yaml"
        req_cat, req_root = "", "data"
        try:
            p = safe_config_path(name)
            vals = load_yaml_file(p)
            req_cat = str(vals.get("CAT", "") or "").strip()
            req_root = str(vals.get("DATA_ROOT", "data") or "data")
        except Exception:
            pass
        run = RUNNER.snapshot()
        # ② 未传 file -> 跟随运行类目(诊断/监控用); ① 显式 file 优先
        if not file_q and run.get("active") and run.get("cat"):
            result = {"CAT": run["cat"], "DATA_ROOT": run.get("data_root", "data")}
        elif req_cat:
            result = {"CAT": req_cat, "DATA_ROOT": req_root}
        elif run.get("active") and run.get("cat"):
            result = {"CAT": run["cat"], "DATA_ROOT": run.get("data_root", "data")}
        else:
            result = {"CAT": req_cat, "DATA_ROOT": req_root}
        # ★ 2026-11-12: 类目白名单 —— 非 admin 只能读/写被授权的类目（所有走 _status_cfg 的接口自动覆盖）
        self._require_cat(str(result.get("CAT") or ""))
        return result


def another_instance_live(host: str, port: int, timeout: float = 0.6) -> str:
    """另一个看板实例是否已在监听 host:port? 返回它自报的 build(空串 = 没人监听 / 读不到)。

    为什么需要(2026-10-10 用户实测): `ThreadingHTTPServer` 默认 `allow_reuse_address=1`, 在 Windows 上
    **第二个实例能绑上同一个端口而不报错**(所以下面那个"端口被占就自动 +1"的循环也不会触发) →
    新旧两个实例并存, 请求被谁接走不确定, 页面时而报『旧构建(…): 请重启服务』, 用户以为重启没生效。
    启动时先探测一次, 从源头拒绝静默多实例(`--force` 可显式绕过)。
    """
    import socket
    import urllib.request

    probe_host = host if host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    try:
        with socket.create_connection((probe_host, int(port)), timeout=timeout):
            pass
    except OSError:
        return ""                                     # 端口没人监听 = 可以启动
    try:
        with urllib.request.urlopen("http://%s:%d/api/bootstrap" % (probe_host, int(port)),
                                    timeout=max(1.0, timeout)) as fh:
            return str(json.loads(fh.read().decode("utf-8", "replace")).get("webui_version") or "")
    except Exception:                                 # noqa: BLE001 — 端口被别的东西占着 / 读不到版本
        return "?"


def main():
    ap = argparse.ArgumentParser(description="Amazon 选品流水线 Web 控制台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--force", action="store_true",
                    help="已确认要同时跑两个实例时才用(默认检测到已有实例会拒绝启动)")
    ap.add_argument("--no-open", action="store_true", help="启动时不自动打开浏览器")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[ERROR] WebUI 只能监听本机地址；云端请通过 HTTPS 反向代理访问。")
        return 2
    public_origin = os.environ.get("WEBUI_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if public_origin:
        parsed = urlparse(public_origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            print("[ERROR] WEBUI_PUBLIC_ORIGIN 必须是完整的 HTTPS 来源，例如 https://example.com")
            return 2

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 启动时迁移项目根散落的历史备份到集中目录(幂等)
    try:
        mig = migrate_legacy_config_backups()
        if mig["migrated"]:
            RING.push(f"已迁移 {len(mig['migrated'])} 个历史配置备份到 {CONFIG_BACKUP_DIR.name}/", level="WARN")
    except Exception as exc:
        RING.push(f"配置备份迁移失败(忽略): {exc}", level="WARN")

    server = None
    port = args.port
    if not getattr(args, "force", False):
        other = another_instance_live(args.host, port)
        if other:
            print("=" * 78)
            print("[ERROR] 端口 %s:%d 已经有东西在监听了 —— 看板不会启动。" % (args.host, port))
            print("        它自报的 build = %s"
                  % (other if other != "?" else "(读不到版本, 可能是别的程序占了端口)"))
            print("        为什么必须拦住: 两个实例会抢同一批状态文件(step-status / .run.lock / 运行快照),")
            print("        而且请求被谁接走不确定 —— 这正是『重启了却还弹旧构建提示』的原因。")
            print("        处理: ① 双击 start_webui.bat(启动前会自动清掉占用者, 不影响正在跑的流水线子进程)")
            print("              ② 或 python scripts/webui_port_guard.py --yes")
            print("              ③ 确认要同时跑两个实例时加 --force(不推荐)")
            print("=" * 78)
            return 2
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            port += 1
    if server is None:
        print(f"[webui] 无法绑定端口 {args.port}-{args.port + 19}, 请换端口重试")
        return 1

    url = f"http://{args.host}:{port}/"
    print("=" * 60)
    print("  Amazon 选品流水线 Web 控制台")
    print(f"  项目根目录 : {PROJECT_ROOT}")
    print(f"  构建版本   : {WEBUI_VERSION}")
    print(f"  访问地址   : {url}")
    print("  关闭方式   : 在此窗口按 Ctrl+C")
    print("=" * 60)
    RING.push(f"Web 控制台已启动: {url}  (build {WEBUI_VERSION}, python {sys.version.split()[0]})")
    # 后台预热首屏重扫描(2026-10-09): 配置清单扫描冷启动约 1.3s, 预热后首个
    # /api/bootstrap 与 /api/configs 直接命中缓存(≈0.3ms), 首屏少等 2 秒左右。
    threading.Thread(target=_warm_startup_caches, name="warm-caches", daemon=True).start()
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[webui] 正在退出…")
        if RUNNER.active:
            print("[webui] 检测到流水线子进程运行中, 正在终止…")
            try:
                RUNNER.stop()
            except Exception:
                pass
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
