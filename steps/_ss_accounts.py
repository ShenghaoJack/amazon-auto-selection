# -*- coding: utf-8 -*-
"""卖家精灵多账号配额协调(FR-11/12 导出配额用尽自动换号, 2026-09 用户规则)。

规则:
- 主账号(config SS_ACCOUNT, 主 Profile, 可随时使用): 导出按钮提示"当天剩余0次"
  (tooltip: 支持快速导出前2000条/自定义勾选…说明: 闲时(晚7点~次日早8点)可导出前3000条)
  → 配额耗尽, 触发自动换号
- 备用账号(config SS_ACCOUNTS 列表, 独立 Profile ss-profile-b<n>, 有头自动登录):
  **每天 SS_QUOTA_WINDOW_START(默认 20:00)之后才能导出**(窗口=20:00~次日08:00,
  对齐卖家精灵闲时通道); 未到窗口需等待
- 换号后实时读该账号"当天剩余N次"; 备用账号也耗尽 → 换下一个;
  全部耗尽 → 微信+弹窗+日志 提醒提供新账号(加入 config 后重跑续跑)

注意: tooltip 为卖家精灵动态渲染(Element UI el-tooltip), 用宽松文本扫描定位,
真机页面结构变化时仅影响检测命中(检测不到=不拦截, 保持原导出行为)。
"""
from __future__ import annotations

import re
import hashlib
import base64
import json
from urllib.parse import unquote, urlparse
from pathlib import Path
import time
from datetime import datetime, time as dtime

from core import notify

# 配额提示关键字(按钮 hover tooltip 文本; 出现即表示平台在提示配额)
_HINT_KEYS = ("支持快速导出", "当天剩余", "今日剩余", "可导出前", "剩余0次")
# 剩余次数解析: "当天剩余0次" / "当天剩余 5 次" / "剩余12次" / "今日剩余 N 次"
_QUOTA_RES = (
    re.compile(r"(?:当天|今日|本日)?\s*剩余\s*(\d+)\s*次"),
    re.compile(r"可导出前\s*(\d+)\s*条"),
)


class QuotaExhausted(Exception):
    """卖家精灵导出配额耗尽(提示'当天剩余0次'), 触发自动换号。"""

    def __init__(self, hint: str = "", account: str = ""):
        super().__init__(hint or "卖家精灵导出配额耗尽")
        self.hint = hint
        self.account = account


def account_id(account: str) -> str:
    """Stable, non-credential identifier for associating export tasks with an account."""
    return hashlib.sha256(str(account or '').strip().lower().encode('utf-8')).hexdigest()


def _authenticated_sellersprite_body(body: str) -> bool:
    """Require a personalized greeting or an explicit logout, not a guest search box."""
    if any(s in body for s in ('退出登录', '退出登入', 'Log out', 'Sign out')):
        return True
    if re.search(r'(?im)^\s*Welcome\s*[,，]\s*\S+', body):
        return True
    return (('快捷访问' in body or '快捷搜索' in body)
            and bool(re.search(r'(?m)^\s*\S.{1,60}(?:早上好|上午好|中午好|下午好|晚上好)', body)))


def _token_fingerprint(page) -> str:
    cookies = page.context.cookies('https://www.sellersprite.com')
    token = next((unquote(c['value']) for c in cookies if c.get('name') == 'Sprite-X-Token'), '')
    return hashlib.sha256(token.encode('utf-8')).hexdigest() if token else ''


def _bound_session(browser, page, account: str) -> bool:
    try:
        from core import read_json
        record = read_json(str(Path(browser.profile_dir) / '.pipeline-ss-session.json'), default={})
        return (isinstance(record, dict) and record.get('account_id') == account_id(account)
                and bool(record.get('token_hash')) and record['token_hash'] == _token_fingerprint(page))
    except Exception:
        return False


