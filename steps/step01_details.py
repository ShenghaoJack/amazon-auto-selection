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
import time
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
        browser_started = False
        if fallback_mode == "always":
            self.log(ctx, "浏览器兜底=always: 步骤1 全部 ASIN 走真实浏览器抓取")
        elif fallback_mode == "auto":
            self.log(ctx, "浏览器兜底=auto: 直连失败的 ASIN(bot_blocked/http_0 等)自动转浏览器补抓")
        for i, asin in enumerate(pending, 1):
            rec = None
            if fallback_mode == "always":
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
                if not rec["ok"] and fallback_mode == "auto" and self._needs_browser(rec["status"]):
                    rec = self._browser_fetch_one(ctx, asin)
                    browser_started = browser_started or rec is not None
            if rec is None:                      # 浏览器会话不可用 -> 保留直连结果(如实记失败)
                rec = {
                    "request_asin": asin, "ok": False, "status": "http_0",
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

    def _browser_fetch_one(self, ctx: StepContext, asin: str) -> dict | None:
        """用真实浏览器打开该 ASIN 详情页, 解析标题/五点; 会话不可用返回 None。

        与 HTTP 直连的记录结构保持一致(ok/status/title/bullets/fetched_at), 仅多一个
        `source` 字段标明来源, 便于审计"这条是直连抓的还是浏览器补的"。
        每 ASIN 最多 _BROWSER_MAX_TRIES 次(依次换 URL 形态), 全失败则如实记 ok=False。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        last_status = "failed"
        for attempt in range(1, self._BROWSER_MAX_TRIES + 1):
            try:
                with ctx.browser("amazon") as browser:
                    page = browser.new_page()
                    try:
                        for url in url_candidates(ctx.config.market, asin):
                            try:
                                browser.goto(page, url)
                                time.sleep(2.0)          # 页面渲染缓冲(与步骤10 一致)
                                text = page.content() or ""
                            except Exception as exc:     # noqa: BLE001
                                self.log(ctx, f"浏览器补抓 {asin} 打开失败({exc}), 换形态")
                                continue
                            title, bullets = parse_product_html(text)
                            if title:
                                return {
                                    "request_asin": asin, "ok": True, "status": "ok",
                                    "fetched_at": now, "title": title,
                                    "bullets": list(bullets or []), "source": "browser",
                                }
                            low = (text or "").lower()
                            if "page not found" in low or "page introuvable" in low:
                                last_status = "not_found"
                            elif any(m in low for m in ("captcha", "robot check",
                                                        "verify you are human")):
                                last_status = "bot_blocked"
                            else:
                                last_status = "parse_failed"
                        self.log(ctx, f"浏览器补抓 {asin} 第 {attempt}/{self._BROWSER_MAX_TRIES} 次未取到"
                                      f"(status={last_status})")
                    finally:
                        self._close_page(page)
            except Exception as exc:                          # noqa: BLE001
                self.log(ctx, f"浏览器会话不可用({exc}), 该 ASIN 保留直连失败结果: {asin}")
                return None
        return {
            "request_asin": asin, "ok": False, "status": last_status,
            "fetched_at": now, "title": "", "bullets": [], "source": "browser",
        }

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
