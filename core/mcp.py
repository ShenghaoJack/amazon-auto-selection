"""卖家精灵 MCP 客户端 (FR-05/FR-06)。

三接口(2026-09 实测真实工具映射):
- keyword_trend  -> keyword_research_trends: 关键词趋势(历史月度序列)
                   返回 search(搜索量)/purchase(购买量)/purchaseRate(购买率%)/
                   yearlyGrowth(同比)/chainGrowth(环比)/threeMonthGrowth(3月增长)
- aba_weekly_bid -> aba_research_trend(周粒度): ABA 排名+周搜索量
                   + keyword_miner: CPC/bidMin/bidMax
- google_trend   -> google_trend: Google 趋势月度指数(items 毫秒时间戳+value)

协议: 标准 MCP Streamable HTTP (2025-03-26), 必须先 initialize 握手再 tools/call;
请求头 secret-key 鉴权; 返回包装为 {"content":[{"type":"text","text":"<JSON>"}]},
数据 JSON 为 {code, message, data}; 部分工具参数嵌套 request 对象(如 keyword_miner/
google_trend), 部分平铺(如 keyword_research_trends/aba_research_trend), 由各方法决定。
单接口失败不影响其他 (FR-05)。dry_run 模式返回 MockMCP 数据。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

import httpx

from .config import Config
from .mock import MockMCP


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
# 但**重试不能过小**——卖家精灵 MCP 会偶发 SSL 连接中断(UNEXPECTED_EOF_WHILE_READING),
# 需多次重试(2/5/8s 退避)才能命中成功, 否则 google_trend 数据易丢。
MCP_HTTP_TIMEOUT = 25
MCP_CALL_RETRIES = 3
# 握手/密钥预检(_initialize / check)的重试次数与退避: 与 _call 同等待遇
# (2026-09-15: 步骤6 因预检单次 POST 撞上瞬时 SSL EOF 而整步失败)
MCP_HTTP_RETRIES = 3

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


class MCPClient:
    def __init__(self, config: Config, *, url_key: str = "SS_MCP_URL",
                 key_key: str = "SS_MCP_KEY", auth_header: str = "secret-key",
                 auth_scheme: str = ""):
        """MCP 客户端。

        默认指向卖家精灵(SS_MCP_URL/SS_MCP_KEY + secret-key 头), 保持既有行为不变;
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
        url = str(self.config.get(self._url_key, "") or "")
        if not url:
            raise RuntimeError(f"{self._url_key} 未配置")
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
        tries = max(1, int(retries))
        last: Exception | None = None
        for attempt in range(tries):
            try:
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
        """校验密钥并返回可用工具列表。

        2026-09-09 实测: tools/list 会同时返回真实工具(49 个)与 4 个哨兵工具
        (secret_expired/secret_unauthorized/secret_invalid/secret_no_remaining,
        调用哨兵才返回对应错误)——判定有效与否看**真实所需工具是否存在**,
        不能因列表含 secret_expired 就判过期(见 steps/base.check_mcp_key)。
        """
        if self._mock is not None:
            return ["keyword_research_trends", "aba_research_trend", "google_trend", "keyword_miner"]
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

        返回格式: {"content":[{"type":"text","text":"<JSON>"}], "isError": bool}
        业务 JSON: {"code":"OK","data":...}; isError 或 code!=OK 时抛错。
        """
        if self._mock is not None:
            return None
        url = self._url()
        last_err: str = ""
        for attempt in range(retries):
            try:
                self._initialize()          # 已有会话则跳过(见其 docstring), 不再每次工具调用都握手
                body = {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                    "id": 2,
                }
                resp = self._http().post(url, headers=self._headers(), json=body)
                resp.raise_for_status()
                data = parse_mcp_json(resp)
                if "error" in data and data["error"]:
                    raise MCPBusinessError(tool, data["error"].get("code"), str(data["error"]), data)
                result = data.get("result") or {}
                if result.get("isError"):
                    texts = _content_texts(result)
                    raise MCPBusinessError(tool, "isError", f"执行失败: {texts[:300]}", data)
                payload = _parse_payload(result)
                if isinstance(payload, dict) and payload.get("code") not in (None, "OK", 200, 0):
                    raise MCPBusinessError(tool, payload.get("code"), str(payload.get("message", payload)), payload)
                return payload.get("data") if isinstance(payload, dict) else payload
            except (httpx.HTTPError, OSError) as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                # 连接类错误(含 SSL EOF/超时/长连接被服务端关掉/会话失效的 4xx):
                # 丢弃连接与会话, 让下一次重试**重新握手**(而不是复用坏连接) —— 2026-09-15
                self._reset_connection(type(exc).__name__)
                time.sleep(2 + attempt * 3)
            except RuntimeError:
                raise
        raise RuntimeError(f"MCP {tool} 重试 {retries} 次仍失败: {last_err}")

    def call_raw(self, tool: str, arguments: dict, retries: int = MCP_CALL_RETRIES) -> dict:
        """调用工具并返回**完整业务信封**(含 code/message/pointCost), 供需要成本/审计的调用方。

        与 `_call` 的差别: 不做 code 判定(把业务错误交给调用方判断, 便于识别"额度耗尽"等
        可恢复情形), 也不剥离 data。返回 {} 表示 dry_run。
        """
        if self._mock is not None:
            return {}
        url = self._url()
        last_err: str = ""
        for attempt in range(retries):
            try:
                self._initialize()
                body = {"jsonrpc": "2.0", "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}, "id": 2}
                resp = self._http().post(url, headers=self._headers(), json=body)
                resp.raise_for_status()
                data = parse_mcp_json(resp)
                if data.get("error"):
                    raise MCPBusinessError(tool, data["error"].get("code"), str(data["error"]), data)
                result = data.get("result") or {}
                if result.get("isError"):
                    raise MCPBusinessError(tool, "isError", f"执行失败: {_content_texts(result)[:300]}", data)
                payload = _parse_payload(result)
                return payload if isinstance(payload, dict) else {"code": None, "data": payload}
            except (httpx.HTTPError, OSError) as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                self._reset_connection(type(exc).__name__)
                time.sleep(2 + attempt * 3)
        raise RuntimeError(f"MCP {tool} 重试 {retries} 次仍失败: {last_err}")

    def keyword_trend(self, keyword: str, month: str) -> dict:
        """接口A: 关键词趋势(keyword_research_trends, 完整历史月度序列)。"""
        if self._mock is not None:
            return self._mock.keyword_trend(keyword, month)
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call(
            "keyword_research_trends",
            {"marketplace": mp, "keyword": keyword, "month": month},
        )
        return _normalize_trend(data, keyword, month)

    def aba_weekly_bid(self, keyword: str, month: str) -> dict:
        """接口B: ABA 周度竞价(aba_research_trend 周粒度 + keyword_miner CPC)。"""
        if self._mock is not None:
            return self._mock.aba_weekly_bid(keyword, month)
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call(
            "aba_research_trend",
            {"marketplace": mp, "keyword": keyword, "timeGranularity": "W"},
        )
        cpc = self._cpc_of(keyword, month)
        return _normalize_aba(data, keyword, month, cpc)

    def _cpc_of(self, keyword: str, month: str) -> dict:
        """从 keyword_miner 取 CPC/下限/上限(月级)。"""
        mp = _marketplace(str(self.config.get("SITE", "")))
        try:
            data = self._call(
                "keyword_miner",
                {"request": {"keywordList": [keyword], "historyDate": month, "marketplace": mp}},
            )
            return _extract_cpc(data)
        except Exception:
            return {}

    def google_trend(self, keyword: str, years: int = 5) -> dict:
        """接口C: Google 趋势(google_trend, monthly=true 月度指数)。"""
        if self._mock is not None:
            return self._mock.google_trend(keyword, years)
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call(
            "google_trend",
            {"request": {"keyword": keyword, "marketplace": mp, "monthly": True, "googleProp": "web"}},
        )
        return _normalize_google(data, keyword)

    def google_trend_weekly(self, keyword: str) -> dict:
        """Fetch the provider's weekly index, indexed by UTC week start."""
        from datetime import timezone
        if self._mock is not None:
            return {"keyword": keyword, "google_weekly_index": {}, "status": "ok"}
        mp = _marketplace(str(self.config.get("SITE", "")))
        data = self._call("google_trend", {"request": {"keyword": keyword, "marketplace": mp,
                                                        "monthly": False, "googleProp": "web"}})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Google 周趋势响应缺少 items")
        series = {}
        for item in data["items"]:
            if isinstance(item, dict) and item.get("time") is not None:
                day = datetime.fromtimestamp(int(item["time"]) / 1000, timezone.utc).date().isoformat()
                series[day] = item.get("value") if day not in series else None
        return {"keyword": keyword, "google_weekly_index": series, "status": "ok"}

    def competitor_lookup(self, month: str, asins: list[str]) -> list[dict]:
        """查竞品批量查询(MCP competitor_lookup, FR-14 物流补齐用)。

        实测(2026-09): request.asins 最多 40 个, 超需拆批; 返回 data.items[]
        每行含 asin / fulfillment(FBA/AMZ/FBM/NA...) / availableDate(毫秒时间戳) 等;
        month 格式 yyyyMM(如 202607, 兼容传入 yyyy-MM);
        默认模糊匹配返回集可能大于请求数且分页(total>size)——
        2026-09-07: 自动翻页拉全, 命中全部请求 ASIN 或到末页即停。
        """
        if self._mock is not None:
            return []
        asins = [str(a).strip() for a in (asins or []) if str(a).strip()]
        if not asins:
            return []
        mp = _marketplace(str(self.config.get("SITE", "美国") or "美国"))
        month = str(month or "").strip().replace("-", "")
        request_asins = asins[:40]
        out: list[dict] = []
        seen: set[str] = set()
        size = min(len(request_asins), 40)
        for page in range(1, 21):  # 翻页上限防死循环
            data = self._call(
                "competitor_lookup",
                {"request": {"marketplace": mp, "asins": request_asins,
                             "month": month, "size": size, "page": page}},
            )
            items = []
            if isinstance(data, dict):
                items = data.get("items") or data.get("list") or []
            elif isinstance(data, list):
                items = data
            new = [
                it for it in items
                if isinstance(it, dict) and str(it.get("asin") or "").strip() not in seen
            ]
            for it in new:
                seen.add(str(it.get("asin") or "").strip())
            out.extend(new)
            if set(request_asins) <= seen:
                break
            pages = data.get("pages") if isinstance(data, dict) else 1
            if not new or page >= int(pages or 1):
                break
        return out


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


def _parse_payload(result: dict) -> Any:
    """解析 MCP content 包装为业务 JSON 对象。

    2026-09-07: 不再按 " | " 切分文本——单段 content 的 JSON 内(商品标题/类目等
    字符串字段)本身可能含 " | ", 切分会把整段 JSON 撕碎导致解析失败
    (competitor_lookup 全历史回填 0 的根因)。解析顺序:
    ① 整段拼接整体 json.loads(单段 JSON / 多段分片 JSON 均覆盖);
    ② 逐段独立解析(多段各自为完整 JSON 的场景);
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
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, ValueError):
        pass
    for block in blocks:
        try:
            return json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
    for text in " | ".join(blocks).split(" | "):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
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


def _month_key(month: str) -> str:
    return str(month or "").strip()


def _parse_cn_month(text: str) -> str | None:
    """把 '2026年07月' 解析为 '2026-07'; 失败返回 None。"""
    try:
        year = text[:4]
        mon = text[5:7] if len(text) > 6 else text[4:6]
        return f"{year}-{mon.zfill(2)}"
    except Exception:
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
