"""HTTP 直连抓取客户端 (FR-01/FR-10)。

- **curl_cffi 模拟 Chrome TLS 指纹**(impersonate="chrome", JA3/JA4/HTTP2 指纹与真实浏览器一致,
  大幅降低亚马逊风控识别率——实测原脚本 1469 请求成功率 99.6%, 仅 6 失败)
- 模拟浏览器指纹(User-Agent/Accept-Language=en-US 等) + 会话 Cookie 鉴权
- 指数退避重试(6/15/30 秒 + 随机抖动), 每 ASIN 最多 3 次
- 请求间随机间隔 1.5-3 秒防限流
- 失败状态分类: bot_blocked / blocked_page / not_found / http_N (FR-01)
- dry_run 模式返回 MockHttp 模拟数据
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

from curl_cffi import requests as cffi_requests

from .config import Config
from .mock import MockHttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class HttpResponse:
    status: int
    text: str
    url: str = ""
    ok: bool = False
    status_class: str = ""


@dataclass
class ProductPage:
    request_asin: str
    ok: bool
    status: str
    title: str = ""
    bullets: list = field(default_factory=list)
    fetched_at: str = ""


class HttpFetcher:
    """带重试/限流/状态分类的 HTTP 客户端。"""

    def __init__(self, config: Config, cookie: str | None = None):
        self.config = config
        self.cookie = cookie
        self._last_request_at = 0.0
        self._mock = MockHttp() if config.dry_run else None
        self._session = None

    def _get_session(self):
        if self._session is None:
            self._session = cffi_requests.Session(impersonate="chrome")
        return self._session

    def _reset_session(self) -> None:
        sess = self._session
        self._session = None
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._reset_session()

    def __del__(self):
        try:
            self._reset_session()
        except Exception:  # noqa: BLE001
            pass

    def _headers(self) -> dict:
        headers = {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.cookie is not None:
            cookie = self.cookie
        else:
            from core.amazon_login_share import resolve_cookie_header
            cookie = resolve_cookie_header(self.config)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _throttle(self) -> None:
        lo = float(self.config.get("REQUEST_MIN_INTERVAL", 1.5))
        hi = float(self.config.get("REQUEST_MAX_INTERVAL", 3.0))
        elapsed = time.monotonic() - self._last_request_at
        wait = random.uniform(lo, hi) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str, retries: int | None = None) -> HttpResponse:
        """GET 请求(curl_cffi 模拟 Chrome 指纹), 递增退避重试, 返回带状态分类的结果。

        重试对齐实测稳定方案: 最多 4 次尝试, 间隔 [8,20,40,70] 秒递增 + 0-5 秒随机抖动。
        curl_cffi 异常均为 RequestsError(OSError 子类), 统一降级为 http_0。
        """
        if self._mock is not None:
            return self._mock.get(url)
        retries = retries if retries is not None else int(self.config.get("REQUEST_RETRY", 2))
        backoffs = [8, 20, 40, 70]
        last: HttpResponse | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                client = self._get_session()
                resp = client.get(
                    url,
                    headers=self._headers(),
                    timeout=float(self.config.get("HTTP_TIMEOUT", 30)),
                    allow_redirects=True,
                )
                last = HttpResponse(resp.status_code, resp.text, str(resp.url))
                last.status_class = classify_failure(resp.status_code, resp.text)
                if last.status_class == "ok":
                    last.ok = True
                    return last
                if last.status_class == "not_found":
                    return last
            except OSError as exc:
                # curl_cffi RequestsError/CurlError 均为 OSError 子类; 连接异常时重建 Session
                self._reset_session()
                last = HttpResponse(0, str(exc), url)
                last.status_class = "http_0"
            except Exception as exc:  # 其他意外异常(页面解析/构造)同样降级
                self._reset_session()
                last = HttpResponse(0, str(exc), url)
                last.status_class = "http_0"
            if attempt < retries - 1:
                backoff = backoffs[min(attempt, len(backoffs) - 1)]
                time.sleep(backoff + random.uniform(0, 5))
        return last or HttpResponse(0, "", url, status_class="http_0")

    def fetch_product_page(self, asin: str, market: str | None = None) -> ProductPage:
        """抓取商品详情页(FR-01): 解析标题与五点, 做状态分类。"""
        market = market or self.config.market
        url = f"https://{market}/dp/{asin}"
        resp = self.get(url)
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if resp.status_class != "ok":
            return ProductPage(asin, False, resp.status_class, fetched_at=now)
        title, bullets = parse_product_html(resp.text)
        if not title:
            return ProductPage(asin, False, "parse_failed", fetched_at=now)
        return ProductPage(asin, True, "ok", title=title, bullets=bullets, fetched_at=now)


def _is_not_found_title(text: str) -> bool:
    m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.I | re.S)
    if not m:
        return False
    title = re.sub(r"\s+", " ", m.group(1)).strip().lower()
    return any(k in title for k in ("page not found", "page introuvable", "seite nicht gefunden"))


def classify_failure(status: int, text: str) -> str:
    """FR-01 失败状态分类。

    真实商品页优先: 含 productTitle 的页面即使偶含错误提示短语
    (如"something went wrong"链接文案)也判定为 ok, 避免误判 blocked_page。
    200 状态的 404 下架页(含 Page Not Found 标题与底部 automated access 注释)优先判 not_found。
    """
    if status == 200:
        if _is_not_found_title(text):
            return "not_found"
        if is_bot_page(text):
            return "bot_blocked"
        low = text.lower()
        if 'id="producttitle"' in low or "producttitle" in low:
            return "ok"
        if is_error_page(text):
            return "blocked_page"
        return "ok"
    if status == 404:
        return "not_found"
    if status in (429, 503):
        return "bot_blocked"
    return f"http_{status}"


def is_bot_page(text: str) -> bool:
    lowered = text.lower()
    markers = ["captcha", "robot check", "automated access", "api-gateway", "verify you are human"]
    return any(m in lowered for m in markers)


def is_error_page(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "sorry, we just need to make sure you're not a robot",
        "something went wrong",
        "we encountered an error",
    ]
    return any(m in lowered for m in markers)


def parse_product_html(html: str) -> tuple[str, list[str]]:
    """从详情页 HTML 中提取标题与五点(轻量正则解析)。

    五点**仅取 feature-bullets 容器首个 <ul>..</ul> 内**的 <li> 项(2026-09 修复:
    旧版全局扫描会把页面其它区域(产品描述/A+/视频组件)同构的 <li> 误当五点,
    出现 3-4 万字符的超长脏数据)。页面无 feature-bullets(结构变化/A+布局)
    时五点留空, 不抓取全局杂项。
    """
    import re

    title = ""
    m = re.search(r'<span id="productTitle"[^>]*>\s*(.*?)\s*</span>', html, re.S)
    if m:
        title = clean_text(m.group(1))
    bullets: list[str] = []
    seg = ""
    i = html.find('id="feature-bullets"')
    if i != -1:
        ul = html.find("<ul", i)
        if ul != -1:
            end = html.find("</ul>", ul)
            if end != -1:
                seg = html[ul : end + 5]
    if not seg:
        return title, bullets
    for m in re.finditer(
        r'<li[^>]*>\s*<span class="a-list-item">\s*(.*?)\s*</span>\s*</li>',
        seg,
        re.S,
    ):
        text = clean_text(m.group(1))
        if text and len(bullets) < 5:
            bullets.append(text)
    return title, bullets


def clean_text(raw: str) -> str:
    """去标签 + 完整 HTML 反转义(html.unescape 处理全部命名/数字实体,
    如双引号/对勾/方向标记等)+ 合并空白。"""
    import html
    import re
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
