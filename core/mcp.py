"""卖家精灵 MCP v2 客户端 (FR-04/FR-05/FR-06/FR-14)。

卖家精灵 MCP v1(https://mcp.sellersprite.com/mcp) 已弃用，统一使用 v2
(https://mcp.sellersprite.com/v2/mcp):
- keyword_miner     -> keyword.related.list(根词扩展) / keyword.batch.info(批量验证, <=20词/批)
- keyword_trend     -> keyword.trend: 关键词历史月度趋势(searchVolume/purchaseVolume/
                       purchaseRate/yearlyGrowth/chainGrowth/threeMonthGrowth)
- aba_weekly_bid    -> keyword.aba.trend(granularity=WEEK): ABA 排名+周搜索量
                       + keyword.batch.info: CPC/bidMin/bidMax
- google_trend      -> keyword.google.trend(gprop=WEB): 周度指数(按月均值聚合/周度透传)
- competitor_lookup -> asin.list: 竞品配送方式/上架时间(cursor 分页)

协议: 标准 MCP Streamable HTTP (2025-03-26), 必须先 initialize 握手再 tools/call;
请求头 secret-key 鉴权; v2 tools/list 与 tools/call 返回 SSE(text/event-stream) 帧,
内层直接为业务 JSON 对象(错误返回 isError/error 对象)。
单接口失败不影响其他 (FR-05)。dry_run 模式返回 MockMCP 数据。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .mock import MockMCP
from .run_lock import _try_lock, _unlock

_MCP_LOCK_DIR = Path(__file__).resolve().parent.parent / "data" / ".locks"
_MCP_THREAD_LOCKS: dict[str, threading.RLock] = {}
_MCP_THREAD_LOCKS_GUARD = threading.Lock()
DEFAULT_MCP_MIN_INTERVAL = 0.25

_MCP_CALL_LOG_LOCK = threading.Lock()
_MCP_CALL_LOGGER = None


def _get_mcp_call_logger():
    """返回 MCP 调用流水日志记录器（轮转写入 logs/mcp_calls.log，不记录密钥）。"""
    global _MCP_CALL_LOGGER
    env_path = str(os.environ.get("PIPELINE_MCP_LOG_PATH") or "").strip()
    log_path = Path(env_path) if env_path else (Path(__file__).resolve().parent.parent / "logs" / "mcp_calls.log")
    with _MCP_CALL_LOG_LOCK:
        if _MCP_CALL_LOGGER is not None and getattr(_MCP_CALL_LOGGER, "_target_path", None) == str(log_path):
            return _MCP_CALL_LOGGER
        import logging
        from logging.handlers import RotatingFileHandler
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger(f"pipeline.mcp.calls.{hashlib.sha1(str(log_path).encode('utf-8')).hexdigest()[:8]}")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        for h in list(lg.handlers):
            try:
                h.close()
            except Exception:
                pass
            lg.removeHandler(h)
        fh = RotatingFileHandler(str(log_path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(fh)
        lg._target_path = str(log_path)
        _MCP_CALL_LOGGER = lg
        return lg


def _record_mcp_call(
    *,
    endpoint: str,
    tool: str,
    ok: bool,
    duration_ms: int,
    attempts: int,
    cat: str = "",
    status_code: int | None = None,
    error: str = "",
) -> None:
    """将单次 MCP 工具调用记入 JSONL 流水日志（永不记录密钥或原始敏感头）。"""
    try:
        clean_ep = str(endpoint or "").split("?", 1)[0]
        entry: dict[str, Any] = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cat": str(cat or ""),
            "endpoint": clean_ep,
            "tool": str(tool or ""),
            "ok": bool(ok),
            "duration_ms": int(max(0, duration_ms)),
            "attempts": int(max(1, attempts)),
        }
        if status_code is not None:
            entry["status_code"] = int(status_code)
        if error:
            entry["error"] = str(error)[:200]
        _get_mcp_call_logger().info(json.dumps(entry, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — 日志写入失败绝不影响业务调用
        pass


def _mcp_lock_dir() -> Path:
    env = str(os.environ.get("PIPELINE_MCP_LOCK_DIR") or os.environ.get("PIPELINE_AMZ_LOCK_DIR") or "").strip()
    return Path(env) if env else _MCP_LOCK_DIR


@contextmanager
def _mcp_key_gate(url: str, key: str, *, min_interval: float = DEFAULT_MCP_MIN_INTERVAL, wait_seconds: float = 300.0):
    """跨进程+跨线程按 (MCP URL, Key) 串行化请求并保证最小请求间隔，防止多类目同时跑步骤4/5/6/14 触发并发 429/连接重置。"""
    token = f"{url}|{key or 'anon'}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    with _MCP_THREAD_LOCKS_GUARD:
        tlock = _MCP_THREAD_LOCKS.setdefault(digest, threading.RLock())
    with tlock:
        ldir = _mcp_lock_dir()
        try:
            ldir.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield
            return
        lock_file = ldir / f"mcp-key-{digest}.lock"
        ts_file = ldir / f"mcp-key-{digest}.ts"
        fh = None
        locked = False
        deadline = time.monotonic() + max(1.0, float(wait_seconds))
        try:
            fh = open(lock_file, "r+b" if lock_file.is_file() else "w+b")
            while True:
                res = _try_lock(fh)
                if res is True or res is None:
                    locked = res is True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if min_interval > 0:
                now = time.time()
                try:
                    if ts_file.is_file():
                        last_ts = float(ts_file.read_text(encoding="utf-8").strip() or 0.0)
                        gap = min_interval - (now - last_ts)
                        if 0 < gap <= min_interval + 5.0:
                            time.sleep(gap)
                except (OSError, ValueError):
                    pass
            try:
                yield
            finally:
                if min_interval > 0:
                    try:
                        ts_file.write_text(f"{time.time():.6f}", encoding="utf-8")
                    except OSError:
                        pass
        finally:
            if fh is not None:
                if locked:
                    _unlock(fh)
                try:
                    fh.close()
                except OSError:
                    pass


class MCPBusinessError(RuntimeError):
    """MCP 业务层错误(code != OK / isError)。

    携带完整信封, 供调用方区分"额度耗尽/鉴权失效"等可恢复情形与真正的参数错误:
      · payload: 完整业务信封(含 code/message/pointCost/data)
      · code/message: 便于做关键词判定(如 积分不足 → 切换另一个数据源)
    继承 RuntimeError, 保持与既有 `except RuntimeError` 捕获逻辑兼容。
    """

    def __init__(self, tool: str, code: Any, message: str, payload: Any = None):
        self.tool = tool
        self.code = code
        self.message = str(message)
        self.payload = payload
        super().__init__(f"MCP {tool} 业务错误(code={code}): {self.message[:300]}")


def parse_mcp_json(resp: httpx.Response) -> dict:
    """解析 MCP 响应: 兼容**纯 JSON** 与 **SSE 帧**(`text/event-stream`, `data: {...}`)。

    背景(2026-10-09 实测): 卖家精灵 MCP 返回纯 JSON; 而 Pangolinfo MCP 返回 SSE 帧,
    原实现直接 `resp.json()` 会抛 JSONDecodeError。

    实现顺序: 非 SSE(或拿不到 content-type)时先走 `resp.json()` —— 保持既有行为与
    单测假响应(只实现 json())兼容; 失败或判为 SSE 时再按 `data:` 帧解析。
    """
    headers = getattr(resp, "headers", None) or {}
    try:
        ctype = str(headers.get("content-type") or "").lower()
    except Exception:  # noqa: BLE001  假响应/异常头部
        ctype = ""
    if "text/event-stream" not in ctype:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001  落到 SSE/文本解析
            pass
    text = getattr(resp, "text", "") or ""
    if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:", "id:", ":")):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue
        raise ValueError("SSE 响应中没有可解析的 data JSON: " + text[:300])
    return json.loads(text)

# 2026-09-09: 挖词逐词调用 MCP, 单词走 3 接口。单请求超时调小避免"看起来卡死";
# 但**重试不能过小**——卖家精灵 MCP 会偶发 SSL 连接中断(UNEXPECTED_EOF_WHILE_READING)或并发 429,
# 需多次重试退避才能命中成功, 否则批量验证/趋势数据易丢。
MCP_HTTP_TIMEOUT = 25
MCP_CALL_RETRIES = 5
# 握手/密钥预检(_initialize / check)的重试次数与退避: 与 _call 同等待遇
# (2026-09-15: 步骤6 因预检单次 POST 撞上瞬时 SSL EOF 而整步失败)
MCP_HTTP_RETRIES = 4

_SITE_TO_CODE = {
    "美国": "US",
    "英国": "UK",
    "日本": "JP",
    "德国": "DE",
    "法国": "FR",
    "意大利": "IT",
    "西班牙": "ES",
    "加拿大": "CA",
    "印度": "IN",
    "墨西哥": "MX",
    "巴西": "BR",
    "澳大利亚": "AU",
    "阿联酋": "AE",
}


def _marketplace(site: str) -> str:
    """把配置的站点名(如 美国)映射为 MCP 的 country code(如 US)。

    容错(2026-09-15): 原实现只认**精确**站点名/两字国家码, 于是 `德国站`、`德`、`Germany`、
    `Amazon.de` 这类常见写法会**静默回落成 US** —— 用户以为在挖德国站, 实际拿到美国库数据。
    现在按"归一化 + 逐步宽松"匹配:
      ① 去掉空白/前后缀(站/亚马逊/amazon/域名后缀);
      ② 命中站点名(美国/德国/…)或两字国家码(us/de/uk/…)→ 对应 code;
      ③ 仍认不出 → 返回 "US"(保持向后兼容), 但调用方会另行告警(见 MCPClient 启动日志)。
    """
    site = str(site or "").strip()
    if not site:
        return "US"
    # ①a 域名写法先取国家码(必须在"删点"之前做, 否则 Amazon.co.uk 会被压成 Amazoncouk,
    #     域名信息就丢了 —— 2026-09-15 踩过这个坑): 末段/末两段任一命中两字国家码即采用。
    parts = [p for p in re.split(r"[\s]+", site) if p]
    for token in ([site] + parts):
        for seg in reversed(re.split(r"[\.\-_/]+", token)):
            if seg.upper() in _SITE_TO_CODE.values():
                return seg.upper()
    # ①b 归一化: 去空格/点/连字符, 去"站/亚马逊/amazon/商城"等前后缀
    norm = re.sub(r"[\s\.\-_]+", "", site)
    norm = re.sub(r"^(亚马逊|amazon)", "", norm, flags=re.I)
    norm = re.sub(r"(站|商城|站点)$", "", norm)
    # ② 精确 → 归一化后精确 → 两字国家码(大小写不敏感)
    if site in _SITE_TO_CODE:
        return _SITE_TO_CODE[site]
    for name, code in _SITE_TO_CODE.items():
        if norm == re.sub(r"[\s\.\-_]+", "", name):
            return code
    code = norm.upper()
    if code in _SITE_TO_CODE.values():
        return code
    # ③ 认不出(拼写错误/英文名/未收录站点) → US, 由调用方告警提示
    return "US"


def is_known_site(site: str) -> bool:
    """该站点写法能否被识别(供调用方对"认不出而回落 US"发出告警)。"""
    site = str(site or "").strip()
    if not site:
        return False
    return _marketplace(site) != "US" or site.strip() in ("美国", "US", "us", "usa", "USA")


SS_MCP_V2_URL = "https://mcp.sellersprite.com/v2/mcp"
_SS_MCP_V1_URLS = {
    "https://mcp.sellersprite.com/mcp",
    "http://mcp.sellersprite.com/mcp",
}


def normalize_ss_mcp_url(url: str) -> str:
    """卖家精灵 MCP v1 已弃用：旧 v1 端点自动升级为 v2 端点。"""
    s = str(url or "").strip()
    if s.rstrip("/").lower() in _SS_MCP_V1_URLS:
        return SS_MCP_V2_URL
    return s


class MCPClient:
    def __init__(self, config: Config, *, url_key: str = "SS_MCP_URL",
                 key_key: str = "SS_MCP_KEY", auth_header: str = "secret-key",
                 auth_scheme: str = ""):
        """MCP 客户端。

        默认指向卖家精灵 v2(SS_MCP_URL/SS_MCP_KEY + secret-key 头);
        通过 url_key/key_key/auth_header/auth_scheme 可指向其它 MCP 服务, 例如:
          MCPClient(cfg, url_key="PANGOLIN_MCP_URL", key_key="PANGOLIN_MCP_KEY",
                    auth_header="Authorization", auth_scheme="Bearer ")
        """
        self.config = config
        self._mock = MockMCP() if config.dry_run else None
        self._session_id: str | None = None
        self._client: httpx.Client | None = None
        self._url_key = url_key
        self._key_key = key_key
        self._auth_header = auth_header
        self._auth_scheme = auth_scheme

    def _url(self) -> str:
        url = str(self.config.get(self._url_key, "") or "").strip()
        if not url:
            raise RuntimeError(f"{self._url_key} 未配置")
        if self._url_key == "SS_MCP_URL":
            url = normalize_ss_mcp_url(url)
        return url

    # ---------------- 连接复用(2026-09-15)
    def _http(self) -> httpx.Client:
        """复用同一个 httpx.Client(**连接池 + TLS 长连接**), 而不是每个请求新建。

        为什么必须复用(实测 2026-09-15, 用户报"mcp 总是连接不上"):
          卖家精灵 MCP 端点解析出 **16 个 A 记录**(CDN 池), 其中部分节点的 TLS 握手会被
          中途 EOF —— 每次**新建连接**都要重新握手, 实测 20 次全新连接里 **1 次失败(5%)**,
          且 20 次耗时 86.5s; 而**复用同一连接**时 20 次 **0 失败、仅 10.1s**(快 8.6 倍) ——
          因为整批只握手一次, 之后都走已建立的 TLS 长连接。
          改动前: `_post_json`/`_call` 每个请求都 `httpx.Client(...)` 新建, 且 `_call()` 还会
          在**每次工具调用前**重新 initialize → 步骤6 有 N 个关键词就要 ~2N 次握手,
          按 5%/次算, N=20 时"至少失败一次"的概率约 87% —— 这就是"总是连接不上"的由来。
        """
        if self._client is None:
            self._client = httpx.Client(timeout=MCP_HTTP_TIMEOUT)
        return self._client

    def _reset_connection(self, reason: str = "") -> None:
        """丢弃当前连接与会话(连接类错误后调用): 让重试**重新握手**而不是复用坏连接。"""
        c, self._client = self._client, None
        self._session_id = None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        if reason:
            _mcp_log(f"连接/会话已重置({reason}), 下次请求重新握手")

    def close(self) -> None:
        """关闭底层连接(进程结束/步骤收尾时尽力而为)。"""
        self._reset_connection()

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        key = str(self.config.get(self._key_key, "") or "")
        if key:
            headers[self._auth_header] = (self._auth_scheme + key) if self._auth_scheme else key
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post_json(self, body: dict, *, retries: int = MCP_HTTP_RETRIES, what: str = "MCP 请求") -> httpx.Response:
        """POST 一次 MCP 请求, **对连接类瞬时错误重试**(2026-09-15 用户报障修复)。

        背景: 步骤6(站内月度趋势)开头的密钥预检走 `check()` → `_initialize()`, 而这两处
        原来**只有一次** POST —— 一次瞬时 TLS 断开就让整步 FAILED。实测: 19:41:59 步骤05 刚用
        同一个 MCP 成功跑完(耗时 10 分 49 秒), 19:42:11 步骤06 的预检就撞上
        `[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol`, 于是
        "卖家精灵 MCP 连接失败" → 流水线提前终止。`_call()` 一直有 3 次重试
        (`httpx.HTTPError`/`OSError`, 而 `ssl.SSLError` 正是 `OSError` 子类), 这里补齐同类待遇。

        重试策略: 连接类错误(含 SSL/超时/协议中断)重试; **4xx 立即抛**(请求或鉴权问题, 重试无意义);
        5xx 视为瞬时, 照常重试。每次重试在流水线日志留一行, 便于事后判断是"网络抖动"还是"服务端故障"。
        """
        url = self._url()
        key = str(self.config.get(self._key_key, "") or "")
        tries = max(1, int(retries))
        last: Exception | None = None
        for attempt in range(tries):
            try:
                with _mcp_key_gate(url, key):
                    resp = self._http().post(url, headers=self._headers(), json=body)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    raise                       # 4xx: 参数/鉴权问题, 重试没意义
                last = exc
                self._reset_connection(f"HTTP {exc.response.status_code}")
            except (httpx.HTTPError, OSError) as exc:
                last = exc                      # 连接/SSL/超时等瞬时错误
                self._reset_connection(type(exc).__name__)
            if attempt < tries - 1:
                wait = 2 + attempt * 3
                _mcp_log(f"{what} 第 {attempt + 1}/{tries} 次失败({type(last).__name__}: "
                         f"{str(last)[:120]}), {wait}s 后重试")
                time.sleep(wait)
        raise last if last is not None else RuntimeError(f"{what} 失败")

    def _initialize(self) -> None:
        """MCP initialize 握手(标准 MCP, 2025-03-26)。

        卖家精灵 MCP 为 Streamable HTTP: 必须先 initialize 才能 tools/call
        (缺少握手直接 tools/call 会返回 400)。

        **已有会话则跳过**(2026-09-15): 服务端在 initialize 响应里给出 `Mcp-Session-Id`,
        后续请求带上它即可(`_headers()` 已带) —— 原先每次工具调用前都重新握手, 一次调用
        变两次握手, 把"CDN 节点偶发 TLS EOF"的暴露面翻倍(实测 5%/次)。
        会话失效时由调用方 `_reset_connection()` 清空, 下次自然会重新握手。
        """
        if self._session_id:
            return
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "dsh-pipeline", "version": "1.0"},
            },
        }
        resp = self._post_json(body, what="MCP initialize")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

    def check(self) -> list[str]:
        """校验密钥并返回可用工具列表(卖家精灵 MCP v2)。"""
        if self._mock is not None:
            return [
                "keyword.trend",
                "keyword.aba.trend",
                "keyword.google.trend",
                "keyword.related.list",
                "keyword.batch.info",
            ]
        self._initialize()
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = self._post_json(body, what="MCP tools/list")
        data = parse_mcp_json(resp)
        tools = []
        result = data.get("result") or {}
        for item in result.get("tools") or []:
            name = item.get("name")
            if name:
                tools.append(name)
        return tools

    def _call(self, tool: str, arguments: dict, retries: int = MCP_CALL_RETRIES) -> Any:
        """调用 MCP 工具, 解析 content 包装, 返回业务 data(失败重试)。

        兼容卖家精灵 MCP v1({code:"OK", data:...}) 与 v2(直接返回业务 dict, 错误返回 error 对象)。
        """
        if self._mock is not None:
            return None
        url = self._url()
        key = str(self.config.get(self._key_key, "") or "")
        cat_name = str(getattr(self.config, "cat", "") or "")
        t0 = time.monotonic()
        last_err: str = ""
        last_status: int | None = None
        used_attempts = 0
        for attempt in range(retries):
            used_attempts = attempt + 1
            try:
                self._initialize()          # 已有会话则跳过(见其 docstring), 不再每次工具调用都握手
                body = {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                    "id": 2,
                }
                with _mcp_key_gate(url, key):
                    resp = self._http().post(url, headers=self._headers(), json=body)
                last_status = getattr(resp, "status_code", None)
                resp.raise_for_status()
                data = parse_mcp_json(resp)
                if "error" in data and data["error"]:
                    raise MCPBusinessError(tool, data["error"].get("code"), str(data["error"]), data)
                result = data.get("result") or {}
                payload = _parse_payload(result)
                if result.get("isError"):
                    err_obj = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(err_obj, dict) and err_obj.get("retryable") and attempt < retries - 1:
                        last_err = f"{err_obj.get('code')}: {str(err_obj.get('message') or '')[:120]}"
                        time.sleep(2 + attempt * 3)
                        continue
                    texts = _content_texts(result)
                    raise MCPBusinessError(tool, "isError", f"执行失败: {texts[:300]}", data)
                if isinstance(payload, dict) and payload.get("error"):
                    err_obj = payload["error"]
                    if isinstance(err_obj, dict) and err_obj.get("retryable") and attempt < retries - 1:
                        last_err = f"{err_obj.get('code')}: {str(err_obj.get('message') or '')[:120]}"
                        time.sleep(2 + attempt * 3)
                        continue
                    err_code = err_obj.get("code") if isinstance(err_obj, dict) else "error"
                    err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                    raise MCPBusinessError(tool, err_code, str(err_msg or err_obj), payload)
                if isinstance(payload, dict) and payload.get("code") not in (None, "OK", 200, 0):
                    raise MCPBusinessError(tool, payload.get("code"), str(payload.get("message", payload)), payload)
                _record_mcp_call(
                    endpoint=url, tool=tool, ok=True,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    attempts=used_attempts, cat=cat_name, status_code=last_status,
                )
                return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                last_status = status_code
                body_preview = (exc.response.text or "")[:120] if exc.response is not None else ""
                last_err = f"HTTPStatusError({status_code}): {body_preview or str(exc)[:120]}"
                self._reset_connection(f"HTTP {status_code}")
                if status_code == 429:
                    time.sleep(5 + attempt * 5)
                else:
                    time.sleep(2 + attempt * 3)
            except (httpx.HTTPError, OSError) as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                # 连接类错误(含 SSL EOF/超时/长连接被服务端关掉/会话失效的 4xx/429):
                # 丢弃连接与会话, 让下一次重试**重新握手**(而不是复用坏连接) —— 2026-09-15
                self._reset_connection(type(exc).__name__)
                time.sleep(2 + attempt * 3)
            except RuntimeError as exc:
                _record_mcp_call(
                    endpoint=url, tool=tool, ok=False,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    attempts=used_attempts, cat=cat_name, status_code=last_status,
                    error=str(exc),
                )
                raise
        _record_mcp_call(
            endpoint=url, tool=tool, ok=False,
            duration_ms=int((time.monotonic() - t0) * 1000),
            attempts=max(1, used_attempts), cat=cat_name, status_code=last_status,
            error=last_err,
        )
        raise RuntimeError(f"MCP {tool} 重试 {retries} 次仍失败: {last_err}")

    def call_raw(self, tool: str, arguments: dict, retries: int = MCP_CALL_RETRIES) -> dict:
        """调用工具并返回**完整业务信封**(含 code/message/pointCost), 供需要成本/审计的调用方。

        与 `_call` 的差别: 不做 code 判定(把业务错误交给调用方判断, 便于识别"额度耗尽"等
        可恢复情形), 也不剥离 data。返回 {} 表示 dry_run。
        卖家精灵 v1 的 `review` 调用会自动转译为 v2 `asin.review.list` 并保持信封兼容。
        """
        if self._mock is not None:
            return {}
        if self._url_key == "SS_MCP_URL" and tool == "review":
            req_args = dict(arguments or {})
            page = max(1, int(req_args.get("page", 1) or 1))
            size = max(1, int(req_args.get("size", 10) or 10))
            v2_args: dict[str, Any] = {
                "marketplace": req_args.get("marketplace") or _marketplace(str(self.config.get("SITE", ""))),
                "asin": req_args.get("asin"),
                "limit": min(100, page * size),
            }
            stars = req_args.get("starList") or req_args.get("star")
            if stars:
                v2_args["star"] = [int(s) for s in stars]
            v2_payload = self.call_raw("asin.review.list", v2_args, retries=retries)
            if not isinstance(v2_payload, dict):
                return {"code": "OK", "message": "成功", "pointCost": 0, "data": {"items": [], "total": 0}}
            if v2_payload.get("error"):
                err = v2_payload["error"]
                return {
                    "code": err.get("code") if isinstance(err, dict) else "error",
                    "message": err.get("message") if isinstance(err, dict) else str(err),
                    "pointCost": 0,
                    "data": None,
                }
            all_items = v2_payload.get("items") or []
            start = (page - 1) * size
            page_items = all_items[start:start + size] if isinstance(all_items, list) else []
            billing = v2_payload.get("billing") if isinstance(v2_payload.get("billing"), dict) else {}
            cost = billing.get("points") or billing.get("pointCost") or billing.get("cost") or 0
            total = v2_payload.get("total")
            if total is None:
                total = len(all_items) if isinstance(all_items, list) else 0
            return {
                "code": "OK",
                "message": "成功",
                "pointCost": cost,
                "data": {"items": page_items, "total": total},
            }
        url = self._url()
        key = str(self.config.get(self._key_key, "") or "")
        cat_name = str(getattr(self.config, "cat", "") or "")
        t0 = time.monotonic()
        last_err: str = ""
        last_status: int | None = None
        used_attempts = 0
        for attempt in range(retries):
            used_attempts = attempt + 1
            try:
                self._initialize()
                body = {"jsonrpc": "2.0", "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}, "id": 2}
                with _mcp_key_gate(url, key):
                    resp = self._http().post(url, headers=self._headers(), json=body)
                last_status = getattr(resp, "status_code", None)
                resp.raise_for_status()
                data = parse_mcp_json(resp)
                if data.get("error"):
                    raise MCPBusinessError(tool, data["error"].get("code"), str(data["error"]), data)
                result = data.get("result") or {}
                if result.get("isError"):
                    raise MCPBusinessError(tool, "isError", f"执行失败: {_content_texts(result)[:300]}", data)
                payload = _parse_payload(result)
                _record_mcp_call(
                    endpoint=url, tool=tool, ok=True,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    attempts=used_attempts, cat=cat_name, status_code=last_status,
                )
                return payload if isinstance(payload, dict) else {"code": None, "data": payload}
            except (httpx.HTTPError, OSError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    last_status = exc.response.status_code
                last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                self._reset_connection(type(exc).__name__)
                time.sleep(2 + attempt * 3)
            except RuntimeError as exc:
                _record_mcp_call(
                    endpoint=url, tool=tool, ok=False,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    attempts=used_attempts, cat=cat_name, status_code=last_status,
                    error=str(exc),
                )
                raise
        _record_mcp_call(
            endpoint=url, tool=tool, ok=False,
            duration_ms=int((time.monotonic() - t0) * 1000),
            attempts=max(1, used_attempts), cat=cat_name, status_code=last_status,
            error=last_err,
        )
        raise RuntimeError(f"MCP {tool} 重试 {retries} 次仍失败: {last_err}")

    def keyword_miner(self, request: dict) -> dict:
        """关键词召回 / 批量验证(V2 keyword.related.list / keyword.batch.info)。"""
        if self._mock is not None:
            return {"items": []}
        req = dict(request or {})
        mp = _marketplace(str(req.get("marketplace") or self.config.get("SITE", "")))
        month = _month_yyyymm(req.get("historyDate") or req.get("month") or "")
        # 1) 批量精准验证模式: keywordList -> V2 keyword.batch.info (每批最多 20 词)
        if "keywordList" in req:
            raw_words = []
            seen_words = set()
            for w in (req.get("keywordList") or []):
                s = str(w).strip()
                if s and s not in seen_words:
                    seen_words.add(s)
                    raw_words.append(s)
            out_items: list[dict] = []
            for start in range(0, len(raw_words), 20):
                if start > 0:
                    time.sleep(0.35)
                chunk = raw_words[start:start + 20]
                args: dict[str, Any] = {"marketplace": mp, "keywords": chunk}
                if month:
                    args["month"] = month
                resp = self._call("keyword.batch.info", args)
                items = resp.get("items") if isinstance(resp, dict) else []
                if not isinstance(items, list):
                    raise ValueError("keyword.batch.info response lacks items list")
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if str(it.get("resultStatus") or "").lower() == "no_data":
                        continue
                    row = dict(it)
                    if "searches" not in row and "searchVolume" in row:
                        row["searches"] = row.get("searchVolume")
                    if "bid" not in row and "ppcBid" in row:
                        row["bid"] = row.get("ppcBid")
                    out_items.append(row)
            return {"items": out_items}
        # 2) 根词扩展召回模式: keyword -> V2 keyword.related.list
        seed = str(req.get("keyword") or req.get("seedKeyword") or "").strip()
        size = max(1, min(100, int(req.get("size", 100) or 100)))
        page_num = max(1, int(req.get("page", 1) or 1))
        args = {
            "marketplace": mp,
            "seedKeyword": seed,
            "page": {"cursor": (page_num - 1) * size, "size": size},
        }
        if req.get("filterRootWord") and seed:
            args["includeKeywords"] = [seed]
        resp = self._call("keyword.related.list", args)
        items = resp.get("items") if isinstance(resp, dict) else None
        if not isinstance(items, list):
            raise ValueError("keyword.related.list response lacks items list")
        norm_items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            row = dict(it)
            if "searches" not in row and "searchVolume" in row:
                row["searches"] = row.get("searchVolume")
            if "bid" not in row and "ppcBid" in row:
                row["bid"] = row.get("ppcBid")
            norm_items.append(row)
        page_info = resp.get("page") if isinstance(resp, dict) and isinstance(resp.get("page"), dict) else {}
        return {
            "items": norm_items,
            "total": resp.get("total") if isinstance(resp, dict) else len(norm_items),
            "hasNextPage": bool(page_info.get("hasMore", False)),
        }

    def keyword_trend(self, keyword: str, month: str) -> dict:
        """接口A: 关键词趋势(V2 keyword.trend, 完整历史月度序列)。"""
        if self._mock is not None:
            return self._mock.keyword_trend(keyword, month)
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call(
            "keyword.trend",
            {
                "marketplace": mp,
                "keyword": keyword,
                "metrics": [
                    "searchVolume",
                    "purchaseVolume",
                    "purchaseRate",
                    "yearlyGrowth",
                    "chainGrowth",
                    "threeMonthGrowth",
                ],
                "granularity": "MONTH",
            },
        )
        return _normalize_trend(_v2_trend_to_rows(data), keyword, month)

    def aba_weekly_raw(self, keyword: str, site: str = "") -> list[dict]:
        """ABA 周度原始序列([{label:'YYYYMMDD', rank, searches}, ...], V2 keyword.aba.trend)。"""
        if self._mock is not None:
            return []
        mp = _marketplace(str(site or self.config.get("SITE", "")))
        data = self._call(
            "keyword.aba.trend",
            {"marketplace": mp, "keyword": keyword, "granularity": "WEEK"},
        )
        return _v2_aba_to_rows(data)

    def aba_weekly_bid(self, keyword: str, month: str) -> dict:
        """接口B: ABA 周度竞价(V2 keyword.aba.trend + keyword.batch.info CPC)。"""
        if self._mock is not None:
            return self._mock.aba_weekly_bid(keyword, month)
        data = self.aba_weekly_raw(keyword)
        cpc = self._cpc_of(keyword, month)
        return _normalize_aba(data, keyword, month, cpc)

    def _cpc_of(self, keyword: str, month: str) -> dict:
        """从 V2 keyword.batch.info 取 CPC/下限/上限(月级)。"""
        mp = _marketplace(str(self.config.get("SITE", "")))
        try:
            args: dict[str, Any] = {"marketplace": mp, "keywords": [keyword]}
            ym = _month_yyyymm(month)
            if ym:
                args["month"] = ym
            data = self._call("keyword.batch.info", args)
            return _extract_cpc(data)
        except Exception:
            return {}

    def google_trend(self, keyword: str, years: int = 5) -> dict:
        """接口C: Google 趋势(V2 keyword.google.trend 按月均值聚合)。"""
        if self._mock is not None:
            return self._mock.google_trend(keyword, years)
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call(
            "keyword.google.trend",
            {"marketplace": mp, "keyword": keyword, "gprop": "WEB"},
        )
        return _normalize_google_v2_monthly(data, keyword)

    def google_trend_weekly(self, keyword: str) -> dict:
        """Fetch the provider's weekly index, indexed by UTC week start (V2 keyword.google.trend)."""
        if self._mock is not None:
            return {"keyword": keyword, "google_weekly_index": {}, "status": "ok"}
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call(
            "keyword.google.trend",
            {"marketplace": mp, "keyword": keyword, "gprop": "WEB"},
        )
        return _normalize_google_v2_weekly(data, keyword)

    def competitor_lookup(self, month: str, asins: list[str]) -> list[dict]:
        """查竞品批量查询(V2 asin.list, FR-14 物流/上架时间补齐用)。

        每批最多 40 个 ASIN; 按 cursor 自动翻页拉全, 命中全部请求 ASIN 或到末页即停;
        availableDate(ISO 日期字符串)统一归一为毫秒时间戳, 保持下游兼容。
        """
        if self._mock is not None:
            return []
        asins = [str(a).strip() for a in (asins or []) if str(a).strip()]
        if not asins:
            return []
        mp = _marketplace(str(self.config.get("SITE", "美国") or "美国"))
        month = _month_yyyymm(month)
        request_asins = asins[:40]
        out_v2: list[dict] = []
        seen_v2: set[str] = set()
        cursor = 0
        for _ in range(20):
            args: dict[str, Any] = {
                "marketplace": mp,
                "asins": request_asins,
                "page": {"cursor": cursor, "size": 100},
            }
            if month:
                args["dataMonth"] = month
            data = self._call("asin.list", args)
            items = data.get("items") if isinstance(data, dict) else []
            if not isinstance(items, list):
                break
            new = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                a = str(it.get("asin") or "").strip()
                if not a or a in seen_v2:
                    continue
                seen_v2.add(a)
                row = dict(it)
                row["availableDate"] = _normalize_available_date_ms(row.get("availableDate"))
                new.append(row)
            out_v2.extend(new)
            if set(request_asins) <= seen_v2:
                break
            page_info = data.get("page") if isinstance(data, dict) and isinstance(data.get("page"), dict) else {}
            if not new or not page_info.get("hasMore") or page_info.get("nextCursor") is None:
                break
            cursor = int(page_info["nextCursor"])
        return out_v2


