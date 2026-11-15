"""FR-12 查竞品(第三方数据平台): 提交导出任务。

输入: 全部变体清单(<CAT>-variants.txt, 2026-09-09 起步骤10 只写**过滤后**的
      变体=保留的子体 ∪ 保留的单变体产品自身; 缺失时从 <CAT>-results.jsonl 推导)
      + 卖家精灵平台登录态(浏览器 Profile)
输出: 提交记录 <CAT>/12_查竞品/<CAT>-submitted.json

逻辑: 变体分批(默认1000=BATCH_VARIANT) -> 逐组处理
      -> 三重去重(下载目录组文件/本地提交记录/平台含首ASIN任务)
      -> 清单指纹校验(变化强制清空记录重提)
      -> 浏览器逐组提交: 选站点->填ASIN->立即查询->等待表格
         ->取消勾选"展示所有变体"->真实点击导出->弹窗处理
      -> 记录提交(组号/时间/导出任务名)

容错: 本地记录+平台任务名双重去重; 指纹变化强制重提; 配额=组数;
      提交失败重试该组; 无变体清单时写出空提交记录保证 DONE 校验
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta

from core import (
    BrowserError,
    StepFailedError,
    read_json,
    read_jsonl,
    read_txt_lines,
    warn_missing,
    write_json,
)
from steps._month_fallback import (  # noqa: E402
    MonthFallbackNeedsConfirm,
    MonthUnavailableError,
    confirm_or_auto,
    is_recent30,
    list_options,
    select_recent30,
)
from steps._ss_accounts import (
    QuotaExhausted,
    account_plan,
    notify_need_new_account,
    read_export_quota,
    wait_until_window,
)
from steps._browser_ux import click_by_text, click_verified, first_visible, park_mouse
from steps.base import BaseStep, StepContext


class Step12CompetitorSubmit(BaseStep):
    number = 12
    title = "查竞品提交"
    description = "FR-12: 变体分批提交查竞品导出任务并记录提交记录"

    archive_dirs = ["12_查竞品"]

    LOOKUP_URL = "https://www.sellersprite.com/v3/competitor-lookup"
    EXPORT_LOG_URL = "https://www.sellersprite.com/v2/export-log"

    # 每次点击/导航后等待页面渲染的秒数(2026-09-24 用户要求):
    # 连续提交时, goto 后页面/站点下拉/月份下拉可能尚未渲染完成就去点, 导致
    # "月份下拉找不到 → 误判 2026-08 不存在 → 触发最近30天回退" 的偶发失败。
    # 在每个关键点击后留出渲染时间, 避免控件还没出来就操作下一步。
    _RENDER_WAIT = 1.5

    @property
    def artifacts(self) -> list[str]:
        return [f"12_查竞品/{self._cat}-submitted.json"]

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        site = str(ctx.config.get("SITE", "美国") or "美国")
        batch_size = int(ctx.config.get("BATCH_VARIANT", 1000) or 1000)
        if batch_size <= 0:
            batch_size = 1000
        self.log(ctx, f"开始查竞品提交: CAT={cat} SITE={site} 每批={batch_size}")

        asins = self._load_variants(ctx, cat)
        # 过滤非标准商品 ASIN(2026-09-04 方案B): 纯数字 ISBN 型(如 0593334469=书名ASIN)
        # 非亚马逊商品, 卖家精灵无法查询(提交会导致整组查询失败)。标准商品 ASIN = 10位
        # 字母数字混合且以字母开头(B0/B00... 或字母开头旧ASIN); 纯数字/异常格式剔除。
        raw_n = len(asins)
        asins = [a for a in asins if self._is_product_asin(a)]
        if len(asins) != raw_n:
            self.log(ctx, f"过滤非标准 ASIN {raw_n - len(asins)} 个(纯数字ISBN型/格式异常, "
                          f"卖家精灵无法查询; 原 {raw_n} → {len(asins)})")
        fp = self._fingerprint(asins)
        self.log(ctx, f"变体清单: {len(asins)} 个, 指纹={fp}")

        submitted_path = ctx.resolve_artifact(self.artifacts[0])
        old = read_json(submitted_path, default=None)
        force = old is None or old.get("fingerprint") != fp
        groups: list[dict] = []
        if old is not None and old.get("fingerprint") == fp:
            groups = list(old.get("groups") or [])
            self.log(ctx, f"指纹一致, 沿用本地提交记录 {len(groups)} 组")
        elif old is not None:
            self.log(ctx, "清单指纹变化, 清空提交记录强制重新提交")
        else:
            self.log(ctx, "无历史提交记录, 全新提交")

        batches = self._batch(asins, batch_size)
        self.log(ctx, f"变体共 {len(batches)} 组, 本次配额=组数={len(batches)}")

        if not batches:
            self.log(ctx, "无变体清单, 写出空提交记录(仅保证 DONE 校验)")
            write_json(submitted_path, {"fingerprint": fp, "groups": []})
            return

        # 账号计划: SS_ACCOUNTS 统一账号列表(is_main 主账号随时可用; 备用仅窗口后可导出);
        # 某组提交时提示配额耗尽 → 自动换下一账号继续未完成组; 全部耗尽 → 提醒供号后阻断
        accounts = account_plan(ctx.config)
        if len(accounts) > 1:
            self.log(
                ctx,
                "账号计划: " + "; ".join(
                    f"{a['note']}({'随时可用' if a['kind'] == 'main' else '20:00后可导出'})"
                    for a in accounts
                ),
            )
        todo: list[tuple[int, list[str]]] = list(enumerate(batches))
        self.log(ctx, f"待提交组 {len(todo)}; 账号计划 {len(accounts)} 个")
        for acct in accounts:
            if not todo:
                break
            if acct["kind"] == "backup":
                wait_until_window(ctx, lambda m: self.log(ctx, m))
            todo = self._run_account_batches(
                ctx, acct, todo, site, groups, fp, submitted_path, force
            )
        write_json(submitted_path, {"fingerprint": fp, "groups": groups})
        if todo:
            notify_need_new_account(
                ctx,
                f"步骤12 查竞品提交剩余 {len(todo)} 组; 主账号+备用账号配额均已耗尽",
            )
            raise StepFailedError(
                "卖家精灵查竞品提交配额耗尽: 主账号+备用账号均无剩余次数"
                f"(剩余 {len(todo)} 组)。请提供新账号加入 config.yaml SS_ACCOUNTS 后重跑续跑。"
            )
        self.log(ctx, f"提交记录已写入: {submitted_path}")

    def _run_account_batches(
        self,
        ctx: StepContext,
        acct: dict,
        todo: list,
        site: str,
        groups: list[dict],
        fp: str,
        submitted_path: str,
        force: bool,
    ) -> list:
        """用指定账号提交一批组(含三重去重); 配额耗尽中断返回剩余组(换下一账号)。"""
        # 备用账号: 独立 Profile + 有头自动登录(use_cdp=False); 主账号走 CDP 直连默认值。
        # 2026-09-16: 必须 `with ... as` 绑定真浏览器 —— ctx.browser 返回的是租约
        # (SsBrowserLease), 直接赋值再用会报没有 new_page。
        backup = acct["kind"] != "main"
        with ctx.browser(
            "sellersprite",
            profile_dir=(ctx.paths.profile_ss_backup(acct["index"]) if backup else None),
            use_cdp=(False if backup else None),
        ) as b:
            page = None
            if not ctx.dry_run:
                page = b.new_page()
                self._ensure_login_for(ctx, b, page, acct)
            self.log(ctx, f"账号[{acct['note']}] 开始提交, 剩余待处理组 {len(todo)}")
            first_submitted = True
            for i, batch_asins in todo:
                first_asin = batch_asins[0]
                old_group = self._find_group(groups, i)
                old_task = (old_group or {}).get("task_name")
                if not force:
                    if self._group_downloaded(ctx, i, old_task):
                        self.log(ctx, f"组{i}: 下载目录已有组文件, 跳过提交")
                        continue
                    if old_group is not None:
                        self.log(ctx, f"组{i}: 本地提交记录已存在, 跳过提交")
                        continue
                    platform_task = self._platform_task_name(ctx, b, page, first_asin)
                    if platform_task:
                        self.log(ctx, f"组{i}: 平台已有任务[{platform_task}], 跳过并补记记录")
                        groups.append(self._make_record(i, platform_task, first_asin, batch_asins))
                        write_json(submitted_path, {"fingerprint": fp, "groups": groups})
                        continue
                try:
                    task_name = self._submit_group(
                        ctx, b, page, site, batch_asins, first_asin, first_submitted
                    )
                except QuotaExhausted as exc:
                    self.log(ctx, f"组{i}: [{acct['note']}] 导出配额耗尽({exc}) → 切换账号")
                    write_json(submitted_path, {"fingerprint": fp, "groups": groups})
                    return [(g, ba) for (g, ba) in todo if g >= i]
                first_submitted = False
                groups.append(self._make_record(i, task_name, first_asin, batch_asins))
                write_json(submitted_path, {"fingerprint": fp, "groups": groups})
                self.log(ctx, f"组{i}: 提交成功, 任务名={task_name}")
            return []

    def _ensure_login_for(self, ctx: StepContext, b, page, acct: dict) -> None:
        """按账号登录: 主账号走原流程; 备用账号独立 Profile + 账号密码自动登录。"""
        if acct["kind"] == "main":
            self._ensure_login_real(ctx, b, page)
            return
        from core.browser import BrowserAutomation
        if BrowserAutomation.login_verified(b.profile_dir):
            self.log(ctx, f"备用账号[{acct['note']}] Profile 已有登录态, 复用")
            return
        b.goto(page, "https://www.sellersprite.com/")
        if b.check_sellersprite_login(page):
            self.log(ctx, f"备用账号[{acct['note']}] 已登录")
            return
        self.log(ctx, f"备用账号[{acct['note']}] 未登录, 自动登录")
        if b.login_sellersprite(page, acct["account"], acct["password"]):
            self.log(ctx, f"备用账号[{acct['note']}] 自动登录成功")
            return
        if b.check_sellersprite_login(page):
            self.log(ctx, "自动登录后复查发现实际已登录, 继续")
            return
        raise BrowserError(f"备用账号[{acct['note']}] 登录失败(检查 SS_ACCOUNTS 账号密码)")

    def _load_variants(self, ctx: StepContext, cat: str) -> list[str]:
        """读取变体清单; 缺失时从 results.jsonl 推导(多变体取子体, 单变体取自身)。"""
        vpath = ctx.paths.cat_file(f"{cat}-variants.txt")
        if os.path.exists(vpath):
            return read_txt_lines(vpath)
        rpath = ctx.paths.cat_file(f"{cat}-results.jsonl")
        if os.path.exists(rpath):
            asins: list[str] = []
            seen: set[str] = set()
            for rec in read_jsonl(rpath):
                vas = rec.get("variant_asins") or []
                if vas:
                    for a in vas:
                        if a and a not in seen:
                            seen.add(a)
                            asins.append(a)
                else:
                    req = rec.get("request_asin")
                    if req and req not in seen:
                        seen.add(req)
                        asins.append(req)
            self.log(ctx, f"从 {cat}-results.jsonl 推导变体清单 {len(asins)} 个")
            return asins
        warn_missing(vpath, f"{cat}-variants.txt")
        warn_missing(rpath, f"{cat}-results.jsonl")
        return []

    def _fingerprint(self, asins: list[str]) -> str:
        content = f"数量:{len(asins)}\n" + "\n".join(asins)
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_product_asin(a: str) -> bool:
        """标准亚马逊商品 ASIN 判定(2026-09-04 方案B): 10 位字母数字且以字母开头。
        纯数字 10 位 = ISBN 型(书籍 ASIN, 如 0593334469), 卖家精灵无法查询 → 剔除。
        """
        s = str(a or "").strip()
        if len(s) != 10 or not s.isalnum():
            return False
        return s[0].isalpha()

    def _batch(self, asins: list[str], size: int) -> list[list[str]]:
        return [asins[i:i + size] for i in range(0, len(asins), size)]

    def _find_group(self, groups: list[dict], group_num: int) -> dict | None:
        for g in groups:
            if g.get("group") == group_num:
                return g
        return None

    def _make_record(self, group: int, task_name: str, first_asin: str, asins: list[str]) -> dict:
        return {
            "group": group,
            "time": self.now(),
            "task_name": task_name,
            "first_asin": first_asin,
            "asins": list(asins),
        }

    def _group_downloaded(self, ctx: StepContext, group_num: int, task_name: str | None) -> bool:
        """检查 ①: 下载目录(13_查竞品下载)是否已有该组文件。"""
        d13 = ctx.paths.step_dir(13)
        if os.path.exists(os.path.join(d13, f"组{group_num}.xlsx")):
            return True
        if task_name:
            prefix = self._safe_filename(task_name)
            for name in os.listdir(d13):
                if name.startswith(prefix) and name.endswith((".xlsx", ".zip")):
                    return True
        return False

    def _platform_task_name(self, ctx: StepContext, b, page, first_asin: str) -> str | None:
        """检查 ③: 平台"我的导出"是否已有含首 ASIN 的任务, 命中返回任务名。"""
        if ctx.dry_run:
            mock = b.mock_sellersprite(ctx)
            try:
                tasks = mock.export_log_ready()
            except Exception:
                return None
            for t in tasks or []:
                name = str(t.get("task") or t.get("task_name") or "")
                if name and (first_asin in name or first_asin[:8] in name):
                    return name
            return None
        return self._platform_task_name_real(b, page, first_asin)

    def _platform_task_name_real(self, b, page, first_asin: str) -> str | None:
        try:
            b.goto(page, self.EXPORT_LOG_URL, timeout=60000)
            b.wait_selector(page, "table", timeout=30000)
            trs = page.locator("table tbody tr, table tr").all()
            for tr in trs:
                cells = [c.inner_text().strip() for c in tr.locator("td").all()]
                for c in cells:
                    if first_asin in c:
                        return c
            for tr in trs:
                cells = [c.inner_text().strip() for c in tr.locator("td").all()]
                for c in cells:
                    if first_asin[:8] in c:
                        return c
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_export_time(value: str) -> datetime | None:
        """解析平台导出时间(分钟精度, 如 '2026-08-25 15:15'); 解析失败返回 None。"""
        s = str(value or "").strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y.%m.%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _wait_export_task_created(self, ctx: StepContext, b, page, month: str,
                                  submitted_at: datetime,
                                  wait_sec: int = 120, interval: int = 10) -> str | None:
        """点击导出后, 在"我的导出"确认本次提交的任务**已创建**, 返回真实任务名(未创建→None)。

        口径与步骤13 的候选过滤一致(列位 2026-09 实测: tds[1]=任务名/t[2]=来源/t[3]=时间/t[4]=状态):
        来源含"查竞品" + 任务名含目标月(兼容 2026-08 / 2026.08) + 时间 >= 本次提交-90s。
        **存在即算创建成功**(状态可为 创建/导出中/已完成; 是否完成交给步骤13 轮询),
        因为本步只需确认"提交生效", 不必等导出完成。
        """
        grace = timedelta(seconds=90)
        # ★ 2026-10-26 修: `COMPETITOR_MONTH` 允许填「最近30天」这类**周期选项**（不是月份）。
        #   实测 Dehumidifiers 填了「最近30天」→ `month_key="最近30天"` 被拿去匹**任务名**，
        #   而平台导出任务名里根本没有这个词 → 10 行导出记录全部被跳过 → 误判"提交未生效"，
        #   实际上任务已经创建（配额已扣）。这种情况**只按时间过滤**：
        #   本次提交的新任务，其导出台账时间一定 ≥ submitted_at - 90s。
        if is_recent30(month):
            month_key = ""
        else:
            month_key = str(month or "").replace("-", ".")
        deadline = time.monotonic() + max(10, int(wait_sec))
        while True:
            seen = 0
            try:
                b.goto(page, self.EXPORT_LOG_URL, timeout=60000)
                b.wait_selector(page, "table", timeout=30000)
                trs = page.locator("table tbody tr, table tr").all()
                matched: list[tuple[str, str, str]] = []
                for tr in trs:
                    cells = [c.inner_text().strip() for c in tr.locator("td").all()]
                    if len(cells) < 5:
                        continue
                    name = cells[1].replace("最新", "").strip()
                    source = cells[2] or ""
                    time_raw = (cells[3] or "").split("\n")[0].strip()
                    if not name or not source:
                        continue
                    seen += 1
                    if "查竞品" not in source:
                        continue
                    if month_key and month_key not in name.replace("-", "."):
                        continue
                    t = self._parse_export_time(time_raw)
                    if t is not None and t < submitted_at - grace:
                        continue
                    matched.append((name, source, time_raw))
                if matched:
                    name, source, time_raw = matched[0]
                    self.log(ctx, f"导出任务已创建: {name}(来源={source}, 时间={time_raw}; "
                                  f"我的导出共 {seen} 行)")
                    return name
                self.log(ctx, f"等待导出任务出现... 我的导出 {seen} 行, 暂未匹配到本次提交的查竞品任务")
            except Exception as exc:  # noqa: BLE001 - 页面异常不致命, 轮询重试
                self.log(ctx, f"查询'我的导出'异常(将重试): {exc}")
            if time.monotonic() >= deadline:
                return None
            time.sleep(max(3, int(interval)))

    def _submit_group(
        self,
        ctx: StepContext,
        b,
        page,
        site: str,
        batch_asins: list[str],
        first_asin: str,
        is_first_group: bool,
    ) -> str:
        if ctx.dry_run:
            mock = b.mock_sellersprite(ctx)
            result = mock.competitor_submit(batch_asins, site)
            return result.get("task_name") or self._fallback_task_name(site, first_asin)
        return self._submit_group_real(ctx, b, page, site, batch_asins, is_first_group)

    def _submit_group_real(
        self,
        ctx: StepContext,
        b,
        page,
        site: str,
        batch_asins: list[str],
        is_first_group: bool,
    ) -> str:
        first_asin = batch_asins[0]
        month = str(ctx.config.get("COMPETITOR_MONTH", "") or "")
        force = str(ctx.config.get("SS_FORCE_RESUBMIT", "") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        last_err = ""
        for attempt in range(3):
            try:
                if not force:
                    existing = self._platform_task_name_real(b, page, first_asin)
                    if existing:
                        self.log(ctx, f"重试时发现平台已有任务, 直接复用: {existing}")
                        return existing
                else:
                    self.log(ctx, "SS_FORCE_RESUBMIT=true, 跳过平台任务复用, 强制重新提交")
                b.goto(page, self.LOOKUP_URL, timeout=60000)
                time.sleep(self._RENDER_WAIT)                 # 页面导航后等渲染
                self._select_site(ctx, page, site)
                time.sleep(self._RENDER_WAIT)                 # 选站点后等下拉/页面刷新
                self._select_month(ctx, page, month)
                time.sleep(self._RENDER_WAIT)                 # 选月份后等下拉/页面刷新
                self._fill_asins(ctx, page, batch_asins)
                time.sleep(self._RENDER_WAIT)                 # 填 ASIN 后等输入框/联动控件渲染
                self._click_by_text(ctx, page, "立即查询", required=True)
                time.sleep(self._RENDER_WAIT)                 # 点查询后等结果表格/遮罩加载
                b.wait_selector(page, "table", timeout=60000)
                self._uncheck_show_all_variants(ctx, page)
                time.sleep(self._RENDER_WAIT)                 # 取消勾选后等列表刷新
                self._check_quota(ctx, page)  # 配额提示"当天剩余0次"→ 抛 QuotaExhausted 换号
                self._click_export(ctx, page)
                time.sleep(self._RENDER_WAIT)                 # 点导出后等弹窗渲染
                self._handle_export_dialog(ctx, page, first_group=is_first_group)
                # 2026-09-10: 点击导出后必须确认任务**真实创建**在"我的导出"里, 否则视为提交未生效;
                # 不再用兜底任务名当成功(会掩盖"导出没生效" → 下游 step13 拿到空数据)。
                # 校验口径与步骤13 完全一致(来源=查竞品 + 任务名含目标月 + 时间>=本次提交-90s),
                # **不按 ASIN 匹配**——平台任务名常不含 ASIN(实测如 Competitor-US-2026.08-293265),
                # 按 ASIN 匹配会把"已创建"误判成"未生成"(2026-09-10 实战踩坑)。
                submitted_at = datetime.now()
                real = self._wait_export_task_created(ctx, b, page, month, submitted_at)
                if not real:
                    raise StepFailedError(
                        f"导出任务未在'我的导出'生成(首ASIN={first_asin}, "
                        f"提交时间={submitted_at.strftime('%H:%M:%S')}), 提交未生效"
                    )
                return real
            except QuotaExhausted:
                raise  # 配额耗尽不重试(重试无意义), 直接冒泡由外层换号
            except Exception as exc:
                # 待弹窗确认(目标月缺失): 立即失败让弹窗出现, 不重试(2026-09-30)
                if "[MONTH_FALLBACK_CONFIRM]" in str(exc):
                    raise
                if "导出任务未在'我的导出'生成" in str(exc):
                    last_err = str(exc)
                    if attempt < 2:
                        self.log(ctx, f"组{first_asin} 第{attempt + 1}次导出未确认生成，"
                                      "重新检查平台任务后再次导出；可能重复消耗配额")
                        continue
                    break
                last_err = str(exc)
                self.log(ctx, f"组{first_asin} 第{attempt + 1}次提交失败: {last_err}")
        raise StepFailedError(f"查竞品提交最终失败({first_asin}): {last_err}")

    def _check_quota(self, ctx: StepContext, page) -> None:
        """点击导出前悬停读取配额 tooltip; '当天剩余0次' → 抛 QuotaExhausted 触发换号。

        检测不到 tooltip(页面结构变化)不拦截, 保持原行为(点击导出由后续结果判定)。
        """
        try:
            remaining, hint = read_export_quota(
                page,
                "button:has-text('导出'):not([disabled]), [class*=export-btn]:not([disabled]), [class*=export]:not([disabled])",
            )
            if remaining == 0:
                raise QuotaExhausted(hint or "当天剩余0次(快速导出2000条已用尽)")
            if remaining is not None:
                self.log(ctx, f"导出配额: 当前账号当天剩余 {remaining} 次")
        except QuotaExhausted:
            raise
        except Exception as exc:
            self.log(ctx, f"配额检测异常(忽略, 继续导出): {exc}")

    def _ensure_login_real(self, ctx: StepContext, b, page) -> None:
        from core.browser import BrowserAutomation
        if BrowserAutomation.login_verified(b.profile_dir):
            self.log(ctx, "卖家精灵已登录(本轮已确认)")
            return
        b.goto(page, "https://www.sellersprite.com/")
        if b.check_sellersprite_login(page):
            self.log(ctx, "卖家精灵已登录")
            return
        account = str(ctx.config.get("SS_ACCOUNT", "") or "")
        password = str(ctx.config.get("SS_PASSWORD", "") or "")
        if account and password and b.login_sellersprite(page, account, password):
            self.log(ctx, "卖家精灵自动登录成功")
            return
        raise BrowserError("卖家精灵未登录, 请检查 SS_ACCOUNT/SS_PASSWORD 或手动登录 Profile")

    def _select_site(self, ctx: StepContext, page, site: str) -> None:
        """选站点(需求文档技术路线: 下拉选择 mousedown+click 展开 -> 文本模糊匹配)。

        查竞品页站点为 el-select.market-select(2026-09 实测): 点击展开下拉,
        点选可见的精确文本选项。
        """
        if not site:
            return
        try:
            # 2026-09-16: 只点可见候选 + 点前退遮挡(原 .first.click 会盲取第一个匹配,
            # 可能点到视野外元素 → 页面被滚动 + 鼠标悬停顶栏 → 顶栏菜单盖住页面)
            if not click_verified(page, "[class*=market-select] input", what="站点下拉",
                                  timeout=8000, log=lambda m: self.log(ctx, m)):
                raise BrowserError("未找到可见的站点下拉")
            # 页面有多个下拉(排序/筛选等), 必须等**目标文本对应的可见选项**
            option = page.locator(".el-select-dropdown__item:visible").filter(
                has_text=site
            ).first
            option.wait_for(state="visible", timeout=5000)
            option.click(timeout=5000)
            park_mouse(page)
            self.log(ctx, f"已选择站点: {site}")
        except Exception as exc:
            self.log(ctx, f"站点选择失败(将使用页面默认): {site}, {exc}")

    def _select_month(self, ctx: StepContext, page, month: str) -> None:
        """选月份(需求文档技术路线: 下拉选择; 查竞品页日期为 el-select.date-select)。

        选项如 最近30天/2026-07/2026-06..., 按文本精确匹配 COMPETITOR_MONTH,
        点击后校验输入框值(防静默用错日期)。
        2026-09: 配置值 = recent30 时直接选「最近30天」; 目标月份在下拉中不存在时
        询问用户是否改用「最近30天」替代并写回 config(webui 子进程自动替代)。
        """
        if not month:
            self.log(ctx, "告警: COMPETITOR_MONTH 未配置, 使用页面默认日期")
            return
        if is_recent30(month):
            try:
                select_recent30(page, "[class*=date-select] input",
                                log=lambda m: self.log(ctx, m))
            except Exception as exc:
                raise BrowserError(f"查竞品日期(最近30天)选择失败: {exc}") from exc
            return
        try:
            if not click_verified(page, "[class*=date-select] input", what="月份下拉",
                                  timeout=8000, log=lambda m: self.log(ctx, m)):
                raise BrowserError("未找到可见的日期输入框")
            option = page.locator(".el-select-dropdown__item:visible").filter(
                has_text=month
            ).first
            option.wait_for(state="visible", timeout=5000)
            option.click(timeout=5000)
            _inp = first_visible(page, "[class*=date-select] input") or \
                page.locator("[class*=date-select] input").first
            val = _inp.input_value()
            if month not in val:
                raise BrowserError(f"日期选择后输入框值异常: {val!r}")
            park_mouse(page)
            self.log(ctx, f"已选择月份: {month}")
        except MonthUnavailableError as exc:
            raise BrowserError(str(exc)) from exc
        except Exception as exc:
            # 目标月可能尚未被平台提供: 列出可用选项, 再询问是否用最近30天替代
            opts: list[str] = []
            try:
                click_verified(page, "[class*=date-select] input", what="月份下拉(列选项)",
                               timeout=8000, log=lambda m: self.log(ctx, m))
                opts = list_options(page, "[class*=date-select] input")
            except Exception:  # noqa: BLE001
                pass
            if opts:
                self.log(ctx, "卖家精灵查竞品日期下拉可用选项: " + " / ".join(opts[:40]))
            try:
                confirm_or_auto(ctx, "COMPETITOR_MONTH", month, opts,
                                log=lambda m: self.log(ctx, m))
            except MonthFallbackNeedsConfirm as need:
                raise BrowserError(
                    f"[MONTH_FALLBACK_CONFIRM] COMPETITOR_MONTH 目标月 {need.month} 在下拉中不存在; "
                    f"请在网页弹窗确认是否改写为「最近30天」(不同意则保持原值, 自行处理配置)"
                ) from need
            except MonthUnavailableError:
                raise BrowserError(
                    f"查竞品日期选择失败: {month}(下拉中不存在, 且未采用最近30天替代; "
                    f"请修改配置 COMPETITOR_MONTH 后重跑), {exc}"
                ) from exc
            # CLI 用户确认: 配置 COMPETITOR_MONTH 已写为「最近30天」(改动已记日志, 不备份) →
            # **停止本步骤**, 由用户重新运行(再次运行将直接选最近30天)
            raise BrowserError(
                f"目标月份 {month} 在下拉中不存在: 配置 COMPETITOR_MONTH 已改为「最近30天」(见上方日志), "
                f"本次运行停止——请重新运行本步骤(步骤12)"
            ) from exc

    def _fill_asins(self, ctx: StepContext, page, batch_asins: list[str]) -> None:
        """填入变体ASIN(**空格分隔**, 单行输入框, 2026-09-04 实测修正)。

        查竞品页 ASIN 输入框为**单行 input**(非 textarea), 多个 ASIN 必须用
        **空格**隔开——换行/逗号分隔在该输入框只识别第一个 ASIN(曾用 "\n".join,
        结果只填入首个 0593334469, 其余连成一个串 → 平台识别成一个 ASIN → 报错)。
        输入框默认为 disabled(2026-09 实测), 需 JS 解除后填入。
        2026-09-04 修复(完整写入, 不依赖重填): Playwright `fill()` 对
        Element-UI 受控输入框是高层模拟, 偶发**吞首字符**(首个 ASIN 丢 "B0"),
        且不抛异常静默成功。改为 **原生 value setter + input 事件**一次性写入
        (Vue 受控组件能识别、内容一次完整), 写后**读回校验**——按空白拆出的
        ASIN 数必须等于提交数且首 ASIN 完整, 否则即报错(不重填不静默)。
        """
        first = str(batch_asins[0]).strip()
        text = " ".join(batch_asins)   # 空格分隔(单行输入框要求)
        sel = "input[placeholder*='ASIN'], textarea[placeholder*='ASIN']"
        # 2026-09-16: 只取**可见且不在顶栏内**的候选(原 .first 可能盲取到隐藏/顶栏里的同名元素)
        inp = first_visible(page, sel) or page.locator(sel).first
        try:
            if not inp.is_editable():
                page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (el) el.removeAttribute('disabled');
                    }""",
                    sel,
                )
        except Exception:
            pass
        # 原生写入(在已定位元素上直接执行): 受控组件(Vue)需触发 input/change 更新 model,
        # 一次写入完整不吞首字符; 不做 fill→fill2→keyboard 重试链(根因在写入方式)
        inp.evaluate(
            """(el, text) => {
                const tag = el.tagName;
                if (tag === "TEXTAREA") {
                    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(el, text);
                } else {
                    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, text);
                }
                el.dispatchEvent(new Event("input", {bubbles: true}));
                el.dispatchEvent(new Event("change", {bubbles: true}));
                return "ok";
            }""",
            text,
        )
        # 读回校验: 按空白拆出 ASIN 数必须等于提交数 且 首 ASIN 完整存在
        # (防吞前缀/换行未被识别/截断等静默错误), 不完整立即报错不重填
        cur = ""
        try:
            cur = inp.input_value()
        except Exception:
            pass
        got = [p for p in cur.replace("\n", " ").split(" ") if p.strip()]
        self.log(ctx, f"填入校验: 提交 {len(batch_asins)} 个, 输入框识别 {len(got)} 个, 首={got[0] if got else '(空)'!r}")
        if got and first in got and len(got) == len(batch_asins):
            self.log(ctx, f"已填入变体ASIN {len(batch_asins)} 个(空格分隔, 原生写入+读回校验通过)")
            park_mouse(page)     # 填完把鼠标挪回内容区(避免顶栏 hover 菜单盖住按钮)
            return
        raise StepFailedError(
            f"填 ASIN 不完整: 提交 {len(batch_asins)} 个, 输入框仅识别 {len(got)} 个"
            f"(首 ASIN {first!r} 完整={first in got}); 实际值前120字符: {cur[:120]!r}, "
            f"疑似输入框类型/分隔符不符, 请人工检查后重跑"
        )

    def _click_by_text(self, ctx: StepContext, page, text: str, required: bool = False) -> bool:
        """按文本点击(2026-09-16: 走 `_browser_ux.click_by_text` —— 可见候选 + 命中判定 + 短超时)。"""
        if click_by_text(page, text, what=f"文本「{text}」", timeout=8000,
                         log=lambda m: self.log(ctx, m)):
            return True
        if required:
            raise StepFailedError(f"未找到可点击元素: {text}")
        return False

    def _uncheck_show_all_variants(self, ctx: StepContext, page) -> None:
        """取消勾选'展示所有变体'(FR-12: 只导出单变体数据)。

        2026-09 实测: 该控件为 Element UI 勾选框, 原生 input 隐藏,
        文本在 span.el-checkbox__label 内, 状态由 label.el-checkbox 的
        is-checked 类标识——点击 label 切换(不能直接操作隐藏 input)。
        2026-09-16: 点击改走命中判定版(被弹层/顶栏菜单遮住时先退遮挡再点)。
        """
        try:
            label = page.locator("label.el-checkbox", has_text="展示所有变体").first
            if label.count() == 0 or not label.is_visible():
                self.log(ctx, "未找到'展示所有变体'勾选框, 跳过")
                return
            cls = label.get_attribute("class") or ""
            if "is-checked" not in cls:
                self.log(ctx, "'展示所有变体'未勾选, 无需取消")
                return
            click_verified(page, "label.el-checkbox:has-text('展示所有变体')",
                           what="'展示所有变体'勾选框", timeout=5000,
                           log=lambda m: self.log(ctx, m))
            # 校验点击后已取消(is-checked 类消失), 未生效则再点一次
            cls2 = label.get_attribute("class") or ""
            if "is-checked" in cls2:
                click_by_text(page, "展示所有变体", what="'展示所有变体'再点一次",
                              timeout=5000, log=lambda m: self.log(ctx, m))
            self.log(ctx, "已取消勾选'展示所有变体'")
        except Exception as exc:
            self.log(ctx, f"取消勾选'展示所有变体'失败: {exc}")

    def _click_export(self, ctx: StepContext, page) -> None:
        excluded = ("我的导出", "导出明细", "批量下载", "批量删除")
        try:
            nodes = page.locator(
                "//*[self::button or self::a or self::span][contains(., '导出')]"
            ).all()
            for node in nodes:
                text = (node.inner_text() or "").strip()
                if not text:
                    continue
                if any(w in text for w in excluded):
                    continue
                # 2026-09-16: 先退遮挡(被顶栏菜单/弹层盖住时原来会白等 10 秒), 再命中判定点击
                park_mouse(page)
                node.click(timeout=6000)
                self.log(ctx, f"已点击导出: {text}")
                park_mouse(page)
                return
        except Exception as exc:
            raise StepFailedError(f"点击导出失败: {exc}") from exc
        raise StepFailedError("未找到符合条件的导出按钮")

    def _handle_export_dialog(self, ctx: StepContext, page, first_group: bool) -> None:
        primary = "前往查看" if first_group else "等会儿看"
        for text in (primary, "等会儿看", "稍后查看", "关闭", "知道了", "确定"):
            try:
                btn = page.get_by_text(text, exact=True).first
                if btn.count() > 0 and click_by_text(page, text, what=f"弹窗按钮「{text}」",
                                                     timeout=4000, log=lambda m: self.log(ctx, m)):
                    self.log(ctx, f"导出弹窗已处理: {text}")
                    return
            except Exception:
                continue
        if click_verified(page, ".el-dialog__close", what="弹窗关闭", timeout=3000,
                          log=lambda m: self.log(ctx, m)):
            self.log(ctx, "导出弹窗已关闭")
        else:
            self.log(ctx, "未检测到导出弹窗(导出可能已直接完成)")

    def _extract_task_name(self, ctx: StepContext, page, site: str, first_asin: str) -> str:
        try:
            content = page.content()
            m = re.search(r"(?:导出任务|任务名|任务名称)[:：]?\s*([A-Za-z0-9_\-\.]{3,})", content)
            if m:
                name = m.group(1).strip()
                if name:
                    return name
        except Exception:
            pass
        fallback = self._fallback_task_name(site, first_asin)
        self.log(ctx, f"未能从页面提取任务名, 使用兜底: {fallback}")
        return fallback

    def _fallback_task_name(self, site: str, first_asin: str) -> str:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{site}-competitor-{first_asin}-{ts}"

    def _safe_filename(self, name: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(name))
        return cleaned.strip() or "export"
