"""FR-01 榜单详情抓取(HTTP 直连)。

输入: FR-00 产物 <CAT>-bestseller-newrelease.json 中的 union 并集 ASIN
输出: <CAT>-dual-details.jsonl, 每行 {request_asin, ok, status, fetched_at, title, bullets}

逻辑: 读取并集 -> 与已完成(ok=True)记录比对得待抓列表(断点续爬)
      -> 逐个 fetch_product_page(内部已做重试/限流/状态分类)
      -> 成功与失败都追加写 JSONL(dedup_key=request_asin), 失败行 ok=false
      -> 每 10 条进度日志
容错: 断点续爬(ok 记录跳过); 风控失败仅记录不中断
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import deque
from datetime import datetime

from core.amazon_variation import url_candidates
from core.errors import StepDataError
from core.http_client import parse_product_html
from core.io_utils import append_jsonl, atomic_write_text, read_json, read_jsonl
from core.validation import require_file
from steps.base import BaseStep, StepContext

# 落盘与进度的批次大小: 每 N 条追加一次 JSONL 并打印一行进度(原始口径, 2026-09-15 保持)
_FLUSH_EVERY = 10


class Step01Details(BaseStep):
    number = 1
    title = "榜单详情抓取"
    description = "FR-01: HTTP 直连抓取双榜 ASIN 详情(标题/五点)并追加写 JSONL"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        self._union_path = ctx.resolve_artifact(f"{self._cat}-bestseller-newrelease.json")
        require_file(self._union_path, f"{self._cat}-bestseller-newrelease.json")

    @property
    def artifacts(self) -> list[str]:
        return [f"{self._cat}-dual-details.jsonl"]

    def run(self, ctx: StepContext) -> None:
        union = self._read_union()
        jsonl_path = ctx.resolve_artifact(self.artifacts[0])
        self.log(ctx, f"并集 ASIN 数: {len(union)}")

        existing = read_jsonl(jsonl_path)
        done = {str(r.get("request_asin")) for r in existing if r.get("ok")}
        pending = [a for a in union if a not in done]
        self.log(ctx, f"待抓取: {len(pending)} 已完成: {len(done)}")
        if not pending:
            append_jsonl(jsonl_path, [], dedup_key="request_asin")
            self.log(ctx, "无待抓取 ASIN, 跳过")
            return

        self._prune_retried(jsonl_path, set(pending))
        http = ctx.http()
        batch = []
        appended_total = 0
        ok_total = sum(1 for r in existing if r.get("ok"))
        # 浏览器兜底开关(2026-09-15): auto=直连失败的 ASIN 自动转真实浏览器补抓(默认);
        # always=全部直接走浏览器; off=只用 HTTP 直连。见 _needs_browser()。
        fallback_mode = self._browser_fallback_mode(ctx)
        raw_win = ctx.config.get("STEP01_DIRECT_BREAKER_WINDOW", self._DIRECT_BREAKER_WINDOW_DEFAULT)
        breaker_window = (
            max(0, int(raw_win))
            if raw_win is not None
            else self._DIRECT_BREAKER_WINDOW_DEFAULT
        )
        raw_rate = ctx.config.get("STEP01_DIRECT_BREAKER_MIN_RATE", self._DIRECT_BREAKER_MIN_RATE_DEFAULT)
        breaker_min_rate = (
            max(0.0, min(1.0, float(raw_rate)))
            if raw_rate is not None
            else self._DIRECT_BREAKER_MIN_RATE_DEFAULT
        )
        breaker_on = fallback_mode == "auto" and (not ctx.dry_run) and breaker_window > 0
        recent_http: deque[bool] = deque(maxlen=max(1, breaker_window))
        direct_tripped = False

        if fallback_mode == "always":
            self.log(ctx, "浏览器兜底=always: 步骤1 全部 ASIN 走真实浏览器抓取(单会话复用)")
        elif fallback_mode == "auto":
            self.log(ctx, "浏览器兜底=auto: 直连失败的 ASIN(bot_blocked/http_0 等)自动转单会话浏览器补抓"
                          + (f"(直连熔断: 窗口 {breaker_window} 条, 门槛 {breaker_min_rate:.0%})" if breaker_on else ""))
        self._begin_shared_browser()
        try:
            for i, asin in enumerate(pending, 1):
                rec = None
                if fallback_mode == "always" or direct_tripped:
                    rec = self._browser_fetch_one(ctx, asin)
                else:
                    page = http.fetch_product_page(asin)
                    rec = {
                        "request_asin": page.request_asin,
                        "ok": page.ok,
                        "status": page.status,
                        "fetched_at": page.fetched_at,
                        "title": page.title,
                        "bullets": list(page.bullets),
                    }
                    if breaker_on and rec["status"] not in ("not_found", "skipped_page_not_found"):
                        recent_http.append(bool(rec["ok"]))
                        if len(recent_http) >= breaker_window:
                            ok_in_win = sum(1 for x in recent_http if x)
                            rate = ok_in_win / len(recent_http)
                            if rate < breaker_min_rate:
                                direct_tripped = True
                                self.log(
                                    ctx,
                                    f"步骤1 直连触发滑动窗口熔断: 最近 {len(recent_http)} 条仅成功 "
                                    f"{ok_in_win} 条({rate:.0%}<{breaker_min_rate:.0%}), "
                                    f"剩余 {len(pending) - i} 个 ASIN 直接转单会话新版无头浏览器抓取",
                                )
                    if not rec["ok"] and fallback_mode == "auto" and self._needs_browser(rec["status"]):
                        fallback = self._browser_fetch_one(ctx, asin)
                        if fallback is not None:
                            rec = dict(fallback, http_status=rec["status"])
                if rec is None:                      # 浏览器会话不可用 -> 保留直连结果(如实记失败)
                    rec = {
                        "request_asin": asin, "ok": False, "status": "browser_unavailable", "source": "browser",
                        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "title": "", "bullets": [],
                    }
                if rec["ok"]:
                    ok_total += 1
                batch.append(rec)
                # 每 10 条落盘一次并打印进度(**保持原口径不变**: 用户 2026-09-15 明确要求
                # 与原有日志提示一致 —— 不额外插入逐条日志, 也不改批次大小)。
                if i % _FLUSH_EVERY == 0:
                    appended, skipped = append_jsonl(jsonl_path, batch, dedup_key="request_asin")
                    appended_total += appended
                    # 本批结果分布(2026-09-15, 用户要求保持原进度行不变的前提下补一行诊断):
                    # 只报"累计成功N"时看不出失败原因 —— 实测 UK 站每批 10 条只成功 2 条(其余
                    # bot_blocked), 必须能一眼看出是被风控还是真的抓不到。
                    _dist: dict[str, int] = {}
                    for _r in batch:
                        _k = "ok" if _r.get("ok") else str(_r.get("status") or "?")
                        _dist[_k] = _dist.get(_k, 0) + 1
                    batch = []
                    self.log(ctx, f"进度: {i}/{len(pending)} 本次追加{appended}条(跳过{skipped}条) 累计成功{ok_total}")
                    self.log(ctx, "  本批结果: " + ", ".join(
                        f"{k}×{v}" for k, v in sorted(_dist.items(), key=lambda x: -x[1])))
        finally:
            self._close_shared_browser()
        if batch:
            appended, skipped = append_jsonl(jsonl_path, batch, dedup_key="request_asin")
            appended_total += appended
            self.log(ctx, f"末批追加{appended}条(跳过{skipped}条)")
        self.log(ctx, f"榜单详情抓取完成: 处理{len(pending)}条 追加{appended_total}条")

    def _read_union(self) -> list[str]:
        data = read_json(self._union_path, default=None)
        if not isinstance(data, dict):
            raise StepDataError(f"榜单产物格式错误(应为对象): {self._union_path}")
        raw = [str(a).strip() for a in data.get("union", []) or []]
        return list(dict.fromkeys(a for a in raw if a))

    # ---------------- 浏览器兜底(2026-09-15) ----------------
    # 背景: 实测 amazon.co.uk 对 HTTP 直连(curl_cffi)返回 200+captcha 壳页(3805 字节),
    # 且**匿名请求同样被拦**、把请求间隔从 2-4s 放到 12-18s 也无改善 —— 该 IP 已被标记。
    # 而真实浏览器在同一台机器上能正常打开这些页面(步骤0 刚成功抓了 120 个 ASIN)。
    # 因此对直连失败的 ASIN 增加"真实浏览器补抓"兜底, 与步骤10 的做法一致。
    _BROWSER_MAX_TRIES = 2
    _DIRECT_BREAKER_WINDOW_DEFAULT = 10
    _DIRECT_BREAKER_MIN_RATE_DEFAULT = 0.30
    _BROWSER_CHECKPOINT_WAIT_DEFAULT = 295.0

    def _begin_shared_browser(self) -> None:
        self._shared_browser_active = True
        self._shared_cm = None
        self._shared_browser = None
        self._shared_page = None
        self._shared_crawl_slot = None
        self._shared_hits = 0
        self._shared_captcha_streak = 0

    def _ensure_shared_page(self, ctx: StepContext):
        if self._shared_browser is None:
            from core.amz_crawl_lock import AmzCrawlSlot, DEFAULT_WAIT_SECONDS as _AMZ_WAIT_DEFAULT, resolve_max_concurrent
            slot = AmzCrawlSlot(
                max_concurrent=resolve_max_concurrent(ctx.config),
                wait_seconds=float(ctx.config.get("AMZ_CRAWL_LOCK_WAIT", _AMZ_WAIT_DEFAULT) or _AMZ_WAIT_DEFAULT),
                label=f"{ctx.config.cat or 'pipeline'}·步骤1浏览器",
                log=lambda m: self.log(ctx, m),
                warn=lambda m: ctx.log.warning(f"[Step01Details] {m}"),
                enabled=not ctx.dry_run,
            )
            slot.acquire()
            try:
                cm = ctx.browser("amazon")
                browser = cm.__enter__()
                self._shared_crawl_slot = slot
                self._shared_cm = cm
                self._shared_browser = browser
                self._shared_page = browser.new_page()
            except Exception:
                slot.release()
                raise
        elif self._shared_page is None:
            self._shared_page = self._shared_browser.new_page()
        return self._shared_browser, self._shared_page

    def _close_shared_browser(self) -> None:
        page = getattr(self, "_shared_page", None)
        cm = getattr(self, "_shared_cm", None)
        slot = getattr(self, "_shared_crawl_slot", None)
        self._shared_page = None
        self._shared_browser = None
        self._shared_cm = None
        self._shared_crawl_slot = None
        self._shared_browser_active = False
        self._shared_hits = 0
        self._shared_captcha_streak = 0
        if page is not None:
            self._close_page(page)
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if slot is not None:
            try:
                slot.release()
            except Exception:  # noqa: BLE001
                pass

    def _browser_fallback_mode(self, ctx: StepContext) -> str:
        """浏览器兜底模式: auto(默认, 仅失败转浏览器) / always(全走浏览器) / off。

        **2026-09-16 起统一走全局兜底开关 `FALLBACK`**(用户定调: "太多参数了, 我只需要个兜底的就行");
        `STEP01_BROWSER_FALLBACK` 作为本步骤的历史专用键**仍兼容** —— 两份都写时以它为准
        (它是更细的开关: 还能表达 `always`=全部走浏览器)。
        """
        raw = str(ctx.config.get("STEP01_BROWSER_FALLBACK")
                  or ctx.config.get("FALLBACK", "auto") or "auto").strip().lower()
        if raw in ("off", "false", "0", "no", "none"):
            return "off"
        if raw in ("always", "all", "force", "true", "1", "yes"):
            return "always"
        return "auto"

    @staticmethod
    def _needs_browser(status: str) -> bool:
        """该失败状态是否值得转浏览器补抓(风控/连接类转; 404 类不转, 转了也是 404)。"""
        return str(status or "") in ("bot_blocked", "blocked_page", "http_0",
                                     "http_429", "http_503", "parse_failed")

    def _browser_fetch_with_page(self, ctx: StepContext, browser, page, asin: str, now: str) -> dict:
        urls = url_candidates(ctx.config.market, asin)
        url_idx = 0
        last_status = "failed"
        retried_once = False
        signin_streak = 0
        window_waited = False
        raw_ckpt = ctx.config.get("BROWSER_CHECKPOINT_WAIT")
        ckpt_wait = (
            self._BROWSER_CHECKPOINT_WAIT_DEFAULT
            if raw_ckpt is None
            else max(0.0, float(raw_ckpt))
        )

        while True:
            url = urls[url_idx % len(urls)]
            text = ""
            final_url = url
            try:
                browser.goto(page, url)
                if not ctx.dry_run:
                    time.sleep(random.uniform(1.2, 2.2))
                    try:
                        page.mouse.wheel(0, random.randint(200, 600))
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(random.uniform(1.0, 2.3))
                text = page.content() or ""
                final_url = str(getattr(page, "url", "") or url)
            except Exception as exc:  # noqa: BLE001
                last_status = "http_0"
                self.log(ctx, f"浏览器补抓 {asin} 打开失败({exc})")
                if not ctx.dry_run and not retried_once:
                    retried_once = True
                    time.sleep(8.0)
                    continue
                break

            title, bullets = parse_product_html(text)
            if title:
                return {
                    "request_asin": asin, "ok": True, "status": "ok",
                    "fetched_at": now, "title": title,
                    "bullets": list(bullets or []), "source": "browser",
                }
            low = text.lower()
            if any(k in low for k in ("page not found", "page introuvable", "seite nicht gefunden")):
                return {
                    "request_asin": asin, "ok": False, "status": "not_found",
                    "fetched_at": now, "title": "", "bullets": [], "source": "browser",
                }
            if "/checkpoint/continue" in final_url.lower() or "ap/signin" in low:
                last_status = "bot_blocked"
                if not ctx.dry_run:
                    signin_streak += 1
                    if signin_streak < 3 and not window_waited:
                        time.sleep(6.0)
                        continue
                    if signin_streak >= 3 and not window_waited:
                        window_waited = True
                        self.log(
                            ctx,
                            f"浏览器补抓 {asin} 连续 3 次命中 signin/checkpoint → "
                            f"保持会话原地等待 {ckpt_wait:.0f} 秒后重试…",
                        )
                        if ckpt_wait > 0:
                            time.sleep(ckpt_wait)
                        continue
                break
            if any(m in low for m in ("captcha", "robot check", "verify you are human")):
                last_status = "bot_blocked"
                if not ctx.dry_run and not retried_once:
                    retried_once = True
                    pause = random.uniform(12.0, 18.0)
                    self.log(ctx, f"浏览器补抓 {asin} 偶发 captcha → 冷却 {pause:.1f} 秒原地重试 1 次")
                    time.sleep(pause)
                    continue
                break
            last_status = "parse_failed"
            if not ctx.dry_run and not retried_once and len(urls) > 1:
                retried_once = True
                url_idx = 1
                time.sleep(5.0)
                continue
            break

        return {
            "request_asin": asin, "ok": False, "status": last_status,
            "fetched_at": now, "title": "", "bullets": [], "source": "browser",
        }

    def _browser_fetch_one(self, ctx: StepContext, asin: str) -> dict | None:
        """用真实浏览器打开该 ASIN 详情页, 解析标题/五点; 会话不可用返回 None。

        2026-11-22 优化: 在 run() 内全程复用同一个 Chrome 持久会话(避免逐 ASIN 开关
        浏览器触发亚马逊 /checkpoint/continue), 遇 404 立刻返回不空转, 每 25 条长冷却。
        单独调用时(如单元测试)仍自动开关临时会话保持兼容。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if getattr(self, "_shared_browser_active", False):
            try:
                browser, page = self._ensure_shared_page(ctx)
            except Exception as exc:  # noqa: BLE001
                self.log(ctx, f"浏览器会话不可用({exc}), 该 ASIN 保留直连失败结果: {asin}")
                self._close_shared_browser()
                self._shared_browser_active = True
                return None
            long_every = max(0, int(ctx.config.get("FETCH_LONG_EVERY", 25) or 0))
            if not ctx.dry_run and long_every > 0 and self._shared_hits > 0 and self._shared_hits % long_every == 0:
                extra = random.uniform(
                    float(ctx.config.get("FETCH_LONG_MIN", 8) or 8),
                    float(ctx.config.get("FETCH_LONG_MAX", 15) or 15),
                )
                self.log(ctx, f"步骤1 浏览器长间隔冷却 {extra:.1f} 秒(已补抓 {self._shared_hits} 条)")
                time.sleep(extra)
            self._shared_hits += 1
            rec = self._browser_fetch_with_page(ctx, browser, page, asin, now)
            if not ctx.dry_run:
                if not rec.get("ok") and rec.get("status") == "bot_blocked":
                    self._shared_captcha_streak = getattr(self, "_shared_captcha_streak", 0) + 1
                    if self._shared_captcha_streak >= 2:
                        steps = (45.0, 90.0, 180.0)
                        step_wait = steps[min(self._shared_captcha_streak - 2, len(steps) - 1)]
                        self.log(
                            ctx,
                            f"步骤1 浏览器连续 {self._shared_captcha_streak} 次命中风控/captcha → "
                            f"阶梯冷却 {step_wait:.0f} 秒让本机 IP 恢复…",
                        )
                        time.sleep(step_wait)
                else:
                    self._shared_captcha_streak = 0
            return rec

        try:
            with ctx.browser("amazon") as browser:
                page = browser.new_page()
                try:
                    return self._browser_fetch_with_page(ctx, browser, page, asin, now)
                finally:
                    self._close_page(page)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, f"浏览器会话不可用({exc}), 该 ASIN 保留直连失败结果: {asin}")
            return None

    @staticmethod
    def _close_page(page) -> None:
        try:
            page.close()
        except Exception:
            pass

    def _prune_retried(self, path: str, keys: set) -> None:
        if not os.path.exists(path):
            return
        records = read_jsonl(path)
        kept = [r for r in records if str(r.get("request_asin")) not in keys]
        if len(kept) == len(records):
            return
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
        atomic_write_text(path, content)