def _remember_session(browser, page, account: str) -> None:
    try:
        from core import write_json
        token_hash = _token_fingerprint(page)
        if token_hash:
            write_json(str(Path(browser.profile_dir) / '.pipeline-ss-session.json'),
                       {'account_id': account_id(account), 'token_hash': token_hash})
    except Exception:
        # Failure to persist only means the next run cannot reuse an alias.
        return


def _server_identity_verified(page, account: str, allow_bound_alias: bool = False) -> bool | None:
    """Match the account to the current token and a server-authenticated page.

    Cookie values are never logged or persisted. A decoded token alone is not
    accepted: the platform must also return its authenticated export page with
    that full email and a logout marker, without redirecting to login.
    """
    try:
        wanted = str(account or '').strip().casefold()
        cookies = {c['name']: unquote(c['value']) for c in page.context.cookies('https://www.sellersprite.com')}
        token = cookies.get('Sprite-X-Token', '')
        parts = token.split('.')
        if len(parts) != 3:
            return False
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)).decode('utf-8'))
        email = str(payload.get('em') or '').strip().casefold()
        if not email or float(payload.get('exp') or 0) <= time.time():
            return False
        is_alias = wanted != email
        if is_alias:
            if not allow_bound_alias:
                return False
            info_raw = cookies.get('rank-login-user-info', '')
            info = json.loads(base64.urlsafe_b64decode(info_raw + '=' * (-len(info_raw) % 4)).decode('utf-8'))
            aliases = {str(info.get(k) or '').strip().casefold() for k in ('account', 'nickname')}
            if '@' in wanted or not wanted or wanted not in aliases:
                return False
    except Exception:
        return None
    try:
        response = page.context.request.get('https://www.sellersprite.com/v2/export-log', timeout=15000)
        parsed = urlparse(response.url)
        if parsed.hostname == 'www.sellersprite.com' and parsed.path == '/w/user/login':
            return False
        if response.status != 200 or parsed.hostname != 'www.sellersprite.com' or parsed.path != '/v2/export-log':
            return None
        text = response.text().casefold()
        if email not in text or not any(s in text for s in ('退出登录', 'log out', 'sign out')):
            return None
        if is_alias:
            # A child account may have its parent's email in the token. Require
            # the exact configured alias on both the authenticated response and
            # the displayed page, in addition to the user-info cookie match.
            pattern = re.compile(r'(?<![\w])' + re.escape(wanted) + r'(?![\w])', re.I)
            if not pattern.search(text):
                return False
            displayed = page.inner_text('body', timeout=5000).casefold()
            if not pattern.search(displayed):
                return False
        return True
    except Exception:
        return None


