"""COOKIE 有效性检测 (HTTP 直连步骤 1/10 前置检查)。

原理: 用 config.yaml 的 COOKIE 请求一个真实商品详情页, 分析返回内容:
- 含 productTitle 标题       -> COOKIE 有效, 正常访问
- 验证码/机器人检查页        -> 被风控拦截(COOKIE 失效或 IP 被标记)
- 跳转/登录页(signin)        -> COOKIE 过期或未登录
- 连接失败(SSL/超时)         -> 网络问题, 与 COOKIE 无关

供编排器登录预检查(_precheck_cookie)与命令行 scripts/check_cookie.py 复用。
"""
from __future__ import annotations

from .config import Config
from .http_client import HttpFetcher

# 检测用测试 ASIN(需为真实存在商品, 变化时替换)
PROBE_ASIN = "B03A9C46E9"


def analyze_page(text: str) -> str:
    """分析页面内容, 返回类别: ok / bot / signin / error / unknown。"""
    low = text.lower()
    if 'id="producttitle"' in low or "producttitle" in low:
        return "ok"
    if "ap/signin" in low or ("signin" in low and "sign-in" in low):
        return "signin"
    if any(m in low for m in ("captcha", "robot check", "automated access", "api-gateway", "verify you are human")):
        return "bot"
    if "something went wrong" in low or "we encountered an error" in low:
        return "error"
    return "unknown"


def check_cookie(config: Config, asin: str = PROBE_ASIN) -> dict:
    """执行一次 COOKIE 检测, 返回 {valid, status, message}。

    status: ok / no_cookie / expired / bot_blocked / network / 其他状态码
    """
    cookie = str(config.get("COOKIE", "") or "")
    if not cookie:
        return {"valid": False, "status": "no_cookie", "message": "未配置 COOKIE, HTTP 直连将匿名访问"}
    fetcher = HttpFetcher(config, cookie=cookie)
    url = f"https://{config.market}/dp/{asin}"
    # 风控是概率性的: 单次 bot_blocked 可能是瞬时拦截, 用配置的重试次数(默认2, 2026-09-16 内化)
    # 连续多次全被拦才判定 COOKIE 失效/被标记(避免偶发拦截误阻断整轮流程)
    retries = max(1, int(config.get("REQUEST_RETRY", 2) or 2))
    resp = fetcher.get(url, retries=retries)
    kind = analyze_page(resp.text)
    # 页面含真实商品标题(productTitle)即判定 COOKIE 有效 —— 即使 status_class
    # 被 classify_failure 误判为 blocked_page(真实页面偶含错误提示短语)。
    # 2026-09-13 修复: 判定条件必须与 analyze_page 的口径一致 —— 原来这里又要求
    # `id="producttitle"`(窄), 而 analyze_page 只要出现 producttitle(宽)就给 ok, 两者分叉时
    # 会落到末尾兜底返回 {"valid": False, "status": "ok"}: 调用方(orchestrator/check_cookie.py)
    # 只看 status=="ok" → 一次**失败**的预检被打印成"COOKIE 检查通过/有效 ✅"(静默假成功)。
    if kind == "ok":
        return {"valid": True, "status": "ok", "message": "COOKIE 有效, 能正常抓取商品详情"}
    if kind == "signin":
        return {"valid": False, "status": "expired", "message": "返回登录页, COOKIE 已过期或未登录"}
    if resp.status_class == "bot_blocked" or kind == "bot":
        return {"valid": False, "status": "bot_blocked", "message": "被亚马逊风控拦截(COOKIE 失效或 IP 被标记)"}
    if resp.status_class == "http_0":
        return {"valid": False, "status": "network", "message": "网络连接失败(SSL/超时), 与 COOKIE 无关, 检查网络环境"}
    # 兜底: 页面既非商品页也非已知失败形态(如只渲染了骨架)。**不能把 status_class 的 "ok" 当结论**
    # (那会让调用方误判为通过), 统一记为 unrecognized, 由调用方按"不阻断的警告"处理。
    if resp.status_class == "ok":
        return {"valid": False, "status": "unrecognized",
                "message": f"探测页无法识别(既无商品标题也无登录/风控标记, kind={kind}); 无法据此判定 COOKIE, 建议人工抽查"}
    return {"valid": False, "status": resp.status_class, "message": f"检测结果异常: {resp.status_class}/{kind}"}


# ---------------------------------------------------------------- 失败自动定性(2026-09-29)

# 对照探测的其它站点(默认美/英/德; 与当前站点相同者自动跳过, 只取前 2 个)
DEFAULT_OTHER_SITES = ("amazon.com", "amazon.co.uk", "amazon.de")


def _probe(config: Config, market: str, asin: str, cookie: str) -> dict:
    """单次探测(指定站点 + 指定 COOKIE; cookie 传空串 = 匿名), 供 `diagnose_cookie` 做对照。

    只发 1 次请求(retries=1), 且**不抛异常** —— 定性失败本身绝不能把流程搞崩。
    """
    cfg = Config(dict(config.as_dict(), MARKET=str(market)))
    cfg.set("COOKIE", cookie)
    url = f"https://{market}/dp/{asin}"
    try:
        fetcher = HttpFetcher(cfg, cookie=cookie) if cookie else HttpFetcher(cfg)
        resp = fetcher.get(url, retries=1)
        text = resp.text or ""
        return {"site": market, "with_cookie": bool(cookie), "status": resp.status_class,
                "kind": analyze_page(text), "len": len(text)}
    except Exception as exc:                       # noqa: BLE001
        # 把异常也带上(定性日志里能直接看出是 SSL/超时/DNS 还是被工具打断), 截断防刷屏
        return {"site": market, "with_cookie": bool(cookie),
                "status": f"异常:{type(exc).__name__}: {exc}"[:120], "kind": "error", "len": 0}


