"""FR-11 查流量来源(第三方数据平台-卖家精灵)。

输入: 多变体父体清单(<CAT>-parents.txt, 父ASIN, 仅变体数>1 的父体;
      2026-09-09 起步骤10 只写**过滤后仍有效**的父体=至少一个子变体被保留;
      缺失时从 <CAT>-results.jsonl 推导) + 平台登录态(持久 Profile)
输出: 11_查流量来源/ 下分组的流量来源数据文件 + 自动合并文件
      (11_查流量来源/合并-11_查流量来源.xlsx)

逻辑: 父体分批(默认20/批) -> 每组: 选站点/日期(TRAFFIC_MONTH) -> 填父ASIN(逗号分隔)
      -> 发起查询 -> 等待结果(表格行或导出按钮; 错误弹窗中止该组)
      -> 真实点击导出 -> 下载监听(120s*3次兜底) -> 保留原始文件名
      -> 全部组完成后按行合并(列名检查, 不一致告警仍对齐合并)
容错: 同名文件跳过(断点续跑); 下载超时重试; 每5组截图; 无数据文件写出空合并文件
"""
from __future__ import annotations

import os
import hashlib
import json
import time
import zipfile
from pathlib import Path

import pandas as pd

from core.errors import BrowserError, DownloadError, StepFailedError
from core.io_utils import df_to_excel, merge_excel_by_rows, read_jsonl, read_txt_lines
from core.validation import require_file
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
    read_export_quota,
    window_end,
    window_start,
)
from steps._browser_ux import click_verified, first_visible, park_mouse
from steps.base import BaseStep, StepContext

_REVERSING_URL = "https://www.sellersprite.com/v3/reversing"
_MERGED_NAME = "合并-11_查流量来源.xlsx"


class SellerSpriteSessionError(BrowserError):
    """查询页面仍显示游客或登录状态不一致，不能当成普通查询超时。"""


def _visible_guest_marker(page) -> bool:
    """只检查可见的明确访客标识；读取失败表示无法判断，不等于未登录。"""
    for selector in ("text=未登录", "text=游客"):
        try:
            if page.locator(selector).first.is_visible():
                return True
        except Exception:  # 页面尚未加载，交给正常等待/重试
            continue
    return False