def _mcp_log(msg: str) -> None:
    """把 MCP 重试/异常写进流水线日志(拿不到 logger 时静默, 不影响主流程)。"""
    try:
        from .logger import get_logger

        get_logger().warning("[MCP] %s", msg)
    except Exception:  # noqa: BLE001
        pass


def _content_texts(result: dict) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or item.get("type") or ""))
    return " | ".join(parts)


def _decode_json_text(raw: str) -> Any:
    """解析 JSON 对象/数组，兼容外层多包 JSON 字符串及前置/后置横幅文案（如 MCP v1 迁移提醒）。"""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        val = json.loads(s)
        if isinstance(val, (dict, list)):
            return val
        if isinstance(val, str):
            s = val.strip()
            try:
                inner = json.loads(s)
                if isinstance(inner, (dict, list)):
                    return inner
            except (json.JSONDecodeError, ValueError):
                pass
    except (json.JSONDecodeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    best = None
    best_span = -1
    idx = 0
    n = len(s)
    while idx < n:
        pos_obj = s.find("{", idx)
        pos_arr = s.find("[", idx)
        cands = [p for p in (pos_obj, pos_arr) if p >= 0]
        if not cands:
            break
        pos = min(cands)
        try:
            obj, end = decoder.raw_decode(s, pos)
            if isinstance(obj, (dict, list)):
                if isinstance(obj, dict) and any(k in obj for k in ("code", "data", "items")):
                    return obj
                span = end - pos
                if span > best_span:
                    best = obj
                    best_span = span
                idx = end
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        idx = pos + 1
    return best


def _parse_payload(result: dict) -> Any:
    """解析 MCP content 包装为业务 JSON 对象。

    2026-09-07: 不再按 " | " 切分文本——单段 content 的 JSON 内(商品标题/类目等
    字符串字段)本身可能含 " | ", 切分会把整段 JSON 撕碎导致解析失败
    (competitor_lookup 全历史回填 0 的根因)。
    2026-11-23: 卖家精灵 MCP v1 在 content[0].text 前注入了“服务迁移提醒”横幅文本并
    额外包了一层 JSON 字符串引号，导致 json.loads() 解出 str 而非 dict。
    解析顺序:
    ① 整段拼接调用 _decode_json_text(兼容纯 JSON / 外层字符串包装 / 前后横幅文本);
    ② 逐段独立调用 _decode_json_text;
    ③ 兼容旧格式: 仍失败时按 " | " 切分逐份尝试。
    """
    blocks = [
        str(item.get("text") or "").strip()
        for item in result.get("content") or []
        if isinstance(item, dict) and (item.get("text") or "").strip()
    ]
    if not blocks:
        return {}
    joined = "\n".join(blocks)
    parsed = _decode_json_text(joined)
    if parsed is not None:
        return parsed
    for block in blocks:
        parsed = _decode_json_text(block)
        if parsed is not None:
            return parsed
    for text in " | ".join(blocks).split(" | "):
        parsed = _decode_json_text(text)
        if parsed is not None:
            return parsed
    return {}


def _normalize_trend(data, keyword: str, month: str) -> dict:
    """keyword_research_trends 数据 -> 契约结构。

    序列元素: {time:"2026年07月", search, purchase, purchaseRate(%), yearlyGrowth,
               chainGrowth, threeMonthGrowth}
    站内同比(**严格单月同比, 2026-07 vs 2025-07**): 由完整月度序列自算
    (目标月 search vs 去年同月 search); 去年同月缺失时兜底用接口 yearlyGrowth。
    返回目标月行 + 完整月度序列(月: YYYY-MM)。
    """
    rows = data if isinstance(data, list) else []
    target_key = _month_key(month)
    search_by_month: dict[str, float] = {}
    purchase_by_month: dict[str, float] = {}
    rate_by_month: dict[str, float] = {}
    chain_by_month: dict[str, float] = {}
    growth_by_month: dict[str, float] = {}
    fallback_yoy: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ym = _parse_cn_month(str(row.get("time") or ""))
        if not ym:
            continue
        if row.get("search") is not None:
            search_by_month[ym] = _num(row.get("search"))
        if row.get("purchase") is not None:
            purchase_by_month[ym] = _num(row.get("purchase"))
        if row.get("purchaseRate") is not None:
            rate_by_month[ym] = _num(row.get("purchaseRate"))
        if row.get("chainGrowth") is not None:
            chain_by_month[ym] = _num(row.get("chainGrowth"))
        if row.get("threeMonthGrowth") is not None:
            growth_by_month[ym] = _num(row.get("threeMonthGrowth"))
        if row.get("yearlyGrowth") is not None:
            fallback_yoy[ym] = _num(row.get("yearlyGrowth"))

    series = []
    for ym in sorted(search_by_month):
        series.append(
            {
                "month": ym,
                "search_volume": search_by_month.get(ym),
                "purchase_volume": purchase_by_month.get(ym),
                "purchase_rate": rate_by_month.get(ym),
                "yoy": _self_yoy(search_by_month, ym, fallback_yoy),
            }
        )

    target_ym = _target_ym(target_key, search_by_month)
    target = {}
    if target_ym:
        target = {
            "search_volume": search_by_month.get(target_ym),
            "mom_yoy": _self_yoy(search_by_month, target_ym, fallback_yoy),
            "purchase_volume": purchase_by_month.get(target_ym),
            "purchase_rate": rate_by_month.get(target_ym),
            "mom_change": _to_percent(chain_by_month.get(target_ym)),
            "growth_3m": _to_percent(growth_by_month.get(target_ym)),
        }
    return {
        "keyword": keyword,
        "month": target_ym.replace("-", "") if target_ym else None,
        "requested_month": month,
        "search_volume": target.get("search_volume"),
        "mom_yoy": target.get("mom_yoy"),
        "purchase_volume": target.get("purchase_volume"),
        "purchase_rate": target.get("purchase_rate"),
        "mom_change": target.get("mom_change"),
        "growth_3m": target.get("growth_3m"),
        "monthly_series": series,
        "status": "ok",
    }


def _target_ym(target_key: str, search_by_month: dict) -> str | None:
    """确定目标月 YYYY-MM; 配置月缺失时取序列最后一个月。"""
    if not search_by_month:
        return None
    if len(target_key) == 6:
        candidate = f"{target_key[:4]}-{target_key[4:6]}"
        if candidate in search_by_month:
            return candidate
    return sorted(search_by_month)[-1]


def _self_yoy(search_by_month: dict, ym: str, fallback_yoy: dict) -> float | None:
    """站内单月同比: ym 月搜索量 vs 去年同月搜索量; 缺失兜底接口 yearlyGrowth。"""
    cur = search_by_month.get(ym)
    if cur is None:
        return None
    try:
        y, m = int(ym[:4]), ym[5:7]
        prev = search_by_month.get(f"{y - 1}-{m}")
        if prev is not None and prev != 0:
            return round((cur - prev) / prev * 100.0, 2)
    except (TypeError, ValueError):
        pass
    fb = fallback_yoy.get(ym)
    if fb is not None:
        return round(fb * 100.0, 2)
    return None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_aba(data, keyword: str, month: str, cpc: dict) -> dict:
    """aba_research_trend(周) + keyword_miner(CPC) -> 契约结构。

    序列元素: {date(ms), rank, searches, label:"20220205"}
    """
    rows = data if isinstance(data, list) else []
    target = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        if label[:6] == month:
            target = row
    if target is None and rows:
        target = rows[-1] if isinstance(rows[-1], dict) else {}
    target = target or {}
    return {
        "keyword": keyword,
        "week": str(target.get("label") or ""),
        "cpc": cpc.get("cpc"),
        "cpc_min": cpc.get("cpc_min"),
        "cpc_max": cpc.get("cpc_max"),
        "weekly_search_volume": target.get("searches"),
        "search_rank": target.get("rank"),
        "status": "ok",
    }


def _extract_cpc(data) -> dict:
    """从 keyword_miner 的 data.items 提取 CPC/bidMin/bidMax。"""
    items = []
    if isinstance(data, dict):
        items = data.get("items") or []
    elif isinstance(data, list):
        items = data
    for item in items:
        if isinstance(item, dict):
            return {
                "cpc": item.get("bid"),
                "cpc_min": item.get("bidMin"),
                "cpc_max": item.get("bidMax"),
            }
    return {}


def _normalize_google(data, keyword: str) -> dict:
    """google_trend 数据 -> {month: index} 序列。

    data.items: [{time: 毫秒时间戳, value: 指数}]
    """
    series = {}
    if isinstance(data, dict):
        items = data.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            ts = item.get("time")
            if ts:
                month = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m")
                series[month] = item.get("value")
    return {"keyword": keyword, "google_monthly_index": series, "status": "ok"}


def _v2_trend_to_rows(data) -> list[dict]:
    """把 V2 keyword.trend 的 {series: {metric: [{date, value}]}} 转成 _normalize_trend 所需行列表。"""
    if isinstance(data, list):
        return data
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, dict):
        return []
    metric_map = {
        "searchVolume": "search",
        "purchaseVolume": "purchase",
        "purchaseRate": "purchaseRate",
        "yearlyGrowth": "yearlyGrowth",
        "chainGrowth": "chainGrowth",
        "threeMonthGrowth": "threeMonthGrowth",
    }
    by_date: dict[str, dict] = {}
    for v2_key, v1_key in metric_map.items():
        points = series.get(v2_key)
        if not isinstance(points, list):
            continue
        for pt in points:
            if not isinstance(pt, dict):
                continue
            d = str(pt.get("date") or "").strip()
            if not d:
                continue
            row = by_date.setdefault(d, {"time": d})
            if pt.get("value") is not None:
                row[v1_key] = pt.get("value")
    return [by_date[d] for d in sorted(by_date)]