def login_as_account(browser, page, acct: dict) -> None:
    """Replace the SellerSprite session in the leased profile, then log in explicitly.

    A generic 'logged in' signal cannot identify an existing account.  Discard only
    SellerSprite cookies and require an actual login form before submitting the
    requested credentials. Other sites and the profile itself are left intact.
    """
    from core import BrowserError
    from core.browser import BrowserAutomation

    account = str(acct.get('account') or '').strip()
    password = str(acct.get('password') or '')
    if not account:
        raise BrowserError('卖家精灵账号未配置，无法核验当前账号')
    verification = _server_identity_verified(page, account, _bound_session(browser, page, account))
    if verification is True:
        BrowserAutomation.mark_login_verified(browser.profile_dir)
        import logging
        logging.getLogger('pipeline').info('已向卖家精灵核验当前账号会话，直接复用')
        return page
    if verification is None:
        raise BrowserError('卖家精灵会话暂时无法核验，已保留现有登录态，请检查网络后续跑')
    if not password:
        raise BrowserError('卖家精灵密码未配置，无法自动切换账号')
    BrowserAutomation.reset_login_verified(browser.profile_dir)
    try:
        page.context.clear_cookies(domain=re.compile(r'(^|\.)sellersprite\.com$', re.I))
        try:
            browser.goto(page, 'https://www.sellersprite.com/w/user/login')
        except Exception:
            # A stalled navigation can leave the task tab unusable. Retry once
            # in a fresh task-owned tab; the account identity still has to be
            # verified after login before any platform action is allowed.
            try:
                page.close()
            except Exception:
                pass
            page = browser.new_page()
            browser.goto(page, 'https://www.sellersprite.com/w/user/login')
        page.wait_for_selector('input[name=email]', state='attached', timeout=15000)
        fields = ('input[name=email]', 'input[name=account]', 'input[name=username]',
                  'input[placeholder*=账号]', 'input[placeholder*=邮箱]')
        if browser._first_visible(page, fields) is None:
            browser._switch_to_password_tab(page)
        if browser._first_visible(page, fields) is None:
            raise BrowserError('未能确认旧会话已退出，停止切换以防使用错误账号')
        # Do not navigate away while the asynchronous login is still completing.
        # login_sellersprite/check_sellersprite_login may navigate immediately and
        # interrupt the request, or treat an inconclusive page as logged in.
        submitted = False
        for attempt in range(3):
            if browser._fill_sellersprite_form(page, account, password):
                submitted = True
                break
            if attempt < 2:
                page.wait_for_timeout(1000)
        if not submitted:
            # The platform may finish an asynchronous login while the form
            # disappears, making _fill_sellersprite_form report False.
            if _server_identity_verified(page, account, True) is True:
                _remember_session(browser, page, account)
                BrowserAutomation.mark_login_verified(browser.profile_dir)
                return page
            raise BrowserError('无法提交卖家精灵登录表单，请检查登录页或验证码')
        deadline = time.monotonic() + 60
        probe_after = time.monotonic() + 8
        probed = False
        while time.monotonic() < deadline:
            if _server_identity_verified(page, account, True):
                _remember_session(browser, page, account)
                BrowserAutomation.mark_login_verified(browser.profile_dir)
                # The login redirect can remain stuck even though the platform
                # has confirmed the session; replace only our own stalled tab.
                try:
                    if '/v2/welcome' in str(page.url) and page.locator('body').count() == 0:
                        page.close()
                        return browser.new_page()
                except Exception:
                    pass
                return page
            try:
                body = page.inner_text('body', timeout=2000) or ''
                if _authenticated_sellersprite_body(body) and _server_identity_verified(page, account, True):
                    _remember_session(browser, page, account)
                    BrowserAutomation.mark_login_verified(browser.profile_dir)
                    return page
            except Exception:
                pass
            if not probed and time.monotonic() >= probe_after:
                # A login redirect can stall while cookies have already been set.
                # Verify a fresh page in the same context rather than guessing.
                probed = True
                probe = None
                recovered = False
                try:
                    # A stuck redirect can block subsequent pages in the same
                    # context. Close only our own stalled login tab, never the
                    # user's browser, then verify a replacement tab.
                    if '/v2/welcome' in str(page.url) and page.locator('body').count() == 0:
                        page.close()
                    probe = browser.new_page()
                    browser.goto(probe, 'https://www.sellersprite.com/v2/welcome', timeout=25000)
                    if (_authenticated_sellersprite_body(probe.inner_text('body', timeout=3000) or '')
                            and _server_identity_verified(probe, account, True)):
                        _remember_session(browser, probe, account)
                        BrowserAutomation.mark_login_verified(browser.profile_dir)
                        recovered = True
                        return probe
                except Exception:
                    pass
                finally:
                    if probe is not None and not recovered:
                        try:
                            probe.close()
                        except Exception:
                            pass
                if page.is_closed():
                    raise BrowserError('登录跳转页面卡住，重新打开后仍无法确认账号，请检查网络和页面')
            page.wait_for_timeout(500)
        raise BrowserError('自动登录后未检测到明确的已登录标识，已停止以避免误用账号')
    except BrowserError:
        raise
    except Exception as exc:
        raise BrowserError(f'卖家精灵账号切换失败：{type(exc).__name__}；请检查登录页或验证码') from exc