def _same_blocking_page(a: dict, b: dict) -> bool:
    """两页是否"同一张拦截页": 特征一致 + 长度接近(±2%, 至少 ±64 字节)。"""
    if a.get("kind") != b.get("kind") or not a.get("len") or not b.get("len"):
        return False
    tol = max(64, int(max(a["len"], b["len"]) * 0.02))
    return abs(a["len"] - b["len"]) <= tol


def diagnose_cookie(config: Config, result: dict | None = None, *,
                    asin: str = PROBE_ASIN,
                    other_sites: tuple[str, ...] | None = None) -> dict:
    """COOKIE 检查失败时的**自动定性**: 究竟是 COOKIE 失效, 还是本机 IP/客户端形态被标记。

    **只在失败时调用**(正常通过时一个请求都不多打); 代价 = 3 个探测请求(同站匿名 + 其它站 2 个)。
    做什么 = 把项目 2026-09-16 定下的**人工判据自动化**(见 STEP_DEVELOPMENT_GUIDE / CONFIG_GUIDE):
      ① 同一站点**不带 COOKIE(匿名)**再探一次 —— 若与"带 COOKIE"得到**同一张拦截页**, 说明 COOKIE 不是变量;
      ② 再匿名探 2 个**其它站点** —— 若其它站点正常, 说明被标记的是"这个站 + 这份 IP/客户端形态";
      ③ 若其它站点**也全被拦** —— 本机 IP 被大范围标记(更严重, 换 COOKIE 更加无用)。

    返回 `{verdict, message, probes:[{site, with_cookie, status, kind, len}]}`; `verdict` 取值:
      - `ip_or_client_marked` : 该站被标记(匿名同样被拦 + 其它站正常) → **换 COOKIE 无用**
      - `cookie_suspect`      : 带 COOKIE 被拦但匿名正常 → 指向该站 COOKIE 本身(失效/跨站混用/站别错)
      - `cookie_expired`      : 返回登录页 → COOKIE 已过期
      - `network`             : 连接失败, 与 COOKIE 无关
      - `all_sites_blocked`   : 其它站点也被拦 → 本机 IP 被大范围标记
      - `unknown`             : 探测结果无法归因(留人工判断)
    """
    market = str(getattr(config, "market", "") or "")
    cookie = str(config.get("COOKIE", "") or "")
    sites = [s for s in (other_sites if other_sites is not None else DEFAULT_OTHER_SITES)
             if str(s) and str(s) != market][:2]

    probes = [_probe(config, market, asin, cookie), _probe(config, market, asin, "")]
    probes += [_probe(config, s, asin, "") for s in sites]
    with_c, anon = probes[0], probes[1]
    other_bad = [p for p in probes[2:] if p["kind"] != "ok"]
    other_ok = [p for p in probes[2:] if p["kind"] == "ok"]
    other_txt = ("、".join(f"{p['site']}({p['kind']})" for p in probes[2:])) or "无对照站点"

    if with_c["kind"] == "error" or anon["kind"] == "error":
        verdict = "network"
        msg = (f"探测连接失败(与 COOKIE 无关): 本机 {market} 请求异常 —— 检查网络/代理后重试")
    elif _same_blocking_page(with_c, anon) and with_c["kind"] == "bot" and other_ok:
        verdict = "ip_or_client_marked"
        msg = (f"匿名请求同样被拦(**同一张 {with_c['len']} 字节拦截页**), 且其它站点正常({other_txt}) "
               f"→ 结论: **本机 IP/客户端形态在 {market} 被风控, 换 COOKIE 无用**; "
               f"请换网络(热点/代理)、等冷却, 或改用浏览器兜底(FALLBACK: auto)")
    elif with_c["kind"] == "bot" and anon["kind"] == "ok":
        verdict = "cookie_suspect"
        msg = (f"**带 COOKIE 被拦、不带 COOKIE 反而正常**(匿名 kind={anon['kind']}) "
               f"→ 指向 {market} 的 COOKIE 本身(失效/跨站混用/站别错): 请登录**同一站点**后"
               f"F12→Network→复制 document 请求的整条 Cookie 头(一行)替换")
    elif with_c["kind"] == "signin" or anon["kind"] == "signin":
        verdict = "cookie_expired"
        msg = f"返回登录页 → COOKIE 已过期(或未登录该站点), 请重新登录 {market} 并更新 COOKIE"
    elif other_bad and len(other_bad) == len(probes[2:]) and probes[2:]:
        verdict = "all_sites_blocked"
        msg = (f"对照站点**也全被拦**({other_txt}) → 本机 IP 被**大范围**标记"
               f"(换 COOKIE 无用): 优先换网络出口(手机热点/代理)或等冷却")
    else:
        verdict = "unknown"
        msg = (f"探测结果无法归因(带 COOKIE kind={with_c['kind']}/匿名 kind={anon['kind']}; {other_txt}) "
               f"—— 建议人工抽查: `python scripts/check_cookie.py`")
    return {"verdict": verdict, "message": msg, "probes": probes}