def _v2_aba_to_rows(data) -> list[dict]:
    """把 V2 keyword.aba.trend 的 {series: {abaRank, abaSearchVolume}} 转成 [{label:'YYYYMMDD', rank, searches}]。"""
    if isinstance(data, list):
        return data
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, dict):
        return []
    by_label: dict[str, dict] = {}
    for v2_key, v1_key in (("abaRank", "rank"), ("abaSearchVolume", "searches")):
        points = series.get(v2_key)
        if not isinstance(points, list):
            continue
        for pt in points:
            if not isinstance(pt, dict):
                continue
            d = str(pt.get("date") or "").strip()
            label = d.replace("-", "")
            if not label:
                continue
            row = by_label.setdefault(label, {"label": label})
            if pt.get("value") is not None:
                row[v1_key] = pt.get("value")
    return [by_label[k] for k in sorted(by_label)]


def _normalize_google_v2_monthly(data, keyword: str) -> dict:
    """V2 keyword.google.trend 周度序列 -> 按 YYYY-MM 均值聚合为 {month: index}。"""
    series_obj = data.get("series") if isinstance(data, dict) else None
    points = series_obj.get("interest") if isinstance(series_obj, dict) else None
    buckets: dict[str, list[float]] = {}
    if isinstance(points, list):
        for item in points:
            if not isinstance(item, dict):
                continue
            d = str(item.get("date") or "").strip()
            v = item.get("value")
            if len(d) >= 7 and v is not None:
                try:
                    buckets.setdefault(d[:7], []).append(float(v))
                except (TypeError, ValueError):
                    pass
    monthly = {ym: round(sum(vals) / len(vals), 2) for ym, vals in sorted(buckets.items()) if vals}
    return {"keyword": keyword, "google_monthly_index": monthly, "status": "ok"}