def _valid_xlsx(path: str) -> bool:
    """仅把非空、压缩包完整且可解析的 XLSX 当作已下载。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        with zipfile.ZipFile(path) as workbook:
            if "xl/workbook.xml" not in workbook.namelist() or workbook.testzip() is not None:
                return False
        # 使用文件对象：Playwright 的临时下载名可能没有 .xlsx 扩展名。
        from openpyxl import load_workbook
        with open(path, "rb") as fh:
            wb = load_workbook(fh, read_only=True, data_only=True)
            try:
                return bool(wb.sheetnames)
            finally:
                wb.close()
    except Exception:  # 破损 ZIP/XML、非工作簿、下载尚未写完等均不能用于续跑
        return False


def _download_snapshot(directory: str) -> dict[str, tuple[int, int]]:
    """记录点击之前的文件状态，以便识别快速下载和同名覆盖。"""
    result = {}
    try:
        for entry in os.scandir(directory):
            try:
                if entry.is_file():
                    stat = entry.stat()
                    result[entry.path] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                continue
    except OSError:
        pass
    return result


def _wait_new_workbook(directory: str, before: dict[str, tuple[int, int]], timeout: int) -> str | None:
    """等待点击后新增或改写的完整工作簿；文件状态连续两次不变才返回。"""
    deadline = time.monotonic() + timeout
    last: dict[str, tuple[int, int]] = {}
    while time.monotonic() < deadline:
        current = _download_snapshot(directory)
        changed = {p: state for p, state in current.items()
                   if before.get(p) != state and not p.lower().endswith(
                       (".crdownload", ".part", ".tmp", ".no-content"))}
        stable = [p for p, state in changed.items() if last.get(p) == state and _valid_xlsx(p)]
        if stable:
            return max(stable, key=lambda p: current[p][1])
        last = changed
        time.sleep(1)
    return None

# 每次点击/导航后等待页面渲染的秒数(2026-09-24 用户要求):
# 连续查询时, goto 后页面/站点下拉/月份下拉可能尚未渲染完成就去点, 导致
# 下拉找不到/控件不可见等偶发失败。在每个关键点击后留出渲染时间。
_RENDER_WAIT = 5.0

# ASIN 输入框候选选择器(按优先级; 页面结构变化时按需增补)
_ASIN_INPUT_SELECTORS = (
    "[class*=asin] textarea",
    "[class*=asin] input",
    "[class*=textarea] textarea",
    ".el-textarea textarea",
    "textarea",
    "[class*=input] input",
    "input[type=text]",
    "input:not([type=hidden])",
    "[contenteditable=true]",
)


def _is_editable_visible(el) -> bool:
    """元素可见且可编辑(自动排除 readonly 的日期输入框等)。"""
    try:
        return el.is_visible() and el.is_editable()
    except Exception:
        return False


def _find_asin_input(page):
    """定位 ASIN 输入框: 候选选择器优先, 兜底取页面最大可见可编辑文本输入框。"""
    for sel in _ASIN_INPUT_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if _is_editable_visible(el):
                    return el
        except Exception:
            continue
    best, best_area = None, -1
    for sel in ("textarea", "input", "[contenteditable=true]"):
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if not _is_editable_visible(el):
                    continue
                box = el.bounding_box()
                if box and box["width"] * box["height"] > best_area:
                    best, best_area = el, box["width"] * box["height"]
        except Exception:
            continue
    return best


def _dump_inputs(page) -> str:
    """调试: 列出页面可见输入类元素(tag/class/placeholder), 用于选择器排查。"""
    info = []
    for sel in ("textarea", "input", "[contenteditable=true]"):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                try:
                    tag = el.evaluate("e => e.tagName.toLowerCase()")
                except Exception:
                    tag = "?"
                cls = ""
                ph = ""
                try:
                    cls = el.get_attribute("class") or ""
                except Exception:
                    pass
                try:
                    ph = el.get_attribute("placeholder") or ""
                except Exception:
                    pass
                info.append(f"{tag}[class={cls!r} placeholder={ph!r}]")
        except Exception:
            continue
    return "; ".join(info) if info else "无可见输入元素"


def _query_wait_timeout(cfg, download_timeout: int) -> int:
    """查询结果等待超时(秒): `QUERY_WAIT_TIMEOUT` 优先, 未配置时沿用 `DOWNLOAD_TIMEOUT`。

    2026-09-11 拆分: 原先"等查询结果"和"等下载文件"共用 `DOWNLOAD_TIMEOUT`(默认120s),
    表现为具体历史月份查询(实战 2026-08, 20 个父体)×3 次全部 120s 超时;
    但直接调大 `DOWNLOAD_TIMEOUT` 会连带把"下载失败重试"的等待也拉长
    (每组最多 3 次 → 最坏 3×N 秒) → 拆成独立键, 只放宽查询等待。
    """
    try:
        value = int(cfg.get("QUERY_WAIT_TIMEOUT", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:  # 未配置/非法/负数 → 沿用下载等待
        return max(10, int(download_timeout))
    return max(10, value)


class Step11TrafficSource(BaseStep):
    number = 11
    title = "查流量来源"
    description = "FR-11: 浏览器自动化反查流量来源(卖家精灵)并自动合并"
    artifacts = ["11_查流量来源/合并-11_查流量来源.xlsx"]
    archive_dirs = ["11_查流量来源"]

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")

    def run(self, ctx: StepContext) -> None:
        cat = self._cat
        self.log(ctx, f"开始 FR-11 查流量来源: CAT={cat}")
        asins = self._load_parents(ctx)
        site = str(ctx.config.get("SITE", "美国") or "美国")
        month = str(ctx.config.get("TRAFFIC_MONTH", "") or "")
        batch = max(1, int(ctx.config.get("BATCH_PARENT", 20)))
        timeout = max(10, int(ctx.config.get("DOWNLOAD_TIMEOUT", 120)))
        # 查询等待与下载等待分开(2026-09-11): 具体历史月份查询平台明显更慢,
        # 但放宽下载等待会让失败重试成倍变慢 → 查询等待用独立键
        self._query_timeout = _query_wait_timeout(ctx.config, timeout)
        screenshot_every = max(1, int(ctx.config.get("SCREENSHOT_EVERY", 5)))
        download_dir = ctx.paths.step_dir(11)
        merged_path = ctx.resolve_artifact(self.artifacts[0])
        groups = [asins[i : i + batch] for i in range(0, len(asins), batch)]
        self.log(
            ctx,
            f"父体总数={len(asins)} 分批={batch} 组数={len(groups)} 配额=组数(每日限定额度)",
        )
        self.log(
            ctx,
            f"等待参数: 查询结果最长 {self._query_timeout}s / 下载最长 {timeout}s"
            f" (QUERY_WAIT_TIMEOUT={ctx.config.get('QUERY_WAIT_TIMEOUT', '未配置')})",
        )
        if not groups:
            self.log(ctx, "无多变体父体, 跳过导出, 直接写出合并文件")
            self._merge(ctx, download_dir, merged_path)
            return
        if ctx.dry_run:
            self._run_dry(ctx, groups, site, month)
        else:
            self._run_real(
                ctx,
                groups,
                site,
                month,
                timeout,
                screenshot_every,
                download_dir,
            )
        self._merge(ctx, download_dir, merged_path)
        self.log(ctx, f"FR-11 完成: 合并文件 {merged_path}")

    def _load_parents(self, ctx: StepContext) -> list[str]:
        """读取多变体父体清单; parents.txt 缺失时从 results.jsonl 推导。"""
        parents_path = ctx.resolve_artifact(f"{self._cat}-parents.txt")
        if os.path.exists(parents_path):
            return read_txt_lines(parents_path)
        results_path = ctx.resolve_artifact(f"{self._cat}-results.jsonl")
        require_file(results_path, f"{self._cat}-results.jsonl(parents.txt 缺失回退推导)")
        agg = {}
        for rec in read_jsonl(results_path):
            if not rec.get("ok"):
                continue
            p = rec.get("parent_asin") or rec.get("request_asin")
            if p:
                agg.setdefault(p, set()).update(rec.get("variant_asins") or [])
        parents = sorted(p for p, vs in agg.items() if len(vs) > 1)
        self.log(ctx, f"parents.txt 缺失, 从 results.jsonl 推导多变体父体 {len(parents)} 个")
        return parents

    def _run_dry(self, ctx: StepContext, groups: list[list[str]], site: str, month: str) -> None:
        """dry-run: 用 mock.reverse_traffic_export 生成模拟导出 xlsx。"""
        with ctx.browser("sellersprite") as browser:
            mock = browser.mock_sellersprite(ctx)
            for gi, group in enumerate(groups, 1):
                self.log(ctx, f"dry-run 第{gi}/{len(groups)}组: 父体{len(group)}个 生成模拟导出")
                path = mock.reverse_traffic_export(group, site, month)
                self.log(ctx, f"第{gi}组模拟导出文件: {path}")

    def _run_real(
        self,
        ctx: StepContext,
        groups: list[list[str]],
        site: str,
        month: str,
        timeout: int,
        screenshot_every: int,
        download_dir: str,
    ) -> None:
        """使用普通 Chrome 的当前登录会话导出；配额耗尽后等待手动换号续跑。"""
        self._unconfirmed_download_failures = []
        if not str(ctx.config.get("SS_CDP_URL", "") or "").strip():
            raise StepFailedError("步骤11需要配置 SS_CDP_URL，才能连接普通 Chrome 的当前登录会话")
        self.log(ctx, "步骤11使用普通 Chrome 当前登录会话；账号切换需在该浏览器中手动完成")
        preexisting = set(os.listdir(download_dir))
        todo: list[tuple[int, list[str]]] = [
            (gi, group)
            for gi, group in enumerate(groups, 1)
            if not self._already_downloaded(group, preexisting, download_dir, site=site, month=month)
        ]
        if not todo:
            self.log(ctx, "所有组已有导出文件, 无需执行")
            return
        self.log(ctx, f"待导出组 {len(todo)}/{len(groups)}")
        # 共用一个登录态时不能把仍登录的账号误认成下一账号。
        current_session = {"kind": "current", "note": "Chrome 当前登录账号"}
        todo = self._run_groups_on_account(
            ctx, current_session, todo, site, month, timeout, screenshot_every,
            download_dir, has_next=False,
        )
        if todo:
            raise StepFailedError(
                f"普通 Chrome 当前登录账号的导出配额已耗尽，仍有 {len(todo)} 组未完成。"
                "请在该 Chrome 中切换到有配额的账号后续跑步骤11；已完成的导出会保留。"
            )

    @staticmethod
    def _account_window_label(ctx: StepContext, acct: dict) -> str:
        if acct.get("kind") == "main":
            return "随时可用"
        start, end = window_start(ctx.config), window_end(ctx.config)
        return "全天可用" if start == end else f"{start}–{end}可导出"

    def _run_groups_on_account(
        self,
        ctx: StepContext,
        acct: dict,
        todo: list,
        site: str,
        month: str,
        timeout: int,
        screenshot_every: int,
        download_dir: str,
        has_next: bool = False,
    ) -> list:
        """用指定账号跑一组待处理组; 配额耗尽中断, 返回仍未完成的组(换下一账号)。"""
        # 主账号和备用账号共用普通 Chrome 当前登录会话。
        # 2026-09-16: 必须 `with ... as` 绑定真浏览器 —— ctx.browser 返回的是租约
        # (SsBrowserLease), 直接赋值再用会报没有 new_page。
        with ctx.browser(
            "sellersprite",
            downloads_path=download_dir,
        ) as browser:
            page = browser.new_page()
            browser.set_download_dir(page, download_dir)  # CDP: 下载重定向到规定目录
            self._ensure_login_for(ctx, browser, page, acct)
            self.log(ctx, f"账号[{acct['note']}] 开始导出, 剩余待处理组 {len(todo)}")
            for gi, group in todo:
                if self._already_downloaded(group, set(os.listdir(download_dir)), download_dir,
                                            site=site, month=month):
                    self.log(ctx, f"第{gi}组: 检测到同名导出文件, 跳过(断点续跑)")
                    continue
                last_err = ""
                for attempt in range(1, 4):
                    try:
                        self._run_group(
                            ctx, browser, page, gi, group, site, month, timeout,
                            download_dir, acct=acct,
                        )
                        last_err = ""
                        break
                    except QuotaExhausted as exc:
                        self.log(ctx, f"第{gi}组: [{acct['note']}] 导出配额耗尽({exc}) → 切换账号")
                        return [(g, grp) for (g, grp) in todo if g >= gi]
                    except SellerSpriteSessionError:
                        # 登录问题不可按查询超时、下载失败或配额耗尽处理。
                        raise
                    except DownloadError as exc:
                        # 2026-09-08: 导出下载失败(tooltip 可能读不到配额)按"疑似耗尽"
                        # 处理——只要后面还有账号(主/备1/备2…), 就换下一账号继续;
                        # 仅最后一个账号失败才保留原 3 次重试后中止
                        if has_next:
                            if not hasattr(self, "_unconfirmed_download_failures"):
                                self._unconfirmed_download_failures = []
                            self._unconfirmed_download_failures.append(acct.get("note", "账号"))
                            self.log(
                                ctx,
                                f"第{gi}组: [{acct['note']}] 导出下载失败({exc}) "
                                f"→ 尝试下一账号；未确认配额耗尽",
                            )
                            return [(g, grp) for (g, grp) in todo if g >= gi]
                        last_err = str(exc)
                        self.log(ctx, f"第{gi}组第{attempt}次失败(下载: {exc}), 稍后重试")
                        time.sleep(3)
                    except Exception as exc:
                        # 待弹窗确认(目标月缺失): 立即失败让弹窗出现, 不重试(2026-09-30)
                        if "[MONTH_FALLBACK_CONFIRM]" in str(exc):
                            raise
                        last_err = str(exc)
                        self.log(ctx, f"第{gi}组第{attempt}次失败({exc}), 稍后重试")
                        time.sleep(3)
                if last_err:
                    raise BrowserError(f"第{gi}组连续3次失败: {last_err}")
                if gi % screenshot_every == 0:
                    shot = os.path.join(ctx.paths.screenshot_dir(), f"step11-group-{gi}.png")
                    browser.screenshot(page, shot)
                    self.log(ctx, f"第{gi}组截图存档: {shot}")
            return []

    def _mark_main_exhausted(self, ctx: StepContext) -> None:
        """标记主账号当日配额耗尽(2026-09-08): 当天重跑步骤11 备用账号优先,
        不在 0 配额的主账号上空转; 标记按日期命名, 次日自动失效。"""
        try:
            p = os.path.join(
                ctx.paths.cat_root, f".ss-main-quota-{time.strftime('%Y%m%d')}.flag"
            )
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("confirmed_zero_quota\n")
        except Exception:
            pass

    def _ensure_login_for(self, ctx: StepContext, browser, page, acct: dict) -> None:
        """使用普通 Chrome 当前会话，不能将现有登录态臆测为其他账号。"""
        self._ensure_login(ctx, browser, page)

    def _ensure_tool_session(self, ctx: StepContext, browser, page, acct: dict) -> None:
        """查询页明确显示游客时，清除进程缓存并重新检查实际登录态。"""
        if not _visible_guest_marker(page):
            return
        time.sleep(_RENDER_WAIT)
        if not _visible_guest_marker(page):
            return  # 页面刚渲染时的短暂占位
        from core.browser import BrowserAutomation
        profile = getattr(browser, "profile_dir", None)
        if profile is not None:
            BrowserAutomation.reset_login_verified(profile)
        self.log(ctx, f"账号[{acct.get('note', '?')}] 查询页显示未登录，重新核验该浏览器会话")
        try:
            self._ensure_login_for(ctx, browser, page, acct)
            browser.goto(page, _REVERSING_URL)
            time.sleep(_RENDER_WAIT)
        except Exception as exc:
            raise SellerSpriteSessionError(
                f"卖家精灵账号[{acct.get('note', '?')}] 登录态失效或无法验证，请检查该浏览器的登录状态"
            ) from exc
        if _visible_guest_marker(page):
            raise SellerSpriteSessionError(
                f"卖家精灵账号[{acct.get('note', '?')}] 查询页仍显示未登录；"
                "请检查该浏览器的登录状态后续跑"
            )

    def _run_group(
        self,
        ctx: StepContext,
        browser,
        page,
        gi: int,
        group: list[str],
        site: str,
        month: str,
        timeout: int,
        download_dir: str,
        acct: dict | None = None,
    ) -> None:
        """单组完整流程: 导航->站点->日期->填ASIN->查询->导出下载。"""
        browser.goto(page, _REVERSING_URL)
        time.sleep(_RENDER_WAIT)                 # 页面导航后等渲染
        if acct is not None:
            self._ensure_tool_session(ctx, browser, page, acct)
        self._shot(ctx, browser, page, gi, "toolpage")
        self._select_site(ctx, browser, page, site)
        self._shot(ctx, browser, page, gi, "site")
        self._select_month(ctx, browser, page, month)
        self._shot(ctx, browser, page, gi, "date")
        self._fill_asins(ctx, browser, page, group)
        time.sleep(_RENDER_WAIT)                 # 填 ASIN 后等输入框/联动控件渲染
        self._shot(ctx, browser, page, gi, "filled")
        self._click_query(ctx, browser, page)
        self._shot(ctx, browser, page, gi, "queried")
        result = self._wait_results(
            ctx, browser, page, getattr(self, "_query_timeout", timeout)
        )
        self._shot(ctx, browser, page, gi, "done")
        if result == "error":
            raise BrowserError(f"第{gi}组查询出现平台错误提示，未取得可确认结果")
        if result == "empty":
            # 空结果(2026-09-08): 平台对该组父体无流量来源数据 → 提醒并跳过,
            # 不导出不重试; 写 .no-content 标记, 断点续跑时按"已处理"跳过
            self.log(
                ctx,
                f"第{gi}组查询无内容(平台未返回流量来源数据, 父体 {len(group)} 个, "
                f"首 ASIN={group[0] if group else '?'})——跳过该组, 继续后续组",
            )
            self._mark_empty_skip(download_dir, gi, group, site=site, month=month)
            return
        path = self._export_with_retry(ctx, browser, page, download_dir, timeout)
        self._shot(ctx, browser, page, gi, "export")
        if not path:
            raise DownloadError(f"第{gi}组导出下载失败(超时兜底3次仍无文件)")
        # 2026-09-08: 非 CDP(备用账号)下载路径下平台未提供文件名, 浏览器存成 GUID
        # 无扩展名 → 合并/断点跳过都认不到; 先查是否已有 .xlsx 后缀, 没有才补上
        # (保留原名+补 .xlsx, 用户要求; 不做强制改名)
        path = self._normalize_export_name(path)
        self.log(ctx, f"第{gi}组导出文件: {path}")

    def _normalize_export_name(self, path: str) -> str:
        """下载文件名后缀检查: 已有 .xlsx 原样返回; 没有则在末尾补 .xlsx。"""
        try:
            if os.path.basename(path).lower().endswith(".xlsx"):
                return path
            want = path + ".xlsx"
            if os.path.exists(want):
                return path
            os.replace(path, want)
            return want
        except Exception:
            return path

    def _shot(self, ctx: StepContext, browser, page, gi: int, tag: str) -> None:
        """每步截图诊断(技术方案 FR-11): toolpage/site/date/filled/queried/done/export。

        前 3 组完整留痕(验证流程), 失败可回看定位; 组级截图另按 SCREENSHOT_EVERY。
        """
        if gi > 3:
            return
        try:
            shot = os.path.join(ctx.paths.screenshot_dir(), f"step11-g{gi}-{tag}.png")
            browser.screenshot(page, shot)
            self.log(ctx, f"截图: {os.path.basename(shot)}")
        except Exception as exc:
            self.log(ctx, f"截图失败({tag}): {exc}")

    def _ensure_login(self, ctx: StepContext, browser, page) -> None:
        """登录检查: 未登录自动登录(SS_ACCOUNT/SS_PASSWORD), 失败抛异常。

        自动登录失败后**复查一次**——若复查发现实际已登录(检测误判/会话刚生效),
        直接放行; 否则抛异常。
        """
        if browser.check_sellersprite_login(page):
            return
        self.log(ctx, "卖家精灵未登录, 尝试自动登录")
        account = str(ctx.config.get("SS_ACCOUNT", "") or "")
        password = str(ctx.config.get("SS_PASSWORD", "") or "")
        ok = browser.login_sellersprite(page, account, password, click_wait=_RENDER_WAIT)
        if ok:
            return
        if browser.check_sellersprite_login(page):
            self.log(ctx, "自动登录未找到表单但复查发现实际已登录, 继续")
            return
        raise BrowserError("卖家精灵登录失败(检查 SS_ACCOUNT/SS_PASSWORD 或手动登录)")

    def _already_downloaded(self, group: list[str], preexisting: set,
                            download_dir: str | None = None, *, site: str = "",
                            month: str = "") -> bool:
        """同名文件跳过(断点续跑): 目录已有含本组首个父ASIN 的导出文件,
        或存在 .no-content 空结果标记(该组平台无数据, 已跳过)。

        2026-11-03: **0 字节的 .xlsx 视为"未下载成功"** —— 否则重跑会跳过这些
        空文件，步骤11 永远合并出空表（实测 Automatic Cat Feeders-US-202609 全部 0 字节）。
        """
        if not group:
            return False
        first = group[0]
        for name in preexisting:
            if name.startswith("合并"):
                continue
            if name.endswith(".no-content"):
                # 空结果仅对完整组、站点及月份有效；旧标记不含这些上下文，
                # 无法安全证明可复用，保留旧文件并重新查询该组。
                if not download_dir:
                    continue
                try:
                    marker = json.loads(Path(download_dir, name).read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if (isinstance(marker, dict) and marker.get("version") == 2
                        and marker.get("group") == group and marker.get("site") == site
                        and marker.get("month") == month
                        and (not is_recent30(month) or marker.get("day") == time.strftime("%Y-%m-%d"))):
                    return True
                continue
            if not name.endswith(".xlsx") or first not in name:
                continue
            if download_dir:
                path = os.path.join(download_dir, name)
                if not _valid_xlsx(path):
                    continue          # 空文件、未写完或损坏工作簿不算下载成功
            return True
        return False

    def _mark_empty_skip(self, download_dir: str, gi: int, group: list[str], *,
                         site: str = "", month: str = "") -> None:
        """空结果组写 .no-content 标记(断点续跑按已处理跳过, 不污染 xlsx 合并)。"""
        from tools.attr_stability.spec_io import write_json_atomic
        first = group[0] if group else f"g{gi}"
        day = time.strftime("%Y-%m-%d") if is_recent30(month) else ""
        identity = {"group": group, "site": site, "month": month, "day": day}
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        path = Path(download_dir, f"{first}-{digest}.no-content")
        write_json_atomic(path, {"version": 2, "group_index": gi, **identity})

    def _select_site(self, ctx: StepContext, browser, page, site: str) -> None:
        """选站点(卖家精灵站点下拉按文本点击; 选择器需按线上页面核对)。

        2026-09-16: 改走 `_browser_ux` 的"可见候选 + 停鼠标 + 命中判定点击" —— 原实现用
        `browser.click()`(内部 `locator(sel).first`) 可能点到**视野外/顶栏内**的元素: Playwright
        会先把页面滚动过去、并把鼠标带到 sticky 顶栏 → **顶栏超级菜单展开盖住页面**(实测截图)。
        """
        try:
            if not click_verified(
                page, "[class*=site] [class*=select], [class*=country], .el-select",
                what="站点下拉", log=lambda m: self.log(ctx, m),
            ):
                raise BrowserError("未找到可见的站点下拉(候选选择器均不可见)")
            time.sleep(_RENDER_WAIT)
            browser.wait_selector(page, f"text={site}", timeout=5000)
            if not click_verified(page, f"text={site}", what=f"站点选项[{site}]",
                                  skip_header=False, log=lambda m: self.log(ctx, m)):
                raise BrowserError(f"未点到站点选项: {site}")
            time.sleep(_RENDER_WAIT)
            park_mouse(page)                      # 选完把鼠标挪回内容区(避免顶栏 hover 菜单残留在页面上)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(f"卖家精灵站点选择失败: {site}, {exc}")

    def _select_month(self, ctx: StepContext, browser, page, month: str) -> None:
        """选月份(需求文档技术路线: 下拉选择 mousedown+click 展开 -> 文本模糊匹配)。

        反查页日期为 el-select 下拉(2026-09 实测): 选项 = 最近30天 / 2026-07 / 2026-06 ...
        点击输入框展开下拉, 按文本精确匹配目标月份(如 2026-07), 点击后校验生效。
        注意: 该输入框为 el-select 只读输入框, **fill/键盘输入均不生效**——
        必须走下拉选项点击(此前键盘输入导致日期一直停在默认"最近30天")。
        2026-09: 配置值 = recent30(最近30天标记)时直接选「最近30天」; 目标月份
        在下拉中不存在(如平台尚无 2026-08)时, 询问用户是否改用「最近30天」替代
        并写回 config(webui 子进程不可交互则自动替代, 见 _month_fallback)。
        """
        if not month:
            self.log(ctx, "告警: TRAFFIC_MONTH 未配置, 使用页面默认日期")
            return
        selector = "[class*=date] input, [class*=month] input, .el-date-editor input"
        # 最近30天 标记: 不再要求 YYYY-MM 下拉项, 直接选「最近30天」
        if is_recent30(month):
            try:
                select_recent30(page, selector, log=lambda m: self.log(ctx, m),
                                after_click=lambda: time.sleep(_RENDER_WAIT))
            except Exception as exc:
                raise BrowserError(f"卖家精灵日期(最近30天)选择失败: {exc}") from exc
            return
        try:
            # 展开下拉(2026-09-16: 只点可见候选 + 点前退遮挡; 原 browser.click 会盲取 .first,
            # 可能点到视野外元素 → 页面被滚动 + 鼠标悬停顶栏 → 顶栏超级菜单盖住页面)
            if not click_verified(page, selector, what="月份下拉", timeout=8000,
                                  log=lambda m: self.log(ctx, m)):
                raise BrowserError(f"未找到可见的日期输入框({selector})")
            time.sleep(_RENDER_WAIT)
            # 页面可能有多个下拉(排序等), 必须等**目标文本对应的可见选项**
            item = (
                page.locator(".el-select-dropdown__item:visible")
                .filter(has_text=month)
                .first
            )
            item.wait_for(state="visible", timeout=5000)
            item.click(timeout=5000)
            time.sleep(_RENDER_WAIT)
            # 校验: 输入框值应包含目标月份(防静默用错日期)
            _inp = first_visible(page, selector) or page.locator(selector).first
            val = _inp.input_value()
            if month not in val:
                raise BrowserError(f"日期选择后输入框值异常: {val!r}")
            park_mouse(page)          # 选完把鼠标挪回内容区(下拉已关, 不会被 Esc 影响)
            self.log(ctx, f"日期选择成功(下拉选择 {month})")
        except MonthUnavailableError as exc:
            raise BrowserError(str(exc)) from exc
        except Exception as exc:
            # 目标月可能尚未被平台提供: 列出可用选项, 再询问是否用最近30天替代
            opts: list[str] = []
            try:
                opened = click_verified(page, selector, what="月份下拉(列选项)", timeout=8000,
                                        log=lambda m: self.log(ctx, m))
                if opened:
                    time.sleep(_RENDER_WAIT)
                opts = list_options(page, selector)
            except Exception:  # noqa: BLE001
                pass
            if opts:
                self.log(ctx, "卖家精灵日期下拉可用选项: " + " / ".join(opts[:40]))
            try:
                confirm_or_auto(ctx, "TRAFFIC_MONTH", month, opts,
                                log=lambda m: self.log(ctx, m))
            except MonthFallbackNeedsConfirm as need:
                raise BrowserError(
                    f"[MONTH_FALLBACK_CONFIRM] TRAFFIC_MONTH 目标月 {need.month} 在下拉中不存在; "
                    f"请在网页弹窗确认是否改写为「最近30天」(不同意则保持原值, 自行处理配置)"
                ) from need
            except MonthUnavailableError:
                raise BrowserError(
                    f"卖家精灵日期选择失败: {month}(下拉中不存在, 且未采用最近30天替代; "
                    f"请修改配置 TRAFFIC_MONTH 后重跑), {exc}"
                ) from exc
            # CLI 用户确认: 配置 TRAFFIC_MONTH 已写为「最近30天」(改动已记日志, 不备份) →
            # **停止本步骤**, 由用户重新运行(再次运行将直接选最近30天)
            raise BrowserError(
                f"目标月份 {month} 在下拉中不存在: 配置 TRAFFIC_MONTH 已改为「最近30天」(见上方日志), "
                f"本次运行停止——请重新运行本步骤(步骤11)"
            ) from exc

    def _fill_asins(self, ctx: StepContext, browser, page, group: list[str]) -> None:
        """填入父ASIN(**逗号分隔**, 查流量来源页实测有效; 与 step12 查竞品页不同——
        该页单行输入框要求空格分隔, 两页输入框要求不同, 勿盲目统一)。

        输入框定位失败时抛错并附页面输入元素清单(便于按线上页面修正选择器)。
        2026-09-04 修复(与 step12 同): Playwright fill() 对受控输入框偶发吞首字符
        且静默成功 → 改**原生 value setter + input/change 事件**一次完整写入
        (兼容 textarea/input/contenteditable), 写后**读回校验**——按逗号拆出
        的 ASIN 数必须等于提交数 且 首 ASIN 完整, 缺失即报错不重填不静默。
        """
        text = ",".join(group)
        first = str(group[0]).strip()
        inp = _find_asin_input(page)
        if inp is None:
            raise BrowserError(
                f"未找到 ASIN 输入框; 页面可见输入元素: {_dump_inputs(page)}"
            )
        try:
            cls = inp.get_attribute("class") or ""
        except Exception:
            cls = ""
        self.log(ctx, f"ASIN 输入框已定位(class='{cls}'), 填入 {len(group)} 个父体")
        # 原生写入(在已定位元素上直接执行, 兼容 textarea/input/contenteditable):
        # 受控组件(Vue/React)需触发 input/change 事件更新 model, 一次写入完整不吞字符
        inp.evaluate(
            """(el, text) => {
                const tag = el.tagName;
                if (tag === "TEXTAREA") {
                    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(el, text);
                } else if (tag === "INPUT") {
                    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, text);
                } else if (el.isContentEditable) {
                    el.textContent = text;
                } else {
                    el.value = text;
                }
                el.dispatchEvent(new Event("input", {bubbles: true}));
                el.dispatchEvent(new Event("change", {bubbles: true}));
                return "ok";
            }""",
            text,
        )
        # 读回校验(对齐 step12 严谨度): 按逗号拆出 ASIN 数必须等于提交数 且 首 ASIN 完整
        cur = ""
        try:
            cur = inp.input_value()
        except Exception:
            try:
                cur = inp.inner_text()
            except Exception:
                pass
        got = [p.strip() for p in cur.replace("\n", ",").split(",") if p.strip()]
        self.log(ctx, f"填入校验: 提交 {len(group)} 个, 输入框识别 {len(got)} 个, 首={got[0] if got else '(空)'!r}")
        if got and first in got and len(got) == len(group):
            self.log(ctx, f"已填入 {len(group)} 个父体(逗号分隔, 原生写入+读回校验通过)")
            park_mouse(page)      # 填完把鼠标挪回内容区: 用户看到的"粘贴后下拉挡住页面"就是鼠标停在顶栏造成的
            return
        raise BrowserError(
            f"填父 ASIN 不完整: 提交 {len(group)} 个, 输入框仅识别 {len(got)} 个"
            f"(首 ASIN {first!r} 完整={first in got}); 实际值前120字符: {cur[:120]!r}, "
            f"疑似输入框类型/分隔符不符, 请人工检查后重跑"
        )

    def _click_query(self, ctx: StepContext, browser, page) -> None:
        """发起查询。

        2026-09-16: 改走"可见候选 + 退遮挡 + 命中判定点击" —— 原 `browser.click` 被弹层挡住时
        会先等满 **30 秒**(用户看到的"按钮找不到"), 现在默认 8 秒且会先 `park_mouse` 退遮挡;
        点完再把鼠标挪回内容区(避免顶栏 hover 菜单残留在页面上)。
        """
        ok = click_verified(
            page, "button:has-text('查询'), button:has-text('反查'), [class*=search-btn]",
            what="立即查询", timeout=8000, log=lambda m: self.log(ctx, m),
        )
        if not ok:
            raise BrowserError("未找到或点不到「立即查询」按钮(可能被弹层/顶栏菜单遮挡)")
        time.sleep(_RENDER_WAIT)
        park_mouse(page)

    def _wait_results(self, ctx: StepContext, browser, page, timeout: int) -> str:
        """轮询等待查询结果(技术方案 FR-11): 返回 'ok' / 'error' / 'empty'。

        轮询状态: 错误弹窗(积分不足/无效ASIN) → 加载遮罩消失 → 结果表格出现行
        / 导出按钮可用(非禁用) → 'ok'; 表格渲染完成但**无任何数据行**(空结果,
        如这些父体平台无流量来源数据) → 'empty'。非固定 sleep, 超时抛异常。
        """
        deadline = time.monotonic() + timeout
        empty_seen = 0
        guest_seen = 0
        while time.monotonic() < deadline:
            if _visible_guest_marker(page):
                guest_seen += 1
                if guest_seen >= 2:
                    raise SellerSpriteSessionError("查询过程中卖家精灵页面显示未登录，请检查当前浏览器登录态后续跑")
            else:
                guest_seen = 0
            try:
                browser.wait_selector(
                    page,
                    ".el-message--error, [class*=error-msg], [class*=error-message]",
                    timeout=1500,
                )
                return "error"
            except Exception:
                pass
            # 加载遮罩存在 → 等待加载完成
            try:
                mask = page.locator(".el-loading-mask, [class*=loading-mask]").first
                if mask.is_visible():
                    time.sleep(1)
                    continue
            except Exception:
                pass
            try:
                page.locator(".el-table__row").first.wait_for(
                    state="visible", timeout=1500
                )
                return "ok"
            except Exception:
                pass
            try:
                page.locator(
                    "button:has-text('导出'):not([disabled]), "
                    "[class*=export-btn]:not([disabled]), [class*=export]:not([disabled])"
                ).first.wait_for(state="visible", timeout=1500)
                return "ok"
            except Exception:
                pass
            # 空结果(2026-09-08): 表格空态提示可见 → 疑似"该组父体平台无流量来源数据";
            # 需**连续 2 次轮询确认**(防加载间隙误判), 满足才返回 'empty' 由调用方跳过
            try:
                empty = page.locator(
                    ".el-table__empty-text, text=暂无数据, text=暂无相关数据"
                ).first
                if empty.is_visible():
                    empty_seen += 1
                    if empty_seen >= 2:
                        return "empty"
                else:
                    empty_seen = 0
            except Exception:
                empty_seen = 0
            time.sleep(1)
        raise BrowserError(f"查询等待超时({timeout}s), 未出现结果表格或可用的导出按钮")

    def _export_with_retry(
        self,
        ctx: StepContext,
        browser,
        page,
        download_dir: str,
        timeout: int,
    ) -> str | None:
        """真实点击导出 + 下载监听; 超时进入二级导出兜底, 最多 3 次。

        CDP 模式: 点击动作包裹 expect_download, 下载事件捕获后 save_as 到 download_dir;
        普通模式: downloads_path 自动落盘 + 目录轮询 wait_download。

        点击前悬停导出按钮读取配额 tooltip; "当天剩余0次" → 抛 QuotaExhausted
        (由外层自动切换卖家精灵账号; 检测不到 tooltip 不拦截, 保持原行为)。
        """
        try:
            remaining, hint = read_export_quota(
                page,
                "button:has-text('导出'):not([disabled]), [class*=export-btn]:not([disabled])",
            )
            if remaining == 0:
                raise QuotaExhausted(hint or "当天剩余0次(快速导出2000条已用尽)")
            if remaining is not None:
                self.log(ctx, f"导出配额: 当前账号当天剩余 {remaining} 次")
        except QuotaExhausted:
            raise
        except Exception as exc:
            self.log(ctx, f"配额检测异常(忽略, 继续导出): {exc}")
        def _do_click(attempt: int) -> None:
            """点一次导出按钮（第 1 次直接点，之后走下拉里的"导出Excel"）。"""
            if attempt == 1:
                clicked = click_verified(page, "button:has-text('导出'), [class*=export-btn], [class*=export]",
                                         what="导出", timeout=8000, log=lambda m: self.log(ctx, m))
            else:
                opened = click_verified(page, "[class*=export-menu], .el-dropdown, button:has-text('导出')",
                                        what="导出下拉", timeout=8000, log=lambda m: self.log(ctx, m))
                if not opened:
                    raise BrowserError("未能打开导出菜单")
                time.sleep(_RENDER_WAIT)
                browser.wait_selector(page, "text=导出Excel, text=导出数据", timeout=5000)
                clicked = click_verified(page, "text=导出Excel, text=导出数据", what="导出Excel",
                                         skip_header=False, timeout=8000, log=lambda m: self.log(ctx, m))
            if not clicked:
                raise BrowserError("未能点击导出按钮")
            time.sleep(_RENDER_WAIT)

        for attempt in range(1, 4):
            try:
                before = _download_snapshot(download_dir)
                _do_click(attempt)
                # 快照必须在点击前；否则点击后快速完成的文件会被误算成旧文件。
                # 两种浏览器统一目录轮询，不对 CDP 下载重复执行 save_as。
                path = _wait_new_workbook(download_dir, before, timeout)
                if path:
                    return path
            except Exception as exc:
                self.log(ctx, f"导出点击失败(第{attempt}次): {exc}")
            self.log(ctx, f"导出下载未成功(第{attempt}/3次, {timeout}s), 重试")
        return None

    def _merge(self, ctx: StepContext, download_dir: str, merged_path: str) -> None:
        """全部数据按行合并; 无数据文件时写出空合并文件并告警。"""
        merged = merge_excel_by_rows(
            download_dir,
            merged_path,
            exclude_names=[_MERGED_NAME],
        )
        if merged is None:
            self.log(ctx, "告警: 无数据文件可合并, 写出空合并文件(保证 DONE 校验)")
            df_to_excel(merged_path, {"合并": pd.DataFrame()})
        else:
            self.log(ctx, f"合并完成: {merged_path} 共 {len(merged)} 行")
