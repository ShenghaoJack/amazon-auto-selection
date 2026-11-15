# -*- coding: utf-8 -*-
"""VOC 流水线总编排(供 `steps/step19_voc.py` 调用)。

三步合一(全部从 `_voc_test/` 的 step1/2/3 移植, 不 import 测试目录):
  1. **取父ASIN**: 优先读 `data/<CAT>/14_流量拆分/流量拆分数据-需人工补充.xlsx` 的 `AI已补充` 表
     (按父体月销量合计降序), 缺表时回退 `data/<CAT>/<CAT>-parents.txt`(step10 产物);
     父体→子体映射也取自该表。
  2. **采集**: Pangolinfo 主源(`get_amazon_reviews`, 探针 + 剩余计划), 父体无评论→依次探测子体
     (最多 3 个, 命中即停); 业务层 `code != 0` 视为错误, 判为额度耗尽则切卖家精灵继续剩余任务;
     每请求落 raw JSONL(唯一真值), **断点续跑**(已完成键跳过); 累计积分超预算立即停止。
  3. **归一化+去重 → 标注+归一 → 出 14 表工作簿 → 校验**, 并写出全部审计产物。

设计约束: 只用标准库 + 仓库既有第三方; LLM 不可用时**显式抛错**(由步骤决定是否跳过),
绝不静默产出"只有采集、没有标注"的半成品报告。
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import threading
from queue import SimpleQueue, Empty
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from core.io_utils import ensure_dir, read_jsonl, read_txt_lines
from core.category_config import REGION_PROFILE
from core.mcp import MCPBusinessError, MCPClient
from tools.voc import acquire, annotate, report
from tools.voc.dedupe import dedupe_reviews
from tools.voc.normalize import normalize_record

# ---------------- 默认值与口径
DEFAULT_PAGES_PER_PARENT = 5
DEFAULT_STAR_MIX = "critical:3,four_star:1,five_star:1"
DEFAULT_SOURCE_PRIORITY = "pangolinfo,sellersprite"
DEFAULT_SITE = "amz_us"
DEFAULT_BATCH = 20
DEFAULT_WORKERS = 4
# 积分预算: **不作为配置项**(2026-10-09 用户口径: 靠"Pangolinfo→卖家精灵"双源兜底,
# 不设费用熔断)。仅作为内部 API 参数保留(测试/临时限流), 0 或 None = 不限。
DEFAULT_POINT_BUDGET = 0
# 父体范围全量写死(2026-10-09 用户口径: "要跑肯定是全量跑") —— 不设 VOC_MAX_PARENTS 上限键
CHILD_TRIES = 3
TRAFFIC_SHEET = "AI已补充"
OK_CODES = (None, "OK", 200, 0, "0", "ok", "success")
# ★ Pangolinfo MCP **服务地址：写死在代码里**（2026-10-12 用户口径）。
#   为什么：它是**固定的服务端点、不是秘密** —— 放进 `config.yaml` 只会让每个类目
#   各抄一份（实测 14 个类目全是空值 + 一行注释），Web 配置清单里也多两个没用的框。
#   **凭据（KEY）不在这里** —— 见 `resolve_pangolinfo_creds`：它是本机私有令牌，绝不进仓库。
PANGOLIN_MCP_URL = "https://mcp.pangolinfo.com/mcp"
# Pangolinfo **KEY** 的本地开发兜底(仅本机存在时使用; 也可用环境变量覆盖)
CODEX_CFG = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")

# 未标注评论的失败原因(写入工作簿总览, 与报告口径一致)
NO_LLM_HINT = ("VOC 标注需要 LLM: 请在 config.yaml 配置 LLM_API_KEY/LM_BASE_URL/LLM_MODEL, "
               "或在调用方判定后跳过本步骤")

_COLLECT_LOCK = threading.RLock()
_JSONL_LOCK = threading.RLock()


class VOCAnnotationUnavailable(RuntimeError):
    """LLM 不可用(缺密钥 / dry_run), 无法产出带标注的 VOC 报告。"""


# ================================================================ 小工具
def _json_default(obj):
    """`json.dumps` 兜底: 把 set/日期等不可序列化对象转成字符串, 绝不因留档失败中断采集。"""
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def dump_json(path: str, obj) -> str:
    """写 JSON(带兜底序列化 + 原子改名)。"""
    from tools.voc.storage import save_json
    save_json(path, obj, default=_json_default)
    return path


def write_jsonl(path: str, records: list[dict]) -> str:
    """全量写 JSONL(utf-8, 每行一条)。"""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n")
    return path


def append_jsonl(path: str, records: list[dict]) -> str:
    """追加写 JSONL(采集 raw 用: append-only 才是断点续跑的真值)。"""
    with _JSONL_LOCK:
        return _append_jsonl(path, records)


def _append_jsonl(path: str, records: list[dict]) -> str:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    needs_newline = False
    if os.path.exists(path) and os.path.getsize(path):
        with open(path, "rb") as previous:
            previous.seek(-1, os.SEEK_END)
            needs_newline = previous.read(1) not in (b"\n", b"\r")
    with open(path, "a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")  # Preserve a crash-truncated tail without swallowing the next valid record.
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n")
    return path


def _int(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def attribution_repair_pending(directory):
    path = os.path.join(directory, "归属补采记录.json")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8-sig") as stream:
        return json.load(stream).get("status") in {"running", "pending"}


def _is_asin(text: str) -> bool:
    """ASIN 形态检查(10 位字母数字)。"""
    return len(text) == 10 and text.isalnum()


def _norm_asin(value) -> str:
    return str(value or "").strip().upper()


# ================================================================ 1 取父ASIN
def _emit(msg: str, log=None) -> None:
    """安全输出(2026-10-09): 有 log 回调就交给它(步骤日志→文件), 否则安全 print。

    为什么必须容错: 本流程常由 webui 以 stdout=PIPE 起子进程; 看板重启/控制台关闭后管道读端
    消失, 此时任何 print 都会抛 `OSError: [Errno 22] Invalid argument` —— 不加保护会因
    "打印不出一行进度"而让已花掉数千积分的采集结果整步失败(实测踩到过)。
    """
    try:
        (log or print)(msg)
    except OSError:                      # 断管/无效句柄: 丢这一行输出即可, 不影响流程
        pass


def traffic_path(cat_dir: str) -> str:
    """步骤14 流量拆分工作簿(父体/子体/月销量/品牌/标题的唯一来源)。"""
    return os.path.join(cat_dir, "14_流量拆分", "流量拆分数据-需人工补充.xlsx")


def load_parents_from_traffic(xlsx: str, sheet: str = TRAFFIC_SHEET,
                              limit: int | None = None) -> tuple[list[str], dict[str, list[str]], dict]:
    """从流量拆分表读父体(按月销量合计降序)与父体→子体映射。

    返回 `(父ASIN列表, 子体映射, 父体元信息{brand,title,sales})`。
    表不存在/读失败/无合法父体 → `([], {}, {})`, 由调用方回退 parents.txt。
    """
    if not xlsx or not os.path.exists(xlsx):
        return [], {}, {}
    try:
        import openpyxl
    except ImportError:                       # pragma: no cover  仓库已依赖 openpyxl
        return [], {}, {}
    sales: dict[str, float] = {}
    order: list[str] = []
    kids: dict[str, list[str]] = {}
    meta: dict[str, dict] = {}
    try:
        wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001  表被占用/损坏 → 交给调用方回退
        _emit("  [警告] 流量拆分表读取失败(%s), 将回退父体清单" % str(exc)[:120])
        return [], {}, {}
    try:
        ws = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[0]
        header: list[str] | None = None
        ip = iv = ib = it = isales = None
        for row in ws.iter_rows(values_only=True):
            if header is None:
                header = [str(c) if c is not None else "" for c in row]
                ip = header.index("父ASIN") if "父ASIN" in header else 0
                iv = header.index("变体ASIN") if "变体ASIN" in header else None
                ib = header.index("品牌") if "品牌" in header else None
                it = (header.index("商品标题") if "商品标题" in header
                      else header.index("标题") if "标题" in header else None)
                isales = header.index("月销量") if "月销量" in header else None
                continue
            parent = _norm_asin(row[ip] if ip < len(row) else "")
            if not _is_asin(parent):
                continue
            if parent not in sales:
                sales[parent] = 0.0
                order.append(parent)
                kids[parent] = []
                meta[parent] = {
                    "brand": str(row[ib] if ib is not None and ib < len(row) else "" or "").strip(),
                    "title": str(row[it] if it is not None and it < len(row) else "" or "").strip(),
                }
            try:
                sales[parent] += float(row[isales] if isales is not None and isales < len(row) else 0 or 0)
            except (TypeError, ValueError):
                pass
            child = _norm_asin(row[iv] if iv is not None and iv < len(row) else "")
            if _is_asin(child) and child != parent and child not in kids[parent]:
                kids[parent].append(child)
    finally:
        wb.close()
    ranked = sorted(order, key=lambda p: -sales.get(p, 0.0))[:limit] if limit else \
        sorted(order, key=lambda p: -sales.get(p, 0.0))
    for p in ranked:
        meta[p]["sales"] = sales.get(p, 0.0)
    return ranked, {p: kids.get(p, []) for p in ranked}, meta


def load_parents_file(path: str, limit: int | None = None) -> list[str]:
    """读 `<CAT>-parents.txt`(每行 **一个或多个** ASIN, 容忍 BOM/空行/`#` 注释)。

    一行多 ASIN 的写法(逗号/分号分隔)在人工维护的清单里很常见
    (例如 `B051064F6D,B0EFC31FEF`), 因此按[逗号/分号/空白]切分而不是只取首个;
    去重且保持出现顺序。
    """
    out: list[str] = []
    for line in read_txt_lines(path):
        for token in re.split(r"[,;，；\s]+", str(line).split("#")[0]):
            asin = _norm_asin(token)
            if _is_asin(asin) and asin not in out:
                out.append(asin)
    return out[:limit] if limit else out


def resolve_parents(cat_dir: str, cat: str, limit: int | None = None,
                    traffic_xlsx: str | None = None) -> tuple[list[str], dict[str, list[str]], dict, str]:
    """父ASIN 与子体映射: 流量拆分表优先, 缺失则回退 `data/<CAT>/<CAT>-parents.txt`。

    返回 `(父ASIN列表, 子体映射, 元信息, 来源标识)`; 两处都没有 → 抛错(不静默出空报告)。
    """
    xlsx = traffic_xlsx or traffic_path(cat_dir)
    parents, kids, meta = load_parents_from_traffic(xlsx, limit=limit)
    if parents:
        return parents, kids, meta, "14_流量拆分/" + os.path.basename(xlsx)
    fallback = os.path.join(cat_dir, "%s-parents.txt" % cat)
    parents = load_parents_file(fallback, limit=limit)
    if parents:
        _emit("  [回退] 流量拆分表不可用, 改用 %s(%d 个父体; 无子体映射)"
              % (os.path.basename(fallback), len(parents)))
        return parents, {p: [] for p in parents}, {}, os.path.basename(fallback)
    raise FileNotFoundError("未找到父ASIN: %s 不存在, 且回退文件 %s 不存在/为空" % (xlsx, fallback))


# ================================================================ 2 采集
def parse_star_mix(spec: str) -> dict[str, int]:
    """解析 `VOC_STAR_MIX`(如 `critical:3,four_star:1,five_star:1`); 非法项忽略。

    容忍中文输入法打出的**全角冒号/分号**(`：`/`；`)与全角逗号(`,`):
    用户手改配置时极易打成全角, 若不归一, 整个星级分摊会静默回落到默认权重。
    """
    text = str(spec or "").replace("：", ":").replace("；", ",").replace("，", ",")
    out: dict[str, int] = {}
    for part in text.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, num = part.partition(":")
        name = name.strip()
        if name in dict((s, p) for s, p, _, _ in acquire.STRATA):
            n = _int(num, 0)
            if n > 0:
                out[name] = n
    return out


def build_collection_plan(pages_per_parent: int, star_mix: str) -> list[tuple[str, str, list[int], int]]:
    """按 `VOC_STAR_MIX` 把 `pages_per_parent` 分摊到各星级层。

    硬性口径: **每层至少 1 页, 且总和精确等于 pages_per_parent**(用户口径是"每父体 N 页预算")。
    实现: 先按权重比例向下取整分配, 每层抬到 ≥1; 再把"抬升造成的超发"从页数最多的层里扣回
    (扣到 1 页为止); 若仍超出(层数 > 页数), 只保留前 N 层。层次与顺序取自 `acquire.STRATA`,
    与 pangolinfo `filterByStar` / 卖家精灵 `starList` 的映射保持一致。
    """
    total = max(1, _int(pages_per_parent, DEFAULT_PAGES_PER_PARENT))
    mix = parse_star_mix(star_mix) or parse_star_mix(DEFAULT_STAR_MIX)
    strata = [s for s, _, _, _ in acquire.STRATA]
    weights = {s: mix.get(s, 0) for s in strata}
    if sum(weights.values()) <= 0:
        weights = {s: 1 for s in strata}
    used = [s for s in strata if weights.get(s, 0) > 0]
    if len(used) > total:                     # 页数比"要覆盖的层数"还少 → 只保前 N 层
        used = used[:total]
    alloc = {s: max(1, int(total * weights[s] / max(1, sum(weights[x] for x in used))))
             for s in used}
    guard = 0
    while sum(alloc.values()) > total and guard < 1000:
        guard += 1
        biggest = max(alloc, key=lambda s: (alloc[s], -used.index(s)))
        if alloc[biggest] <= 1:
            break
        alloc[biggest] -= 1
    order = {"critical": 0, "four_star": 1, "five_star": 2}
    while sum(alloc.values()) < total:         # 有余量 → 优先给差评层(问题证据价值最高)
        target = min(alloc, key=lambda s: (order.get(s, 9), -alloc[s]))
        alloc[target] += 1
    return [(s, p, stars, alloc[s]) for s, p, stars, _ in acquire.STRATA if s in alloc]


def remaining_plan(pages_per_parent: int, star_mix: str) -> list[tuple[str, str, list[int], int]]:
    """探针(critical 1 页)已执行后的**剩余**计划: critical 层页数减 1。

    为什么必须减 1: 探针已经抓过 critical 第 1 页, 不减就会重复抓同一页
    —— 每家族多花 1 页积分并产生重复评论(见 `tools/voc/acquire` 的说明与单测)。
    """
    out = []
    for stratum, p_filter, stars, pages in build_collection_plan(pages_per_parent, star_mix):
        left = pages - (acquire.PROBE_PAGES if stratum == acquire.PROBE_STRATUM else 0)
        if left > 0:
            out.append((stratum, p_filter, stars, left))
    return out


def plan_pages(pages_per_parent: int, star_mix: str) -> int:
    """一个父体的实际总页数(应恒等于 pages_per_parent)。"""
    plan = build_collection_plan(pages_per_parent, star_mix)
    return sum(p for _, _, _, p in plan)


def resolve_pangolinfo_creds(config) -> tuple[str, str]:
    """Pangolinfo 凭据：**URL 写死在代码**（`PANGOLIN_MCP_URL` 常量）+ KEY 三级兜底。

    2026-10-12 用户口径：这两个键**不再出现在 `config.yaml` / Web 配置清单**里。

    · **URL** —— 固定服务端点、**不是秘密** → 直接硬编码（不再读 config / 环境变量）。
    · **KEY** —— 本机私有令牌（343 字符 JWT），按 `config → 环境变量 → 本机
      `~/.codex/config.toml`` 顺序解析。**绝不写进仓库**（写进源码一旦分享/备份即泄露，
      且令牌轮换还得改代码）。实测 14 个类目的 config 里这个键**本来就是空的** →
      实际一直靠本机 codex 配置在跑，所以"从配置里拿掉"**功能零变化**。

    读不到 KEY 就返回 `(url, "")`，调用方据此退回卖家精灵。
    """
    url = PANGOLIN_MCP_URL
    key = str(config.get("PANGOLIN_MCP_KEY", "") or "").strip()
    if not key:
        key = str(os.environ.get("PANGOLIN_MCP_KEY", "") or "").strip()
    if key:
        return url, key
    try:
        text = open(CODEX_CFG, encoding="utf-8-sig").read()
    except OSError:
        return url, key
    seg = re.search(r"\[mcp_servers\.pangolinfo\.http_headers\](.*?)(?=\n\[|\Z)", text, re.S)
    auth = (re.search(r'Authorization\s*=\s*"([^"]+)"', seg.group(1) if seg else "")
            or [None, ""])[1]
    if auth:
        key = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
    return url, key


def pango_client(config) -> MCPClient | None:
    """构造 Pangolinfo 客户端; 凭据缺失返回 None(调用方退回卖家精灵)。"""
    url, key = resolve_pangolinfo_creds(config)
    if not url or not key:
        return None
    from core.config import Config
    return MCPClient(Config({"PANGOLIN_MCP_URL": url, "PANGOLIN_MCP_KEY": key,
                             "DRY_RUN": bool(getattr(config, "dry_run", False))}),
                     url_key="PANGOLIN_MCP_URL", key_key="PANGOLIN_MCP_KEY",
                     auth_header="Authorization", auth_scheme="Bearer ")


def ss_client(config) -> MCPClient:
    """构造卖家精灵客户端(兜底源)。"""
    return MCPClient(config)


def parse_pango_envelope(envelope: dict) -> tuple[list[dict], int]:
    """业务信封 → `(评论列表, 消耗积分)`; `code` 非成功值一律抛 `MCPBusinessError`。

    这是 P0 修复点: 不做 code 判定会把"额度不足"误判成"该 ASIN 无评论",
    进而触发子体回退白烧积分。
    """
    code = (envelope or {}).get("code")
    if code not in OK_CODES:
        raise MCPBusinessError("get_amazon_reviews", code,
                               str((envelope or {}).get("message") or ""), envelope)
    rows: list[dict] = []
    for item in ((envelope or {}).get("data") or {}).get("json") or []:
        rows += (item.get("data") or {}).get("results") or []
    return rows, _int((envelope or {}).get("pointCost"), 0)


def parse_pango_total(envelope: dict) -> int:
    """该 ASIN 的评论总数(供覆盖率披露); 取不到按 0(不猜)。"""
    try:
        items = ((envelope or {}).get("data") or {}).get("json") or []
        return int(float((items[0].get("data") or {}).get("totalReviews") or 0))
    except (IndexError, TypeError, ValueError, AttributeError):
        return 0


def parse_ss(envelope: dict) -> list[dict]:
    """卖家精灵 `review` 工具信封 → 评论列表(code 非成功值抛业务错误)。"""
    code = (envelope or {}).get("code")
    if code not in OK_CODES:
        raise MCPBusinessError("review", code, str((envelope or {}).get("message") or ""), envelope)
    return ((envelope or {}).get("data") or {}).get("items") or []


def no_review_scope(receipt: dict) -> str:
    """Recognize explicit successful empties, never inferred zero totals or errors."""
    if receipt.get("status") not in {"ok", "page_ok"} or receipt.get("rows") != 0:
        return ""
    raw = receipt.get("raw") or {}
    if raw.get("code") not in OK_CODES:
        return ""
    data = raw.get("data") or {}
    if receipt.get("source") == "pangolinfo":
        items = data.get("json")
        if items == [] and "review page not found for this asin" in str(raw.get("message", "")).lower():
            return "asin"
        if not isinstance(items, list) or not items:
            return ""
        blocks = [(item.get("data") or {}) for item in items if item.get("code") in OK_CODES]
        if len(blocks) != len(items):
            return ""
        return "stratum" if all(b.get("results") == [] and str(b.get("totalReviews")) in {"0", "0.0"}
                                for b in blocks) else ""
    return "stratum" if data.get("items") == [] and str(data.get("total")) in {"0", "0.0"} else ""


def marketplace_of(site: str) -> str:
    """`VOC_SITE`(如 amz_us) → 卖家精灵 `marketplace`(如 US)。"""
    text = str(site or "").strip()
    m = re.search(r"(?:amz|amazon)?[_\-.]?([a-zA-Z]{2})$", text)
    return m.group(1).upper() if m else "US"


# 中文站点名(SITE) / 亚马逊域名(MARKET) → 地区码，**从公开的 `REGION_PROFILE` 派生**
# （不另抄一份表，免得漂移）。
# ⚠ **首个 2 字母码为准**（`setdefault`）：`REGION_PROFILE` 里有别名
#   （`uk`/`gb` 都是"英国"、`us`/`usa` 都是"美国"），字典推导会**后者胜** →
#   英国会得到 `amz_gb`，而**项目口径是 `UK`**（`core/mcp.py::_SITE_TO_CODE`，有单测保证不漂移）。
_SITE_CODE: dict[str, str] = {}
_MARKET_CODE: dict[str, str] = {}
for _code, _prof in REGION_PROFILE.items():
    if len(_code) != 2:                       # 跳过 usa/gb 这类别名
        continue
    if _prof.get("site"):
        _SITE_CODE.setdefault(_prof["site"], _code.upper())
    if _prof.get("market"):
        _MARKET_CODE.setdefault(_prof["market"], _code.upper())


def voc_site_of(config) -> str:
    """**`VOC_SITE` 由 `SITE` 自适应**（2026-10-12 用户口径：不再单设一个一样的参数）。

    为什么必须自适应（实测真 bug）：`VOC_SITE` 原来是一份**独立的拷贝**，用 Web「新建类目」
    从已有类目 `copy_from` 时会**跟着被复制** → 实测英国站/德国站的类目里 `VOC_SITE` 仍是
    `amz_us`，于是英/德站**一直在抓美国站评论**，而且没有任何提示
    （`SITE`/`MARKET` 都是对的，只有这个副本是旧的）。

    来源顺序：`SITE`（中文站点名，如 英国）→ `MARKET`（如 amazon.co.uk）→ `amz_us` 兜底。
    """
    code = _SITE_CODE.get(str(config.get("SITE", "") or "").strip(), "")
    if not code:
        code = _MARKET_CODE.get(str(config.get("MARKET", "") or "").strip().lower(), "")
    return ("amz_" + code.lower()) if code else DEFAULT_SITE


class Collector:
    """评论采集器: 多源 + 父体优先/子体回退 + 断点续跑 + 积分记账。

    · raw JSONL 是**唯一真值**(append-only), 采集审计 CSV 收尾时从 raw 重建;
    · 断点续跑键 = (source, parent_asin, queried_asin, stratum), 只认 `status == "ok"`;
    · 积分累计超过预算立即停止, 并把 `stopped_reason` 记进摘要。
    """

    def __init__(self, out_dir: str, config, sources: list[str], site: str = DEFAULT_SITE,
                 point_budget: int = DEFAULT_POINT_BUDGET, log=print):
        self.dir = ensure_dir(out_dir)
        self.config = config
        self.sources = [s for s in sources if s in ("pangolinfo", "sellersprite")] or ["pangolinfo"]
        self.site = site or DEFAULT_SITE
        self.marketplace = marketplace_of(self.site)
        self.point_budget = max(0, _int(point_budget, DEFAULT_POINT_BUDGET))
        self.log = log
        self.raw_path = {s: os.path.join(self.dir, "%s_raw.jsonl" % s) for s in ("pangolinfo", "sellersprite")}
        self.rev_path = {s: os.path.join(self.dir, "%s_reviews.jsonl" % s) for s in ("pangolinfo", "sellersprite")}
        self.done = self._done_keys()
        self.points = self._spent_points()
        self.stopped_reason = ""
        self.sources_dead = False        # 所有源额度耗尽(兜底也用完) → 整体收尾
        self.calls = 0
        self.rows = 0
        self.failed_parents = {}
        self.source_phase = "normal"
        self.pango = pango_client(config)
        self.ss = ss_client(config)
        if "pangolinfo" in self.sources and self.pango is None:
            self.log("  [警告] 无 Pangolinfo 凭据(config/环境变量/~/.codex/config.toml 均无) → 仅用卖家精灵")
            if "sellersprite" in self.sources:
                self.sources = [s for s in self.sources if s != "pangolinfo"]
        if "pangolinfo" not in self.sources:
            self.pango = None
        self.ss_pages = self._page_records()
        self.recover_saved_data()
        self.empty_asin_records = {(s, p, t): r for (s, p, t, _), r in self.completed_records.items()
                                   if no_review_scope(r) == "asin"}
        self.redirected_asins = {}
        self._load_redirects()
        if self.redirected_asins:
            self.log("[VOC重定向] 已恢复%d个旧ASIN跳转记录，不自动重试旧ASIN或付费追跳" % len(self.redirected_asins))

    def redirect_notice(self, source, target):
        return getattr(self, "redirected_asins", {}).get((source, self.site, str(target or "").strip().upper()))

    def _remember_redirect(self, notice, *, persist=True):
        with _COLLECT_LOCK:
            if not hasattr(self, "redirected_asins"):
                self.redirected_asins = {}
            key = (notice["source"], notice["site"], notice["queried_asin"])
            self.redirected_asins[key] = notice
            if persist:
                dump_json(os.path.join(self.dir, "ASIN重定向记录.json"), {"version": "voc-redirect-v1",
                          "notices": list(self.redirected_asins.values())})

    def _load_redirects(self):
        from tools.voc.redirects import notice_from_error
        ledger = os.path.join(self.dir, "ASIN重定向记录.json")
        if os.path.exists(ledger):
            with open(ledger, encoding="utf-8-sig") as stream:
                for notice in json.load(stream).get("notices", []):
                    self._remember_redirect(notice, persist=False)
        # Upgrade old runs whose business errors were only saved in task/raw logs.
        tasks = os.path.join(os.path.dirname(self.dir), "ASIN跨源补抓任务.json")
        if os.path.exists(tasks):
            with open(tasks, encoding="utf-8-sig") as stream:
                for task in json.load(stream).get("tasks", []):
                    notice = notice_from_error(task.get("error", ""), task.get("other_source"), task.get("queried_asin"), self.site)
                    if notice:
                        self._remember_redirect(notice, persist=False)
        for source, path in self.raw_path.items():
            for rec in read_jsonl(path):
                if rec.get("status") in {"error", "redirected"}:
                    notice = rec.get("redirect_notice") or notice_from_error(rec.get("error", ""), source, rec.get("queried_asin"), self.site)
                    if notice:
                        self._remember_redirect(notice, persist=False)
        if self.redirected_asins:
            self._remember_redirect(next(iter(self.redirected_asins.values())))

    def _page_records(self):
        return {(r.get("parent_asin"), r.get("queried_asin"), r.get("stratum"), r.get("page")): r
                for r in self._read_raw(self.raw_path["sellersprite"]) if r.get("status") == "page_ok"}

    def recover_saved_data(self):
        """Raw receipts repair a crash between receiving, committing and exporting reviews."""
        for receipt in read_jsonl(os.path.join(self.dir, "MCP完整调用回执.jsonl")):
            context = receipt.get("context") or {}
            source = receipt.get("source")
            if source not in self.raw_path or not all(context.get(k) for k in ("parent_asin", "queried_asin", "stratum")):
                continue
            raw = receipt.get("raw") or {}
            if (receipt.get("status") == "redirected" or raw.get("error")
                    or (isinstance(raw.get("result"), dict) and raw["result"].get("isError"))):
                continue  # Billed business errors are never successful empty pages.
            try:
                if source == "pangolinfo":
                    key = (source, context["parent_asin"], context["queried_asin"], context["stratum"])
                    if key in self.done:
                        continue
                    rows, cost = parse_pango_envelope(raw)
                    rec = dict(context, source=source, status="ok", raw=raw, rows=len(rows), pointCost=cost,
                               recovered_from_transport=True, totalReviews=parse_pango_total(raw))
                    append_jsonl(self.raw_path[source], [rec])
                    self.done.add(key)
                elif context.get("page"):
                    key = (context["parent_asin"], context["queried_asin"], context["stratum"], context["page"])
                    if key in self.ss_pages:
                        continue
                    rows = parse_ss(raw)
                    rec = dict(context, source=source, status="page_ok", raw=raw, rows=len(rows),
                               pages=1, requested_pages=context.get("pages"),
                               pointCost=_int(raw.get("pointCost"), 0), recovered_from_transport=True,
                               totalReviews=_int((raw.get("data") or {}).get("total"), 0))
                    append_jsonl(self.raw_path[source], [rec])
                    self.ss_pages[key] = rec
            except (ValueError, TypeError, AttributeError, MCPBusinessError):
                continue  # Error envelopes remain evidence of failure, never a completed checkpoint.
        incomplete = set()
        for source in self.raw_path:
            records = self._read_raw(self.rev_path[source])  # Preserve legacy multi-page derived records too.
            existing_counts = {}
            for record in records:
                key = (source, record.get("_request_asin"), record.get("_queried_asin"), record.get("_stratum"))
                existing_counts[key] = existing_counts.get(key, 0) + 1
            latest = {}
            for rec in self._read_raw(self.raw_path[source]):
                if rec.get("status") not in {"ok", "page_ok"}:
                    continue
                key = (source, rec.get("parent_asin"), rec.get("queried_asin"), rec.get("stratum"))
                raw = rec.get("raw") or {}
                try:
                    if source == "pangolinfo":
                        rows, _ = parse_pango_envelope(raw)
                    elif rec.get("status") == "page_ok":
                        rows = parse_ss(raw)
                    else:
                        rows = [row for page in rec.get("raw_pages", [raw]) for row in parse_ss(page)]
                except (ValueError, TypeError, AttributeError, MCPBusinessError):
                    if rec.get("status") == "ok":
                        latest[key] = existing_counts.get(key, 0) < _int(rec.get("rows"), 0)
                    continue
                if rec.get("status") == "ok":
                    latest[key] = max(len(rows), existing_counts.get(key, 0)) < _int(rec.get("rows"), 0)
                records.extend(dict(r, _source=source, _request_asin=rec.get("parent_asin"),
                                    _queried_asin=rec.get("queried_asin"), _stratum=rec.get("stratum")) for r in rows)
            incomplete.update(key for key, missing in latest.items() if missing)
            unique = {json.dumps(r, ensure_ascii=False, sort_keys=True, default=_json_default): r for r in records}
            temporary = self.rev_path[source] + ".pending"
            write_jsonl(temporary, list(unique.values()))
            os.replace(temporary, self.rev_path[source])
        self.done = self._done_keys()
        self.done.difference_update(incomplete)
        gap_path = os.path.join(self.dir, "历史回执恢复缺口.json")
        if incomplete or os.path.exists(gap_path):
            dump_json(gap_path, {"pending_keys": [list(key) for key in sorted(incomplete)],
                      "note": "历史raw不足以恢复原声明行数，未视为完成，将重新请求；已保留可恢复原文"})
        self.points = self._spent_points()

    # ---------------- 断点续跑
    def _read_raw(self, path: str) -> list[dict]:
        return read_jsonl(path)

    def _done_keys(self) -> set:
        keys = set()
        completed = {}
        for s, path in self.raw_path.items():
            for r in self._read_raw(path):
                if r.get("status") != "ok":
                    continue
                key = (r.get("source") or s, _norm_asin(r.get("parent_asin")),
                       _norm_asin(r.get("queried_asin")), r.get("stratum"))
                keys.add(key)
                previous = completed.get(key)
                if previous is None or int(r.get("pages") or 0) >= int(previous.get("pages") or 0):
                    completed[key] = r
        # Resume lookups must not parse the full raw history for every parent
        # and stratum (thousands of repeated large-JSON reads on real runs).
        self.completed_records = completed
        return keys

    def _spent_points(self) -> int:
        total = 0
        for s, path in self.raw_path.items():
            for r in self._read_raw(path):
                if r.get("status") == "page_ok" or (r.get("status") == "ok" and not r.get("page_checkpointed")):
                    total += _int(r.get("pointCost"), 0)
        return total

    def budget_left(self) -> int:
        """剩余积分预算(无上限时返回一个很大的数)。"""
        if self.point_budget <= 0:
            return 10 ** 9
        return self.point_budget - self.points

    def exhausted(self) -> bool:
        """积分预算熔断: **仅在显式传入 point_budget>0 时生效**(配置里已无此键, 缺省不限)。"""
        return self.point_budget > 0 and self.points >= self.point_budget

    def all_sources_dead(self) -> bool:
        """所有数据源都已判定额度耗尽(主源+兜底源都用完) → 整体收尾, 不再空转。"""
        return self.sources_dead

    # ---------------- 单次请求
    def _tool(self, client, source, tool, arguments, *, context=None):
        from tools.voc.tasks import DeadlineCalls
        from tools.voc.tasks import worker_count
        from tools.voc.redirects import ASINRedirected, notice_from_error
        notice = self.redirect_notice(source, arguments.get("asin"))
        if notice:
            raise ASINRedirected(tool, notice)
        with _COLLECT_LOCK:
            if not hasattr(self, "throttle"):
                from tools.voc.rate_limit import SourceThrottle
                self.throttle = SourceThrottle(getattr(self, "config", {}))
            if not hasattr(self, "deadlines"):
                cfg = getattr(self, "config", None)
                workers = worker_count(cfg, "VOC_FETCH_WORKERS") if cfg else 1
                self.deadlines = DeadlineCalls(cfg.get("VOC_MCP_TIMEOUT", 120) if cfg else 120,
                                               max_active=workers * 2)
                self.client_pools = {name: SimpleQueue() for name in ("pangolinfo", "sellersprite")}
                for name, initial in (("pangolinfo", getattr(self, "pango", None)),
                                      ("sellersprite", getattr(self, "ss", None))):
                    if initial is not None:
                        self.client_pools[name].put(initial)
        key = json.dumps([source, tool, arguments], sort_keys=True)
        call_context = dict(context or {})
        def invoke():
            # Exclusive lease lasts until the actual network call returns, even
            # after the deadline. A timeout must not share/reset a live session.
            pool = self.client_pools[source]
            try:
                leased = pool.get_nowait()
            except Empty:
                cfg = getattr(self, "config", None)
                leased = (pango_client(cfg) if source == "pangolinfo" else ss_client(cfg)) if cfg else client
            try:
                try:
                    result = self.throttle.call(source, lambda: leased.call_raw(tool, arguments), self.log)
                except MCPBusinessError as exc:
                    notice = notice_from_error(exc, source, arguments.get("asin"), self.site)
                    if notice:
                        self._remember_redirect(notice)
                        append_jsonl(os.path.join(self.dir, "MCP完整调用回执.jsonl"), [{"source": source,
                            "tool": tool, "arguments": arguments, "context": call_context, "status": "redirected",
                            "raw": exc.payload, "redirect_notice": notice, "returned_at": datetime.now().isoformat()}])
                        append_jsonl(self.raw_path[source], [dict(call_context, source=source, status="redirected",
                            error=exc.message, redirect_notice=notice, rows=0, pointCost=0, billing_amount_unknown=notice["billed_notice"])])
                        raise ASINRedirected(tool, notice, exc.payload) from exc
                    raise
                # Inspect business-error envelopes only: a review quoting the
                # marker is untrusted text and must never block future requests.
                if isinstance(result, dict) and result.get("code") not in OK_CODES:
                    notice = notice_from_error(result, source, arguments.get("asin"), self.site)
                    if notice:
                        self._remember_redirect(notice)
                        append_jsonl(os.path.join(self.dir, "MCP完整调用回执.jsonl"), [{"source": source,
                            "tool": tool, "arguments": arguments, "context": call_context, "status": "redirected",
                            "raw": result, "redirect_notice": notice, "returned_at": datetime.now().isoformat()}])
                        append_jsonl(self.raw_path[source], [dict(call_context, source=source, status="redirected",
                            error=str(result.get("message", "")), redirect_notice=notice, rows=0, pointCost=0,
                            billing_amount_unknown=notice["billed_notice"])])
                        raise ASINRedirected(tool, notice, result)
                # Late replies remain recoverable after fallback or process exit.
                if hasattr(self, "dir"):
                    append_jsonl(os.path.join(self.dir, "MCP完整调用回执.jsonl"), [{"source": source,
                        "tool": tool, "arguments": arguments, "context": call_context,
                        "returned_at": datetime.now().isoformat(), "raw": result}])
                return result
            finally:
                pool.put(leased)
        return self.deadlines.call(key, invoke)

    def one_call(self, source: str, parent: str, target: str, stratum: str,
                 p_filter: str, ss_stars: list[int], pages: int) -> dict:
        """执行一次采集请求并落盘 raw + 评论原始行; 返回该次记录。"""
        rec = {"source": source, "parent_asin": parent, "queried_asin": target,
               "is_child": target != parent, "stratum": stratum, "pages": pages,
               "called_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if getattr(self, "identity_repair_variants", None):
            rec["purpose"] = "variant_attribution_repair"
        t0 = time.time()
        with _COLLECT_LOCK:
            self.calls += 1
        if source == "pangolinfo":
            envelope = self._tool(self.pango, source, "get_amazon_reviews",
                                           {"asin": target, "site": self.site, "pageCount": pages,
                                            "filterByStar": p_filter, "sortBy": "recent",
                                            "mediaType": "all_contents"}, context=rec)
            reviews, cost = parse_pango_envelope(envelope)
            rec.update({"status": "ok", "rows": len(reviews), "pointCost": cost,
                        "totalReviews": parse_pango_total(envelope), "raw": envelope})
            rec["empty_scope"] = no_review_scope(rec)
            append_jsonl(self.raw_path[source], [rec])
            append_jsonl(self.rev_path[source],
                         [dict(r, _source="pangolinfo", _request_asin=parent,
                               _queried_asin=target, _stratum=stratum) for r in reviews])
        else:
            got: list[dict] = []
            envelope: dict = {}
            cost = 0
            raw_pages = []
            if not hasattr(self, "ss_pages"):
                self.ss_pages = self._page_records()
            for page in range(1, max(1, pages) + 1):
                page_key = (parent, target, stratum, page)
                saved_page = self.ss_pages.get(page_key)
                if saved_page is None:
                    envelope = self._tool(self.ss, source, "review",
                                            {"marketplace": self.marketplace, "asin": target,
                                             "starList": ss_stars, "page": page, "size": 10}, context=dict(rec, page=page))
                    page_rows = parse_ss(envelope)
                    saved_page = dict(rec, page=page, status="page_ok", raw=envelope, rows=len(page_rows),
                                      pages=1, requested_pages=pages, pointCost=_int(envelope.get("pointCost"), 0),
                                      totalReviews=_int((envelope.get("data") or {}).get("total"), 0))
                    append_jsonl(self.raw_path[source], [saved_page])
                    self.ss_pages[page_key] = saved_page
                    with _COLLECT_LOCK:
                        self.points += saved_page["pointCost"]
                    append_jsonl(self.rev_path[source], [dict(r, _source=source, _request_asin=parent,
                        _queried_asin=target, _stratum=stratum) for r in page_rows])
                envelope = saved_page["raw"]
                got += parse_ss(envelope)
                raw_pages.append(envelope)
                cost += _int((envelope or {}).get("pointCost"), 0)
                if no_review_scope(saved_page):
                    break  # Explicit zero for this star range: no later pages exist.
            rec.update({"status": "ok", "rows": len(got), "pointCost": cost,
                        "totalReviews": _int(((envelope.get("data") or {}).get("total")), 0),
                        "raw": envelope, "raw_pages": raw_pages, "page_checkpointed": True})
            rec["empty_scope"] = no_review_scope(rec)
            append_jsonl(self.raw_path[source], [rec])
            append_jsonl(self.rev_path[source],
                         [dict(r, _source="sellersprite", _request_asin=parent,
                               _queried_asin=target, _stratum=stratum) for r in got])
        rec["elapsed_sec"] = round(time.time() - t0, 1)
        with _COLLECT_LOCK:
            if not rec.get("page_checkpointed"):
                self.points += _int(rec.get("pointCost"), 0)
            self.rows += int(rec.get("rows") or 0)
            self.done.add((source, parent, target, stratum))
            if not hasattr(self, "completed_records"):
                self.completed_records = {}
            self.completed_records[(source, parent, target, stratum)] = rec
            if rec.get("empty_scope") == "asin":
                if not hasattr(self, "empty_asin_records"):
                    self.empty_asin_records = {}
                self.empty_asin_records[(source, parent, target)] = rec
        callback = getattr(self, "on_reviews", None)
        if callable(callback):
            try:
                callback()  # 评论完整落盘之后，才允许后台分析读取。
            except Exception as exc:
                self.log("[VOC方面] 后台通知失败，最终从落盘数据重试: %s" % type(exc).__name__)
        self.log("  ok   %s%s %-9s 页=%d 评论=%d %s%.1fs"
                 % (target, "(子体)" if target != parent else "", stratum, pages, rec["rows"],
                    ("%dpt " % rec["pointCost"]) if rec["pointCost"] else "", rec["elapsed_sec"]))
        if rec.get("empty_scope"):
            self.log(f"  [跳过] {target} {stratum} 明确无评论，已留存回执，不重复抓取该范围")
        return rec

    def log_error(self, source: str, parent: str, target: str, stratum: str, pages: int,
                  error: str) -> None:
        """记一条失败请求(同样落 raw, 断点续跑只跳过 status=ok 的键)。"""
        append_jsonl(self.raw_path[source],
                     [{"source": source, "parent_asin": parent, "queried_asin": target,
                       "is_child": target != parent, "stratum": stratum, "pages": pages,
                       "status": "error", "rows": 0, "pointCost": 0,
                       "called_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "error": str(error)[:300]}])
        self.log("  ERR  %s %s" % (target, str(error)[:140]))
        if "ASIN_REDIRECTED" in str(error):
            return  # Terminal redirect is audited separately, not a network retry.
        with _COLLECT_LOCK:
            if not hasattr(self, "parent_errors"):
                self.parent_errors = set()
            self.parent_errors.add(parent)

    def _collect_call(self, source, parent, target, stratum, p_filter, stars, pages):
        with _COLLECT_LOCK:
            sources = list(self.sources)
            exhausted = set(getattr(self, "exhausted_sources", set()))
            absent = getattr(self, "empty_asin_records", {}).get((source, parent, target))
        if absent is not None:
            return absent
        fallback_receipt = None
        repairing = bool(getattr(self, "identity_repair_variants", None))
        for candidate in dict.fromkeys([source, *sources]):
            # Successful fallback requests also satisfy resume; never charge them again.
            key = (candidate, parent, target, stratum)
            if key in self.done:
                cached = self.completed_records.get(key)
                if cached is not None and int(cached.get("pages") or 0) < pages and not no_review_scope(cached):
                    cached = None
                if cached is not None:
                    if (repairing and candidate != "pangolinfo" and not self._receipt_hits(cached)
                            and not no_review_scope(cached)):
                        fallback_receipt = cached
                        continue
                    return cached
        # Another parent may have exhausted the source while this parent was
        # processing a prior stratum. Saved receipts remain reusable above.
        live = [candidate for candidate in sources if candidate not in exhausted]
        phase = getattr(self, "source_phase", "normal")
        if phase == "pangolinfo":
            live = [s for s in live if s == "pangolinfo" and self.pango is not None]
            if not live:
                from tools.voc.tasks import TaskDeferred
                raise TaskDeferred("Pangolinfo本轮不可用；卖家精灵任务留到最后兜底")
            source = "pangolinfo"
        elif phase == "sellersprite":
            live = [s for s in live if s == "sellersprite" and self.ss is not None]
            if not live:
                from tools.voc.tasks import TaskDeferred
                raise TaskDeferred("最后兜底的卖家精灵不可用，保留断点待重试")
            # A slow primary call may still return a billable result. Do not
            # launch a duplicate cross-source request while it is in flight.
            deadlines = getattr(self, "deadlines", None)
            if deadlines is not None:
                with deadlines.lock:
                    pending = [(json.loads(k), b["done"].is_set()) for k, b in deadlines.active.items()]
                if any(k[0] == "pangolinfo" and k[2].get("asin") == target
                       and k[2].get("filterByStar") == p_filter and not done for k, done in pending):
                    from tools.voc.tasks import TaskDeferred
                    raise TaskDeferred("Pangolinfo同一查询仍在执行，暂不消耗卖家精灵积分")
            source = "sellersprite"
        if not live:
            raise MCPBusinessError("review", 402, "所有数据源额度已耗尽")
        if source in exhausted and live:
            source = live[0]
        if repairing and "pangolinfo" in live:
            source = "pangolinfo"
        candidates = [source]
        if phase == "normal" and source == "pangolinfo" and "sellersprite" in live:
            candidates.append("sellersprite")
        for index, candidate in enumerate(candidates):
            try:
                if candidate == "sellersprite" and fallback_receipt is not None:
                    return fallback_receipt  # Never re-charge an already saved fallback request.
                return self.one_call(candidate, parent, target, stratum, p_filter, stars, pages)
            except MCPBusinessError as exc:
                if acquire.looks_exhausted(exc.message, exc.code):
                    exc.voc_source = candidate
                    raise  # Keep the existing run-wide quota switch and exhaustion guard.
                if index + 1 == len(candidates):
                    exc.voc_source = candidate
                    raise
                self.log_error(candidate, parent, target, stratum, pages, str(exc))
            except Exception as exc:
                if index + 1 == len(candidates):
                    exc.voc_source = candidate
                    raise
                self.log_error(candidate, parent, target, stratum, pages, str(exc))
            self.log("  [兜底] 主源本次调用失败，尝试卖家精灵；评论归属按各自来源保留")

    # ---------------- 单个父体(含子体回退)
    def collect_parent(self, parent: str, children: list[str], pages_per_parent: int,
                       star_mix: str) -> None:
        with _COLLECT_LOCK:
            if not hasattr(self, "parent_errors"):
                self.parent_errors = set()
            self.parent_errors.discard(parent)
        try:
            self._collect_parent(parent, children, pages_per_parent, star_mix)
        except Exception as exc:
            self.log_error(self.sources[0], parent, parent, "parent", pages_per_parent, str(exc))
        finally:
            with _COLLECT_LOCK:
                if not hasattr(self, "failed_parents"):
                    self.failed_parents = {}
                if parent in self.parent_errors:
                    self.failed_parents[parent] = [parent, children, pages_per_parent, star_mix]
                else:
                    self.failed_parents.pop(parent, None)
                dump_json(os.path.join(self.dir, "采集补试队列.json"), {"pending_parents": list(self.failed_parents),
                          "jobs": list(self.failed_parents.values()), "stopped_reason": self.stopped_reason})

    def collect_parents(self, parents, kids, pages_per_parent, star_mix):
        from tools.voc.tasks import worker_count
        workers = worker_count(self.config, "VOC_FETCH_WORKERS")
        # A caller-specified finite budget cannot safely reserve unknown costs in parallel.
        if self.point_budget:
            workers = 1
        self.log(f"[VOC并行] 评论采集：并发{workers}个父体；各父体保持探针与子体回退顺序")
        jobs = iter(dict.fromkeys(parents))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="voc-collect") as pool:
            active = {}
            finished = 0
            def fill():
                while len(active) < workers and not self.exhausted() and not self.all_sources_dead():
                    parent = next(jobs, None)
                    if parent is None:
                        break
                    active[pool.submit(self.collect_parent, parent, kids.get(parent, []),
                                       pages_per_parent, star_mix)] = parent
            fill()
            while active:
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    active.pop(future)
                    future.result()
                    finished += 1
                if finished % 20 < len(completed) or not active:
                    self.log(f"[VOC采集进度] 已检查{finished}个父体（含断点复用），待补试{len(self.failed_parents)}个")
                fill()

    def retry_failed(self):
        for _ in range(max(0, int(self.config.get("VOC_RETRY_ROUNDS", 1)))):
            if self.all_sources_dead() or self.exhausted():
                break
            jobs = list(self.failed_parents.values())
            if jobs:
                self.log("[VOC采集] 首轮父体已处理，补试失败父体%d个" % len(jobs))
                # Retry through the same bounded parent scheduler, retaining per-page keys.
                self.collect_parents([args[0] for args in jobs], {args[0]: args[1] for args in jobs},
                                     jobs[0][2], jobs[0][3])
        self.recover_saved_data()

    def collect_with_fallback(self, parents, kids, pages_per_parent, star_mix):
        """Finish the primary pass and its retries before spending fallback credits."""
        parents = list(dict.fromkeys(parents))
        prior_phase = self.source_phase
        path = os.path.join(self.dir, "采集来源阶段.json")
        def checkpoint(phase, status="running"):
            dump_json(path, {"policy": "pangolinfo_then_retry_then_sellersprite",
                            "phase": phase, "status": status,
                            "pending_parents": list(self.failed_parents)})
        def discard_covered():
            for parent in list(self.failed_parents):
                if self._family_complete(parent, kids.get(parent, []), pages_per_parent, star_mix):
                    self.failed_parents.pop(parent, None)
        try:
            self.source_phase = "pangolinfo"
            checkpoint("pangolinfo")
            if self.pango is not None and "pangolinfo" in self.sources:
                self.log("[VOC来源] 先完成Pangolinfo首轮及失败补试；此阶段不新调用卖家精灵")
                self.collect_parents(parents, kids, pages_per_parent, star_mix)
                discard_covered()
                if "pangolinfo" not in getattr(self, "exhausted_sources", set()):
                    self.retry_failed()
            else:
                self.log("[VOC来源] Pangolinfo未配置，剩余任务进入最后兜底阶段")
                self.failed_parents.update({p: [p, kids.get(p, []), pages_per_parent, star_mix] for p in parents})
            self.recover_saved_data()
            # A failed parent probe may already have been covered by a child.
            # Such a family has no missing range worth spending fallback credit on.
            discard_covered()
            jobs = list(self.failed_parents.values())
            if jobs:
                self.source_phase = "sellersprite"
                checkpoint("sellersprite")
                self.log(f"[VOC来源] Pangolinfo阶段结束，最后用卖家精灵补查{len(jobs)}个未完成父体；成功查询继续复用")
                self.collect_parents([a[0] for a in jobs], {a[0]: a[1] for a in jobs}, pages_per_parent, star_mix)
                self.retry_failed()
            checkpoint("complete", "pending" if self.failed_parents else "complete")
            dump_json(os.path.join(self.dir, "采集补试队列.json"), {"pending_parents": list(self.failed_parents),
                      "jobs": list(self.failed_parents.values()), "stopped_reason": self.stopped_reason})
        except Exception:
            checkpoint(self.source_phase, "pending")
            raise
        finally:
            self.source_phase = prior_phase

    def _family_complete(self, parent, children, pages_per_parent, star_mix):
        plan = build_collection_plan(pages_per_parent, star_mix)
        for target in [parent, *list(children)[:CHILD_TRIES]]:
            covered = []
            for stratum, _, _, pages in plan:
                candidates = [self.completed_records.get((s, parent, target, stratum)) for s in self.sources]
                if target == parent and any(r and no_review_scope(r) == "asin" for r in candidates):
                    return True
                found = next((r for r in candidates if r and
                              (int(r.get("pages") or 0) >= pages or no_review_scope(r))), None)
                covered.append(bool(found and (self._receipt_hits(found) or no_review_scope(found))))
            if covered and all(covered):
                return True
        return False

    def _receipt_hits(self, receipt):
        scope = getattr(self, "identity_repair_variants", None)
        if not scope:
            return bool(receipt.get("rows"))
        scope = getattr(self, "identity_repair_parents", {}).get(receipt.get("parent_asin"), scope)
        source = receipt["source"]
        if source == "pangolinfo":
            rows, _ = parse_pango_envelope(receipt.get("raw") or {})
        else:
            rows = [r for page in receipt.get("raw_pages", [receipt.get("raw") or {}]) for r in parse_ss(page)]
        return any((item := normalize_record(dict(row, _source=source))).get("asin_role") == "attributed"
                   and item.get("variant_asin") in scope for row in rows)

    def repair_attribution(self, catalog, parents, pages_per_parent, star_mix):
        """Recover usable identities, not guesses from requested ASINs or SKU text.

        Triggered only when the final admission scope is empty. Successful primary
        receipts remain resumable; historical unowned fallback receipts cannot
        suppress the missing primary request, but are never fetched again.
        """
        if self.pango is None or "pangolinfo" in getattr(self, "exhausted_sources", set()):
            self.log("[VOC归属补采] 主源不可用，保留兜底原文；不能用查询ASIN猜测变体归属")
            return False
        approved = set(parents)
        kids = {}
        for product in catalog["products"]:
            parent = product.get("parent_asin")
            if parent in approved:
                kids.setdefault(parent, []).append(product["variant_asin"])
        selected = [parent for parent in parents if parent in kids]
        if not selected:
            return False
        self.identity_repair_variants = {p["variant_asin"] for p in catalog["products"]}
        self.identity_repair_parents = {p: set(values) for p, values in kids.items()}
        self.log(f"[VOC归属补采] 准入不足或补采未完；按步骤18目录补查{len(selected)}个父体，主源优先、兜底原文复用")
        path = os.path.join(self.dir, "归属补采记录.json")
        dump_json(path, {"status": "running", "step18_source": catalog["source"], "parents": selected,
                        "note": "仅补可确认变体的来源证据；未放宽步骤18准入"})
        try:
            self.collect_with_fallback(selected, kids, pages_per_parent, star_mix)
        except Exception as exc:
            dump_json(path, {"status": "pending", "error": str(exc)[:300], "parents": selected})
            raise
        finally:
            self.identity_repair_variants = None
            self.identity_repair_parents = {}
        dump_json(path, {"status": "pending" if self.failed_parents else "checked", "step18_source": catalog["source"], "parents": selected,
                        "pending_parents": list(self.failed_parents), "stopped_reason": self.stopped_reason})
        return True

    def _collect_parent(self, parent: str, children: list[str], pages_per_parent: int,
                        star_mix: str) -> None:
        """父体优先 → 子体回退(最多 CHILD_TRIES 个, 命中即停)。

        探针(critical 1 页)是"该 ASIN 到底有没有评论"的判据; 探针命中后才抓剩余计划,
        且**已完成探针的键继续补剩余页**(原缺陷: 探针已完成就整家族 return, 永久漏采)。
        """
        source = self.sources[0]
        plan = build_collection_plan(pages_per_parent, star_mix)
        probe_stratum, probe_filter, probe_stars, probe_pages = plan[0] if plan else \
            (acquire.PROBE_STRATUM, "critical", [1, 2, 3], acquire.PROBE_PAGES)
        targets = [parent] + list(children)[:CHILD_TRIES]
        if getattr(self, "source_phase", "normal") == "sellersprite":
            with _COLLECT_LOCK:
                records = list(self.completed_records.items())
            # Fill the known review-bearing target before probing an earlier
            # failed parent again through the more expensive fallback source.
            targets.sort(key=lambda target: not any(
                self._receipt_hits(r) for (s, p, t, _), r in records
                if p == parent and t == target and s in self.sources))
        for target in targets:
            source = self.sources[0]
            if (getattr(self, "identity_repair_variants", None)
                    and "pangolinfo" not in getattr(self, "exhausted_sources", set())):
                source = "pangolinfo"
            hit = False
            while True:
                if self.exhausted():
                    self.stopped_reason = self.stopped_reason or "point_budget"
                    self.log("  [停止] 积分已达预算 %d(已用 %d)" % (self.point_budget, self.points))
                    return
                if self.all_sources_dead():
                    return               # 全部源额度耗尽: 不再对后续父体空转重试
                probe_key = (source, parent, target, probe_stratum)
                cached_probe = self.completed_records.get(probe_key) if probe_key in self.done else None
                if (cached_probe is not None and int(cached_probe.get("pages") or 0) < probe_pages
                        and not no_review_scope(cached_probe)):
                    cached_probe = None
                if cached_probe is not None:
                    self.log("  跳过探针(已完成): %s ← %s, 继续补剩余页" % (parent, target))
                    hit = self._receipt_hits(cached_probe)
                    if no_review_scope(cached_probe) == "asin":
                        self.log(f"  [跳过] {target} 评论页明确不存在，不再补抓其他星级或探测子体")
                        return
                    if not hit and not no_review_scope(cached_probe):
                        break
                else:
                    try:
                        probe = self._collect_call(source, parent, target, probe_stratum,
                                              probe_filter, probe_stars, probe_pages)
                        source = probe["source"]
                        hit = self._receipt_hits(probe)
                        if no_review_scope(probe) == "asin":
                            self.log(f"  [跳过] {target} 评论页明确不存在，不再补抓其他星级或探测子体")
                            return
                    except MCPBusinessError as exc:
                        failed_source = getattr(exc, "voc_source", source)
                        self.log_error(failed_source, parent, target, probe_stratum, probe_pages,
                                       "code=%s: %s" % (exc.code, exc.message[:200]))
                        if self._switch_source(exc, failed_source):
                            source = self.sources[0]
                            self.log("  → 已切换数据源: %s" % source)
                            continue
                        break
                    except Exception as exc:  # noqa: BLE001  网络/超时等 → 记录后试下一个目标
                        self.log_error(getattr(exc, "voc_source", source), parent, target, probe_stratum, probe_pages,
                                       "%s: %s" % (type(exc).__name__, str(exc)[:200]))
                        break
                    if not hit and not no_review_scope(probe):
                        break
                # 命中(或探针已完成): 抓剩余页
                # Fetch the complete first stratum once. A probe and remaining
                # critical call shared the same resume key and could skip pages.
                for stratum, p_filter, ss_stars, pages in plan[1:]:
                    if self.exhausted():
                        self.stopped_reason = self.stopped_reason or "point_budget"
                        self.log("  [停止] 积分已达预算 %d(已用 %d)" % (self.point_budget, self.points))
                        return
                    try:
                        # A cached SellerSprite probe satisfies only its own
                        # range; new star ranges still prefer the current primary.
                        source = self.sources[0]
                        receipt = self._collect_call(source, parent, target, stratum, p_filter, ss_stars, pages)
                        source = receipt["source"]
                    except MCPBusinessError as exc:
                        failed_source = getattr(exc, "voc_source", source)
                        self.log_error(failed_source, parent, target, stratum, pages,
                                       "code=%s: %s" % (exc.code, exc.message[:200]))
                        if self._switch_source(exc, failed_source):
                            source = self.sources[0]
                            self.log("  → 已切换数据源: %s" % source)
                    except Exception as exc:  # noqa: BLE001
                        self.log_error(getattr(exc, "voc_source", source), parent, target, stratum, pages,
                                       "%s: %s" % (type(exc).__name__, str(exc)[:200]))
                return
            if hit:
                return
        self.log("  --  %s 及其前 %d 个子体均无评论" % (parent, CHILD_TRIES))

    def _switch_source(self, exc: MCPBusinessError, current: str) -> bool:
        with _COLLECT_LOCK:
            return self._switch_source_locked(exc, current)

    def _switch_source_locked(self, exc: MCPBusinessError, current: str) -> bool:
        """业务错误是否应切换数据源: 主源 + 判为额度耗尽 + 还有备用源。

        切换是**全运行生效**的(`self.sources` 轮换, 耗尽源排到末尾);
        当额度耗尽且**已无可用备用源**时, 置 `sources_dead=True` 让调用方整体收尾
        (否则会对着几百个父体逐个重试同一类错误, 白耗时间)。
        """
        if not acquire.looks_exhausted(exc.message, exc.code):
            return False
        if not hasattr(self, "exhausted_sources"):
            self.exhausted_sources = set()
        self.exhausted_sources.add(current)
        rest = [s for s in self.sources if s not in self.exhausted_sources]
        if not rest:
            self.sources_dead = True
            self.stopped_reason = self.stopped_reason or "all_sources_exhausted"
            self.log("  [停止] 所有数据源额度均已耗尽(%s), 用已采集数据出报告" % current)
            return False
        self.sources = rest + [s for s in self.sources if s in self.exhausted_sources]
        return True

    # ---------------- 收尾: 审计与积分
    def raw_rows(self) -> list[dict]:
        out: list[dict] = []
        for s, path in self.raw_path.items():
            out += self._read_raw(path)
        return out

    def rebuild_audit(self, csv_path: str) -> tuple[list[dict], dict]:
        """从 raw 重建采集审计 CSV(raw 是唯一真值, CSV 只是视图)。"""
        rows = [dict(r, status="ok", pages=1) if r.get("status") == "page_ok" else r for r in self.raw_rows()
                if not (r.get("status") == "ok" and r.get("page_checkpointed"))]
        cols = ["source", "parent_asin", "queried_asin", "is_child", "stratum", "pages", "empty_scope",
                "status", "rows", "pointCost", "totalReviews", "elapsed_sec", "called_at", "error"]
        ensure_dir(os.path.dirname(os.path.abspath(csv_path)))
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        pages = sum(_int(r.get("pages"), 0) for r in rows if r.get("status") == "ok")
        cost = sum(_int(r.get("pointCost"), 0) for r in rows if r.get("status") == "ok")
        return rows, {"records": len(rows), "pages": pages, "points": cost}


# ================================================================ 3 归一化 + 去重
def write_dedupe_csv(path: str, stats: dict, rows: list[dict], unique: list[dict],
                     badges: dict) -> str:
    """写去重审计 CSV(两列为主: 指标/值; 尾部附问题库的分布明细)。

    格式对齐 `_voc_test/step2_dedupe.py`, 便于与测试期结果对拍;
    `report.load_dedupe_audit` 只取前两列, 因此尾部明细不影响工作簿。
    """
    from collections import Counter
    src_raw = Counter(r.get("_source") for r in rows)
    src_unique = Counter(s for r in unique for s in (r.get("sources") or []))
    star_cnt = Counter(r.get("star") for r in unique)
    parent_cnt = Counter(r.get("parent_asin") for r in unique)
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["指标", "值"])
        for key, label in (("raw", "原始出现行数"), ("unique", "去重后唯一评论"),
                           ("removed", "移除的重复出现"), ("cross_source_merges", "跨源合并次数"),
                           ("missing_id", "缺 reviewId 的行(卖家精灵全部)"),
                           ("duplicate_keys", "重复键数量(同一评论被多个键命中)")):
            w.writerow([label, stats.get(key, 0)])
        w.writerow(["涉及父ASIN", len(parent_cnt)])
        if "step18_included" in stats:
            w.writerow(["步骤18准入分析评论", stats["step18_included"]])
            w.writerow(["步骤18准入排除评论（未进入LLM）", stats["step18_excluded"]])
        w.writerow([])
        w.writerow(["数据质量标记", "条数"])
        for k, v in badges.items():
            w.writerow([k, v])
        w.writerow([])
        w.writerow(["来源(原始行)", "条数"])
        for k, v in src_raw.items():
            w.writerow([k, v])
        w.writerow([])
        w.writerow(["来源(去重后, 可多源)", "条数"])
        for k, v in src_unique.items():
            w.writerow([k, v])
        w.writerow([])
        w.writerow(["星级(去重后)", "条数"])
        for k in sorted(star_cnt, key=lambda x: str(x)):
            w.writerow([k, star_cnt[k]])
        w.writerow([])
        w.writerow(["父ASIN", "唯一评论数"])
        for k, v in parent_cnt.most_common():
            w.writerow([k, v])
    return path


def normalize_and_dedupe(raw_rows: list[dict], meta: dict) -> tuple[list[dict], dict, list[dict], dict]:
    """归一化 → 去重。返回 `(唯一评论, 去重统计, 归一后全部行, 质量标记)`。"""
    from collections import Counter
    rows = [normalize_record(r, meta) for r in raw_rows]
    unique, stats = dedupe_reviews(rows)
    badges: Counter = Counter()
    for r in unique:
        if r.get("star") not in (1, 2, 3, 4, 5):
            badges["星级非法(0或>5)"] += 1
        if not r.get("parent_asin"):
            badges["缺父ASIN"] += 1
        if not str(r.get("review_body") or "").strip():
            badges["正文为空"] += 1
    return unique, stats, rows, dict(badges)


# ================================================================ 4 编排入口
def run_voc(cat_dir: str, config, *, log=print, traffic_xlsx: str | None = None,
            out_dir: str | None = None, max_parents: int | None = None,
            pages_per_parent: int | None = None, source_priority: str | None = None,
            point_budget: int | None = None, validate: bool = True, meter=None) -> dict:
    """跑完 VOC 全流程并返回摘要 dict。

    产出目录(默认 `data/<CAT>/19_VOC报告/`):
      `<CAT>-VOC报告.xlsx`(V3 14 表) / `分析明细.jsonl` / `评分汇总.csv` /
      `去重审计.csv` / `采集审计.csv` / `VOC流水线摘要.json` / `规范化标签.json` /
      采集源目录 `1_采集原始数据/`(raw JSONL 唯一真值)

    返回键: `cat/out_dir/parents/pages/points/raw_rows/unique_reviews/issues/drivers/
    validation_status/stopped_reason/artifacts`。

    异常语义(交给步骤决定是否跳过):
      · `VOC_ENABLED=False` → `RuntimeError`;
      · 父ASIN 两处都取不到 → `FileNotFoundError`;
      · dry_run 或缺 `LLM_API_KEY` → `VOCAnnotationUnavailable`(**不产出无标注的半成品报告**);
      · `VOC_VALIDATE_STRICT=True` 且校验 FAIL → `RuntimeError`(工作簿仍在, 便于人工看错误)。
    """
    cat = str(config.get("CAT", "") or "").strip() or os.path.basename(os.path.abspath(cat_dir))
    if not config.get("VOC_ENABLED", True):
        raise RuntimeError("VOC_ENABLED=False, 已跳过 VOC 步骤(CAT=%s)" % cat)
    pages_per_parent = _int(pages_per_parent if pages_per_parent is not None
                            else config.get("VOC_PAGES_PER_PARENT", DEFAULT_PAGES_PER_PARENT),
                            DEFAULT_PAGES_PER_PARENT)
    star_mix = str(config.get("VOC_STAR_MIX", DEFAULT_STAR_MIX) or DEFAULT_STAR_MIX)
    # 父体范围: **全量**(用户口径 2026-10-09 "要跑肯定是全量跑") —— 不再读配置上限键;
    # max_parents 仅作为内部 API 参数保留(测试/临时限流用), 缺省 None = 不限制。
    max_parents = _int(max_parents, 0) if max_parents else None
    # 积分预算不再是配置项(用户口径: 靠双源兜底, 不设费用熔断);
    # 仅接受调用方显式传入(内部 API), 缺省 0 = 不限。
    point_budget = max(0, _int(point_budget, 0))
    # ★ 数据源顺序**写死**（2026-10-12 用户口径：就这两个源、没有第三种方式，
    #   留一个配置键只会让人以为还有别的花样）。常量：`DEFAULT_SOURCE_PRIORITY`。
    source_priority = str(source_priority if source_priority is not None
                          else DEFAULT_SOURCE_PRIORITY) or DEFAULT_SOURCE_PRIORITY
    # ★ `VOC_SITE` **由 `SITE` 自适应**（不再单独配置，见 `voc_site_of` 的说明）
    site = voc_site_of(config)
    batch = _int(config.get("VOC_LLM_BATCH", DEFAULT_BATCH), DEFAULT_BATCH)
    workers = _int(config.get("VOC_LLM_WORKERS", DEFAULT_WORKERS), DEFAULT_WORKERS)
    default_out = os.path.join(cat_dir, "19_VOC报告")
    out_dir = ensure_dir(out_dir or default_out)
    raw_dir = ensure_dir(os.path.join(out_dir, "1_采集原始数据"))
    from tools.voc.solutions import load_catalog
    # Compatibility annotation obeys the same admission scope even when the opportunity chain is disabled.
    catalog = load_catalog(cat_dir, cat, site)
    log("[VOC] 已载入步骤18交付：%d个变体 / %d个功能属性字段" % (len(catalog["products"]), len(catalog["columns"])))
    plan = build_collection_plan(pages_per_parent, star_mix)
    log("[VOC] 类目=%s 产出=%s" % (cat, out_dir))
    log("[VOC] 采集计划: 每父体 %d 页(首星级层完整请求作探针；分配 %s); 预算 %d 积分"
        % (sum(p for _, _, _, p in plan),
           "、".join("%s×%d" % (s, p) for s, _, _, p in plan),
           point_budget))
    if sum(p for _, _, _, p in plan) != pages_per_parent:
        log("  [警告] 页数分摊合计 %d ≠ VOC_PAGES_PER_PARENT=%d(星级层数多于页数预算)"
            % (sum(p for _, _, _, p in plan), pages_per_parent))

    # ---- 1 父ASIN + 子体映射
    parents, kids, meta, parents_from = resolve_parents(cat_dir, cat, limit=max_parents,
                                                        traffic_xlsx=traffic_xlsx)
    log("[VOC] 父ASIN %d 个(来源: %s): %s" % (len(parents), parents_from, ", ".join(parents)))

    # ---- 2 采集
    sources = [s.strip() for s in source_priority.replace("；", ",").split(",") if s.strip()]
    col = Collector(raw_dir, config, sources, site=site, point_budget=point_budget, log=log)
    semantic = None
    if config.get("VOC_OPPORTUNITY_ENABLED", True) and not getattr(config, "dry_run", False) and config.get("LLM_API_KEY"):
        from tools.voc.opportunity import IncrementalAnalyzer
        semantic = IncrementalAnalyzer(config, os.path.join(out_dir, "2_需求机会链"), site, log=log, catalog=catalog,
                                       **({"meter": meter} if meter is not None else {}))
        from tools.voc.solution_retry import install_solution_retry
        install_solution_retry(semantic.analyzer)

        # Wait for the collected, deduplicated final snapshot: early callbacks can
        # contain only a page of a variant and would analyze partial groups.
        log("[VOC方面] 采集去重完成后，按归属变体ASIN汇集全部评论分批提取")
        col.on_reviews = lambda: None
    log("[VOC] 数据源优先级: %s(当前 %s); 断点续跑已完成键 %d 个, 已用积分 %d"
        % (",".join(col.sources), col.sources[0], len(col.done), col.points))
    try:
        if hasattr(col, "collect_with_fallback"):
            col.collect_with_fallback(parents, kids, pages_per_parent, star_mix)
        else:
            col.collect_parents(parents, kids, pages_per_parent, star_mix)
            if hasattr(col, "retry_failed"):
                col.retry_failed()
    finally:
        if semantic is not None:
            semantic.close()
    audit_rows, col_stats = col.rebuild_audit(os.path.join(out_dir, "采集审计.csv"))
    log("[VOC] 采集完成: 记录 %d 条 / 成功请求 %d 页 / %d 积分%s"
        % (col_stats["records"], col_stats["pages"], col_stats["points"],
           ("; 已停止(%s)" % col.stopped_reason) if col.stopped_reason else ""))

    # ---- 3 归一化 + 去重
    raw_rows = [r for s in ("pangolinfo", "sellersprite")
                for r in read_jsonl(col.rev_path[s])]
    if not raw_rows:
        raise RuntimeError("未采集到任何评论(父体 %d 个, 审计 %d 条) —— 检查凭据/ASIN/预算"
                           % (len(parents), len(audit_rows)))
    unique, dedupe_stats, norm_rows, badges = normalize_and_dedupe(raw_rows, meta)
    semantic_rows = unique
    asin_recovery = None
    if hasattr(col, "completed_records"):
        from tools.voc.asin_recovery import AsinRecovery
        asin_recovery = AsinRecovery(col, out_dir, catalog, plan)
        if asin_recovery.run(semantic_rows):
            raw_rows = [r for s in ("pangolinfo", "sellersprite") for r in read_jsonl(col.rev_path[s])]
            semantic_rows, dedupe_stats, norm_rows, badges = normalize_and_dedupe(raw_rows, meta)
        asin_recovery.annotate(semantic_rows)
        unique = semantic_rows
    from tools.voc.identity import admit_reviews, write_admission
    unique, excluded, admission_rows, admission = admit_reviews(unique, catalog, site)
    write_admission(raw_dir, excluded, admission_rows, admission)
    if ((not unique or attribution_repair_pending(raw_dir)) and hasattr(col, "repair_attribution")
            and col.repair_attribution(catalog, parents, pages_per_parent, star_mix)):
        # Collection success does not imply identity success. Rebuild from raw
        # receipts after the bounded, resumable primary-source identity repair.
        raw_rows = [r for s in ("pangolinfo", "sellersprite") for r in read_jsonl(col.rev_path[s])]
        semantic_rows, dedupe_stats, norm_rows, badges = normalize_and_dedupe(raw_rows, meta)
        if asin_recovery is not None:
            asin_recovery.annotate(semantic_rows)
        unique, excluded, admission_rows, admission = admit_reviews(semantic_rows, catalog, site)
        write_admission(raw_dir, excluded, admission_rows, admission)
        audit_rows, col_stats = col.rebuild_audit(os.path.join(out_dir, "采集审计.csv"))
    if asin_recovery is not None:
        audit_rows, col_stats = col.rebuild_audit(os.path.join(out_dir, "采集审计.csv"))
    dedupe_stats.update(step18_included=len(unique), step18_excluded=len(excluded))
    dedupe_csv = write_dedupe_csv(os.path.join(out_dir, "去重审计.csv"),
                                  dedupe_stats, norm_rows, semantic_rows, badges)
    log("[VOC] 归一+去重: 原始 %d 行 → 唯一 %d 条(移除 %d, 跨源合并 %d); 质量标记: %s"
        % (dedupe_stats["raw"], dedupe_stats["unique"], dedupe_stats["removed"],
           dedupe_stats["cross_source_merges"], badges or "无"))
    log("[VOC准入] 步骤18匹配%d条，排除%d条；排除项不进入方面提取或兼容标注" % (len(unique), len(excluded)))
    if not unique:
        dump_json(os.path.join(out_dir, "VOC流水线摘要.json"), {"status": "no_eligible_reviews", "unique_reviews": 0,
            "raw_unique_reviews": len(semantic_rows), "admission_summary": admission,
            "asin_recovery": asin_recovery.summary if asin_recovery else {}})
        raise RuntimeError("步骤18准入后无可分析评论，已保存排除原因与原文，未调用模型")

    # ---- 4 标注 + 归一
    if bool(getattr(config, "dry_run", False)) or not str(config.get("LLM_API_KEY", "") or "").strip():
        raise VOCAnnotationUnavailable(
            "CAT=%s: %s(dry_run=%s)" % (cat, NO_LLM_HINT, bool(getattr(config, "dry_run", False))))
    opportunity_summary = semantic.finalize(semantic_rows) if semantic is not None else {"status": "disabled"}
    labels, ann_stats = annotate.run_annotation(
        unique, config, batch=batch, workers=workers,
        cache_path=os.path.join(out_dir, "分析明细.jsonl"), log=log,
        **({"meter": meter} if meter is not None else {}))
    annotate.canonicalize(labels, unique, config,
                          cache_path=os.path.join(out_dir, "规范化标签.json"), log=log,
                          **({"meter": meter} if meter is not None else {}))
    annotate.write_annotations(os.path.join(out_dir, "分析明细.jsonl"), labels)
    log("[VOC] 标注完成: %d 条有标签(失败批 %d, 影响 %d 条)"
        % (sum(1 for x in labels if x), ann_stats["failed_batches"], ann_stats["failed_reviews"]))

    # ---- 5 聚合 + 工作簿 + 校验
    agg = report.aggregate(unique, labels)
    agg["annotation"] = ann_stats
    agg["admission"] = admission
    coverage = report.load_audit(list(col.raw_path.values()))[1]
    xlsx = os.path.join(out_dir, "%s-VOC报告.xlsx" % cat)
    report.build_workbook(unique, labels, agg, audit_rows, dedupe_stats, coverage, xlsx)
    log("[VOC] 工作簿: %s" % xlsx)
    report.write_summary_csv(os.path.join(out_dir, "评分汇总.csv"), agg)
    val = {"returncode": None, "status": "SKIP", "json": None, "stdout": ""}
    if validate:
        val = report.validate_workbook(xlsx, report.DEFAULT_VALIDATOR,
                                       json_path=os.path.join(out_dir, "校验结果.json"))
        vj = val.get("json") or {}
        log("[VOC] 校验: status=%s errors=%d warnings=%d returncode=%s"
            % (val["status"], len(vj.get("errors") or []), len(vj.get("warnings") or []),
               val["returncode"]))

    artifacts = [xlsx, os.path.join(out_dir, "分析明细.jsonl"),
                 os.path.join(out_dir, "评分汇总.csv"), dedupe_csv,
                 os.path.join(out_dir, "采集审计.csv"),
                 os.path.join(out_dir, "规范化标签.json")]
    artifacts.extend(os.path.join(out_dir, name) for name in ("ASIN缺失评论明细.csv", "ASIN跨源补抓任务.json", "ASIN跨源补抓结果.json")
                     if os.path.exists(os.path.join(out_dir, name)))
    for src in ("pangolinfo", "sellersprite"):
        if os.path.exists(col.rev_path[src]):
            artifacts.append(col.rev_path[src])
            artifacts.append(col.raw_path[src])
    if val.get("json") is not None:
        artifacts.append(os.path.join(out_dir, "校验结果.json"))
    if semantic is not None:
        from pathlib import Path
        chain_dir = Path(out_dir) / "2_需求机会链"
        artifacts.extend(str(path) for path in sorted(chain_dir.glob("*.csv")))
        artifacts.extend(str(path) for path in sorted(chain_dir.glob("步骤18标准化交付-*.xlsx")))
        artifacts.extend(str(chain_dir / name) for name in ("chain.json", "需求机会链摘要.json", "步骤18关联来源.json", "阶段断点.json"))

    summary = {
        "cat": cat,
        "out_dir": out_dir,
        "parents": len(parents),
        "parents_from": parents_from,
        "pages": col_stats["pages"],
        "points": col_stats["points"],
        "raw_rows": dedupe_stats["raw"],
        "unique_reviews": len(unique),
        "raw_unique_reviews": dedupe_stats["unique"],
        "excluded_reviews": len(excluded),
        "admission_summary": admission,
        "asin_recovery": asin_recovery.summary if asin_recovery else {},
        "mcp_cost_incomplete": any(n.get("billed_notice") and n.get("billing_amount") is None
                                   for n in getattr(col, "redirected_asins", {}).values()),
        "issues": len(agg["neg_rows"]),
        "drivers": len(agg["pos_rows"]),
        "validation_status": val["status"],
        "stopped_reason": col.stopped_reason,
        "call_records": col_stats["records"],
        "source_used": col.sources[0],
        "annotation_failed_reviews": ann_stats["failed_reviews"],
        "collection_pending_parents": list(getattr(col, "failed_parents", {})),
        "opportunity_chain": opportunity_summary,
        "safety_evidence": len(agg["safety"]),
        "neutral3": len(agg["neutral3"]),
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "artifacts": artifacts,
    }
    if val.get("json") is not None:
        summary["validation_errors"] = val["json"].get("errors") or []
        summary["validation_warnings"] = val["json"].get("warnings") or []
    dump_json(os.path.join(out_dir, "VOC流水线摘要.json"), summary)
    if summary["mcp_cost_incomplete"]:
        log("[VOC计费] 服务端声明部分重定向请求已计费但未返回金额；积分合计仅含明确数值，详见ASIN重定向记录.json")
    artifacts.append(os.path.join(out_dir, "VOC流水线摘要.json"))
    if summary["collection_pending_parents"]:
        raise RuntimeError("VOC采集仍有%d个父体待补试；其他成功数据已分析并保留，请续跑步骤19" % len(summary["collection_pending_parents"]))
    if summary["annotation_failed_reviews"]:
        raise RuntimeError("VOC兼容标注仍有%d条评论待补试；成功数据已保留，请续跑步骤19" % summary["annotation_failed_reviews"])
    if summary["asin_recovery"].get("pending_tasks"):
        raise RuntimeError("VOC仍有%d个ASIN缺失跨源补抓任务待重试；其他成功分析已保留，请续跑步骤19" % summary["asin_recovery"]["pending_tasks"])
    if validate and val["status"] not in ("PASS", "SKIP") and config.get("VOC_VALIDATE_STRICT", True):
        raise RuntimeError("VOC 工作簿未通过 --strict-v3 校验(%s): %s"
                           % (val["status"], "; ".join((val.get("json") or {}).get("errors") or [])[:300]))
    return summary


__all__ = ["VOCAnnotationUnavailable", "run_voc", "resolve_parents", "resolve_pangolinfo_creds",
           "pango_client", "ss_client", "Collector", "build_collection_plan", "remaining_plan",
           "plan_pages", "parse_star_mix", "parse_pango_envelope", "parse_pango_total", "parse_ss",
           "normalize_and_dedupe", "write_dedupe_csv", "traffic_path", "dump_json", "write_jsonl",
           "append_jsonl", "marketplace_of"]