def _normalize_google_v2_weekly(data, keyword: str) -> dict:
    """V2 keyword.google.trend 周度序列 -> {YYYY-MM-DD: index}。"""
    series_obj = data.get("series") if isinstance(data, dict) else None
    points = series_obj.get("interest") if isinstance(series_obj, dict) else None
    if not isinstance(points, list):
        raise ValueError("Google 周趋势响应缺少 series.interest")
    series: dict[str, Any] = {}
    for item in points:
        if not isinstance(item, dict):
            continue
        day = str(item.get("date") or "").strip()
        if not day:
            continue
        val = item.get("value")
        if val is None:
            continue
        series[day] = val if day not in series else None
    return {"keyword": keyword, "google_weekly_index": series, "status": "ok"}


def _normalize_available_date_ms(value):
    """把 V2 ISO 日期字符串('YYYY-MM-DD')或 V1 毫秒时间戳统一转为毫秒时间戳 int。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return value


def _month_yyyymm(value) -> str:
    """把 '2026-07' / '2026.07' / '2026年7月' / '202607' 归一为 6 位 '202607'；无效返回 ''。"""
    import re
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{4})[-./年](\d{1,2})月?$", raw)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{yr:04d}{mo:02d}"
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 6:
        mo = int(digits[4:6])
        if 1 <= mo <= 12:
            return digits
    return ""


def _month_key(month: str) -> str:
    return _month_yyyymm(month) or str(month or "").strip()


def _parse_cn_month(text: str) -> str | None:
    """把 '2026年07月' / '2026-07' / '2026.07' / '202607' 解析为 '2026-07'; 失败返回 None。"""
    ym = _month_yyyymm(text)
    if len(ym) == 6:
        return f"{ym[:4]}-{ym[4:6]}"
    return None


def _to_percent(value) -> float | None:
    """把比率(0.0195)转为百分数(1.95)。yearlyGrowth/chainGrowth/threeMonthGrowth
    均为比率(可能 >1 表示多倍增长), 统一乘 100。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v * 100, 2)


def throttle(min_seconds: float = 1.0):
    """FR-05/06: 每词间隔节流(防限流/耗积分)。"""
    time.sleep(min_seconds)