def parse_quota(text: str) -> int | None:
    """从提示文本解析'当天剩余N次'; 无法解析返回 None(未知, 不拦截)。"""
    if not text:
        return None
    for rx in _QUOTA_RES:
        m = rx.search(text)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                return None
    return None


def hint_matches(text: str) -> bool:
    """文本是否命中配额提示(出现任一关键字即视为配额提示区)。"""
    return any(k in text for k in _HINT_KEYS)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def backup_accounts(cfg) -> list[dict]:
    """解析 config SS_ACCOUNTS 中的**备用**账号(非 is_main 项) → [{account,password,note,index}]。

    SS_ACCOUNTS 统一账号列表(2026-09): 每项可带 is_main: true 标记主账号(随时可用);
    未标记的项均为备用账号(仅每天窗口起可导出)。无 is_main 项时(旧格式)全部视为备用。
    """
    items = _parse_accounts(cfg)
    return [_strip_main_flag(a) for a in items if not a.get("is_main")]


def account_plan(cfg) -> list[dict]:
    """统一账号计划(步骤11/12 配额换号用): 主账号在前 + 备用账号依次。

    主账号来源: SS_ACCOUNTS 中 is_main:true 的项(取首个);
    无 is_main 主项时回退旧字段 SS_ACCOUNT/SS_PASSWORD(缺省则无主账号)。
    备用账号 = SS_ACCOUNTS 中非主项(独立 Profile, 仅每天窗口起可用)。
    返回 [{kind: main|backup, index(备用序号), account, password, note}]。
    """
    items = _parse_accounts(cfg)
    mains = [a for a in items if a.get("is_main")]
    backups = [a for a in items if not a.get("is_main")]
    plan: list[dict] = []
    if mains:
        m = mains[0]
        plan.append(
            {
                "kind": "main",
                "note": m.get("note") or "主账号",
                "account": m["account"],
                "password": m["password"],
            }
        )
    else:
        acc = str(cfg.get("SS_ACCOUNT", "") or "").strip()
        pwd = str(cfg.get("SS_PASSWORD", "") or "").strip()
        if acc:
            plan.append({"kind": "main", "note": "主账号", "account": acc, "password": pwd})
    for bi, b in enumerate(backups, 1):
        plan.append(
            {
                "kind": "backup",
                "index": bi,
                "account": b["account"],
                "password": b["password"],
                "note": b.get("note") or f"备用账号{bi}",
            }
        )
    return _reorder_by_active(cfg, plan)


def _reorder_by_active(cfg, plan: list[dict]) -> list[dict]:
    """SS_ACTIVE_ACCOUNT 手动指定本轮起始账号(优先级提到首位; 配额0仍自动换号兜底)。

    取值: 空/auto=默认(主账号优先); main(或"主账号")=主账号;
    backupN(或"备用N")=第 N 个备用; 也可直接填账号字符串(与 account 匹配, 忽略大小写)。
    手动指定仅调整**使用顺序**, 不绕过规则: 被指定为起点的备用账号仍受 20:00 窗口限制;
    该账号配额耗尽时仍自动换下一账号。
    """
    active = str(cfg.get("SS_ACTIVE_ACCOUNT", "") or "").strip().lower()
    if not active or active in ("auto", "自动", "0"):
        return plan
    idx = None
    for i, a in enumerate(plan):
        if str(a.get("account") or "").strip().lower() == active:
            idx = i
            break
        if a["kind"] == "main" and active in ("main", "主账号", "主"):
            idx = i
            break
        if a["kind"] == "backup" and active in (
            f"backup{a.get('index')}",
            f"备用{a.get('index')}",
            f"备用账号{a.get('index')}",
        ):
            idx = i
            break
    if idx is None:
        return plan
    target = plan.pop(idx)
    return [target] + plan


def _parse_accounts(cfg) -> list[dict]:
    """解析 SS_ACCOUNTS 原始列表 → 项含 account/password/note/is_main/index(原始序号)。"""
    out: list[dict] = []
    raw = cfg.get("SS_ACCOUNTS") or []
    if isinstance(raw, str):  # 容错: 单账号字符串
        raw = [raw]
    for i, item in enumerate(raw, 1):
        if isinstance(item, dict):
            acc = str(item.get("account") or "").strip()
            pwd = str(item.get("password") or "").strip()
            note = str(item.get("note") or "").strip()
            is_main = _truthy(item.get("is_main"))
        elif isinstance(item, str):
            acc, _, pwd = item.partition(":")
            acc, pwd = acc.strip(), pwd.strip()
            note = ""
            is_main = False
        else:
            continue
        if not acc:
            continue
        out.append(
            {
                "index": i,
                "account": acc,
                "password": pwd,
                "note": note or (f"主账号" if is_main else f"备用账号{i}"),
                "is_main": is_main,
            }
        )
    return out


def _strip_main_flag(a: dict) -> dict:
    return {
        "index": a["index"],
        "account": a["account"],
        "password": a["password"],
        "note": a.get("note") or f"备用账号{a['index']}",
    }


def window_start(cfg=None) -> str:
    cfg = cfg or {}
    return str(cfg.get("SS_QUOTA_WINDOW_START", "") or "").strip() or "20:00"


def window_end(cfg=None) -> str:
    cfg = cfg or {}
    return str(cfg.get("SS_QUOTA_WINDOW_END", "") or "").strip() or "08:00"


def _hhmm(value: str) -> dtime:
    try:
        h, m = value.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return dtime(20, 0)


def window_open(cfg, now: datetime | None = None) -> bool:
    """备用账号可用窗口: [窗口起点(默认20:00), 次日窗口终点(默认08:00))。

    **起止相同(如 00:00/00:00) = 无窗口限制, 备用账号全天可用**。
    """
    cfg = cfg or {}
    s_str, e_str = window_start(cfg), window_end(cfg)
    if s_str == e_str:
        return True  # 起止相同 = 全天可用(关闭窗口限制)
    now = now or datetime.now()
    cur = now.time()
    s, e = _hhmm(s_str), _hhmm(e_str)
    if s <= e:  # 同日区间(如 08:00-20:00)
        return s <= cur < e
    return cur >= s or cur < e  # 跨天: 20:00~24:00 与 00:00~08:00


def wait_until_window(ctx, log, cfg=None) -> None:
    """未到备用账号可用窗口则等待(分片 sleep + 日志); dry-run 跳过。

    阻塞至窗口起点; 期间每 5 分钟打一行日志便于观测; Ctrl+C 可中断(状态 RUNNING,
    重跑从该步骤续)。**等待秒数计入 ctx.add_step_wait, 不计入步骤活动耗时**
    (步骤耗时 = 完成-开始-等待, 由编排器计算)。
    """
    if ctx.dry_run:
        return
    now = datetime.now()
    if window_open(cfg or ctx.config, now):
        return
    s = _hhmm(window_start(cfg or ctx.config))
    target = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
    if target <= now:
        target += _day()
    log(f"备用账号可用窗口未到(当前 {now:%H:%M}, 窗口 {window_start(ctx.config)} 起), "
        f"等待至 {target:%H:%M} 后执行…(可 Ctrl+C 中断, 重跑续)")
    while True:
        now = datetime.now()
        if now >= target:
            log(f"已到窗口 {target:%H:%M}, 继续执行")
            return
        left = (target - now).total_seconds()
        if left <= 300:
            slp = max(1.0, left)
            time.sleep(slp)
            ctx.add_step_wait(slp)
            continue
        log(f"仍在等待备用账号窗口(约 {int(left // 60)} 分钟后到点)")
        time.sleep(300)
        ctx.add_step_wait(300)


def _day():
    from datetime import timedelta
    return timedelta(days=1)


def notify_need_new_account(ctx, detail: str = "") -> None:
    """所有可用账号配额耗尽: 微信+弹窗+日志 提醒用户提供新账号后加入 config 重跑。"""
    msg = (
        "卖家精灵导出配额耗尽且无可用备用账号 —— 请提供新账号"
        "(卖家精灵账号+密码)加入 config.yaml 的 SS_ACCOUNTS 后重跑续跑。"
        + (f" 详情: {detail}" if detail else "")
    )
    try:
        notify.append_alert(
            ctx.paths.monitor_file("monitor-alerts.txt"), notify.CRIT, msg
        )
    except Exception:
        pass
    try:
        webhook = str(ctx.config.get("WECOM_WEBHOOK", "") or "")
        if webhook:
            notify.push_wecom(webhook, notify.with_context(msg, ctx.config))
    except Exception:
        pass
    try:
        notify.popup_critical(msg)
    except Exception:
        pass


def read_export_quota(page, hover_selector: str = "") -> tuple[int | None, str]:
    """悬停导出按钮读取配额 tooltip。

    返回 (remaining:int|None, hint_text:str):
      remaining==0  → 配额耗尽(触发换号); None → 未检测到(不拦截);
      >0 → 该账号当天剩余次数。
    定位: ①按钮 aria-describedby 指向的 tooltip 容器; ②常见 tooltip popper
    (.el-tooltip__popper / [role=tooltip]); ③JS 扫描可见元素文本(兜底)。
    2026-09-08: **悬停重试**——tooltip 为 hover 后延迟出现的浮层, 一次性悬停
    常因时机未到读空(曾致主账号 0 配额未被识别而空转); 改为最多 3 轮
    「移开→重新悬停→等待→读取」, 命中即停。
    """
    text = ""
    try:
        for attempt in range(3):
            if hover_selector:
                try:
                    page.mouse.move(5, 5)          # 移开, 让旧 tooltip 收起
                except Exception:
                    pass
                page.hover(hover_selector)
                time.sleep(0.4 + 0.3 * attempt)   # 逐轮递增等待, 覆盖慢渲染
            text = _read_tooltip_text(page)
            if text:
                break
            time.sleep(0.5)
    except Exception:
        return None, ""
    if not hint_matches(text):
        return None, text
    return parse_quota(text), text


def _read_tooltip_text(page) -> str:
    """宽松读取当前可见 tooltip 文本(配额提示)。"""
    # ① aria-describedby 指向容器
    try:
        ids = page.eval_on_selector_all(
            "[aria-describedby]",
            "els => els.map(e => e.getAttribute('aria-describedby')).filter(Boolean)",
        )
        for tid in (ids or [])[:5]:
            try:
                loc = page.locator(f"#{tid}")
                if loc.count() and loc.first.is_visible():
                    t = (loc.first.inner_text() or "").strip()
                    if t:
                        return t
            except Exception:
                continue
    except Exception:
        pass
    # ② 常见 tooltip popper 容器
    for sel in (".el-tooltip__popper", "[role=tooltip]", "[class*=tooltip] [class*=content]"):
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 6)
            for i in range(n):
                try:
                    if loc.nth(i).is_visible():
                        t = (loc.nth(i).inner_text() or "").strip()
                        if t and hint_matches(t):
                            return t
                except Exception:
                    continue
        except Exception:
            continue
    # ③ JS 兜底: 扫描可见元素文本(性能可控: 单次一次性)
    try:
        t = page.evaluate(
            """() => {
                const keys = ["支持快速导出","当天剩余","今日剩余","可导出前","剩余0次"];
                const el = [...document.querySelectorAll('body *')].find(e => {
                    if (e.children.length > 0) return false;
                    const s = getComputedStyle(e);
                    if (s.display === 'none' || s.visibility === 'hidden') return false;
                    const t = (e.textContent || '').trim();
                    return t && keys.some(k => t.includes(k));
                });
                return el ? el.textContent.trim().slice(0, 400) : '';
            }"""
        )
        if t:
            return str(t)
    except Exception:
        pass
    return ""
