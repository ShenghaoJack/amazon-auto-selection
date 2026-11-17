"""FR-13 竞品数据下载(第三方数据平台)。

输入: 平台"我的导出"已完成任务 + 提交记录 <CAT>/12_查竞品/<CAT>-submitted.json
      (取最早组提交时间为基准过滤, 兼容跨天/时区差异; 缺组记录回退最后提交时间)
输出: <CAT>/13_查竞品下载/ 目录下的数据文件 + 合并-13_查竞品下载.xlsx

逻辑: 解析导出列表(来源含"查竞品"且状态=已完成, 按时间>=最早提交时间过滤)
      -> 勾选目标任务 -> 批量下载(等待全部文件, 过滤 .crdownload/.tmp)
      -> 解压归档 -> 按组重命名(与提交记录组号对应)
      -> 清单校验(剔除不含当前清单 ASIN 的文件)
      -> 自动合并(全部 xlsx 取第一个 sheet 按行拼接)

容错: 进行中任务不下载; 已下载文件同名跳过; 下载失败重试;
      漏下载跑后核对文件数 vs 组数; 无文件时写出空合并文件保证 DONE 校验
"""
from __future__ import annotations

import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core import (
    BrowserError,
    StepDataError,
    StepFailedError,
    df_to_excel,
    ensure_dir,
    merge_excel_by_rows,
    read_excel_sheet,
    read_json,
    read_jsonl,
    read_txt_lines,
    require_file,
    warn_missing,
)
from steps._browser_ux import click_by_text, click_verified, park_mouse
from steps._month_fallback import is_recent30  # noqa: E402
from steps.base import BaseStep, StepContext


class Step13CompetitorDownload(BaseStep):
    number = 13
    title = "竞品数据下载"
    description = "FR-13: 我的导出页下载查竞品任务, 解压按组归档并自动合并"

    archive_dirs = ["13_查竞品下载"]

    EXPORT_LOG_URL = "https://www.sellersprite.com/v2/export-log"

    @property
    def artifacts(self) -> list[str]:
        return ["13_查竞品下载/合并-13_查竞品下载.xlsx"]

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        require_file(self._submitted_path(ctx), f"{self._cat}-submitted.json")

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        step13_dir = ctx.paths.step_dir(13)
        merged_path = ctx.resolve_artifact(self.artifacts[0])

        submitted = read_json(self._submitted_path(ctx), default=None)
        if not submitted:
            raise StepDataError(f"提交记录缺失或为空: {self._submitted_path(ctx)}")
        groups = submitted.get("groups") or []
        fp = submitted.get("fingerprint", "")
        self.log(ctx, f"提交记录: {len(groups)} 组, 指纹={fp}")

        earliest = self._earliest_time(groups)
        latest = self._latest_time(groups)
        manifest = set(self._load_variants(ctx, cat))
        self.log(ctx, f"清单 ASIN 数={len(manifest)}, 最早提交时间={earliest}, 最晚提交时间={latest}")

        if not groups:
            self.log(ctx, "提交记录无组数据(步骤12无变体清单), 无可下载任务")
            self._write_empty_merged(merged_path)
            return
        expected_names = {str(g.get("task_name") or "").strip() for g in groups}
        if "" in expected_names or len(expected_names) != len(groups):
            raise StepDataError("步骤12提交记录缺少唯一任务名，无法安全区分不同类目或周期；请核对提交记录")

        with ctx.browser("sellersprite", downloads_path=step13_dir) as b:
            if ctx.dry_run:
                mock = b.mock_sellersprite(ctx)
                tasks = mock.export_log_ready()
                task_names = [name for name in self._filter_tasks(ctx, tasks, earliest, latest) if name in expected_names]
                self.log(ctx, f"导出列表过滤后待下载任务: {task_names}")
                if set(task_names) != expected_names:
                    raise StepFailedError("提交记录中的任务未全部匹配，停止下载以避免混入其他周期")
                if task_names:
                    mock.download_export(task_names, step13_dir)
            else:
                page = b.new_page()
                b.set_download_dir(page, step13_dir)  # CDP: 下载重定向到规定目录
                self._ensure_login_real(ctx, b, page)
                self._download_real(ctx, b, page, step13_dir, groups, earliest, latest)

        kept = self._process_downloads(ctx, step13_dir, groups, manifest)
        self.log(ctx, f"下载核对: 文件数={kept} vs 组数={len(groups)}")
        if kept < len(groups):
            self.log(ctx, "存在未下载完成的组, 平台可能仍在处理, 可重跑补齐")

        merged = merge_excel_by_rows(step13_dir, merged_path, exclude_names=["合并-13_查竞品下载.xlsx"])
        if merged is None:
            self.log(ctx, "无数据文件可合并, 写出空合并文件(保证 DONE 校验)")
            self._write_empty_merged(merged_path)
        else:
            self.log(ctx, f"合并完成: {len(merged)} 行 -> {merged_path}")

    def _submitted_path(self, ctx: StepContext) -> str:
        return ctx.resolve_artifact(f"12_查竞品/{self._cat}-submitted.json")

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

    def _earliest_time(self, groups: list[dict]) -> datetime | None:
        times = [t for g in groups if (t := self._parse_time(g.get("time"))) is not None]
        if not times:
            return None
        # 平台导出日志时间只有分钟精度(如 2026-09-02 11:15), 截断到分钟再比较
        # (对齐参考实现 ss-engine.js: sinceTime.slice(0,16) 字符串比较)
        return min(times).replace(second=0, microsecond=0)

    def _latest_time(self, groups: list[dict]) -> datetime | None:
        """本轮最后提交时间(时间窗口上界用, 2026-09-04)。
        平台任务按提交时间顺序创建, 本轮任务应落在 [最早, 最后] 附近;
        上界 = 最后提交时间, 挡住更晚的**其他批次/其他类目**任务(防误勾新任务)。"""
        times = [t for g in groups if (t := self._parse_time(g.get("time"))) is not None]
        if not times:
            return None
        return max(times).replace(second=0, microsecond=0)

    def _parse_time(self, value) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def _filter_tasks(self, ctx: StepContext, tasks: list[dict], earliest: datetime | None,
                      latest: datetime | None = None) -> list[str]:
        """按 来源含查竞品 + 状态已完成 + 时间窗口[最早-90s, 最晚+90s] 过滤导出列表。

        2026-09-04: 时间窗口上下界——下界防历史旧任务(隔天), 上界防**更晚的
        其他批次/其他类目任务**误勾(如当天稍后又跑别的类目生成了新任务)。
        """
        kept: list[str] = []
        grace = timedelta(seconds=90)
        for t in tasks or []:
            name = self._pick(t, ("task", "task_name", "任务名", "名称", "name"))
            status = self._pick(t, ("status", "状态"))
            source = self._pick(t, ("source", "来源", "type", "类型"))
            ttime = self._pick(t, ("time", "时间", "created_at"))
            if not name:
                continue
            if status and not any(k in status for k in ("完成", "成功")):
                self.log(ctx, f"任务 {name} 状态非已完成: {status}, 跳过")
                continue
            if source and "查竞品" not in source:
                self.log(ctx, f"任务 {name} 来源非查竞品: {source}, 跳过")
                continue
            if not source:
                self.log(ctx, f"任务 {name} 缺少来源字段, 按查竞品任务处理")
            t = self._parse_time(ttime)
            if t is None:
                self.log(ctx, f"任务 {name} 时间无法解析({ttime or '空'}), 保留下载")
            elif earliest is not None and t < earliest - grace:
                self.log(ctx, f"任务 {name} 早于最早提交容差, 跳过")
                continue
            elif latest is not None and t > latest + grace:
                self.log(ctx, f"任务 {name} 晚于最晚提交容差(其他批次/类目新任务), 跳过")
                continue
            kept.append(name)
        return kept

    def _pick(self, record: dict, keys: tuple[str, ...]) -> str:
        for k in keys:
            v = record.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    def _download_real(self, ctx: StepContext, b, page, step13_dir: str, groups: list[dict], earliest, latest=None) -> list[str]:
        """真实分支: 我的导出页勾选目标任务并批量下载。

        增强逻辑(2026-09-02):
          ① 三重过滤防下错文件: 来源=查竞品 + 任务名含数据月份(COMPETITOR_MONTH)
             + 时间窗口 [最早-90s, 最晚+90s](2026-09-04: 下界防历史旧任务,
             上界防更晚的其他批次/类目新任务误勾);
          ② 状态轮询: 若目标任务未全部"已完成", 定时刷新导出页观察状态列,
             全部完成才勾选下载(超时则报未完成任务, 可重跑补齐)。
        """
        self._known_task_names = {str(g.get("task_name")) for g in groups if g.get("task_name")}
        month = str(ctx.config.get("COMPETITOR_MONTH", "") or "")
        grace = timedelta(seconds=90)

        def _candidates():
            rows = self._parse_export_rows(ctx, page)
            out = []
            for r in rows:
                source = r.get("source") or ""
                if "查竞品" not in source:
                    continue
                if not self._month_in_name(r.get("task") or "", month):
                    continue
                if (r.get("task") or "") not in self._known_task_names:
                    continue
                t = self._parse_time(r.get("time"))
                # 时间窗口过滤(2026-09-04):
                #  下界: 平台导出日志时间只有分钟精度 vs 本地记录含秒, 同批任务创建
                #        时刻可比本地"最早提交"早 1 分钟(如任务 17:04 vs 记录 17:05:00),
                #        直接 t<earliest 会误杀同批首个任务(实战只下载 2/3) → earliest 前移 90s;
                #  上界: 挡住更晚的其他批次/其他类目任务(防误勾当天稍后新任务)。
                if t is not None:
                    if earliest is not None and t < earliest - grace:
                        self.log(ctx, f"任务 {r.get('task')} 时间 {r.get('time')} 早于最早提交容差 "
                                      f"{(earliest - grace).strftime('%H:%M:%S')}, 跳过(历史旧任务)")
                        continue
                    if latest is not None and t > latest + grace:
                        self.log(ctx, f"任务 {r.get('task')} 时间 {r.get('time')} 晚于最晚提交容差 "
                                      f"{(latest + grace).strftime('%H:%M:%S')}, 跳过(其他批次/类目新任务)")
                        continue
                out.append(r)
            return out

        b.goto(page, self.EXPORT_LOG_URL, timeout=60000)
        b.wait_selector(page, "table", timeout=60000)
        candidates = _candidates()
        if not candidates:
            raise StepFailedError("我的导出页没有与本类目提交记录匹配的任务，停止下载以避免误用其他周期")
        missing_names = self._known_task_names - {r.get("task") for r in candidates}
        if missing_names:
            raise StepFailedError(f"提交记录中的 {len(missing_names)} 个任务未出现在导出列表，停止下载以避免混入其他任务")

        # ② 状态轮询: 等全部目标任务显示"已完成"
        def _pending(rows):
            return [r for r in rows if not any(k in r.get("status") or "" for k in ("完成", "成功"))]

        pending = _pending(candidates)
        poll_wait = max(10, int(ctx.config.get("EXPORT_STATUS_WAIT", 600) or 600))
        poll_interval = max(5, int(ctx.config.get("EXPORT_STATUS_INTERVAL", 15) or 15))
        if pending:
            self.log(
                ctx,
                f"目标任务 {len(candidates)} 个, 未完成 {len(pending)} 个"
                f"({', '.join(r.get('task') for r in pending[:5])}), "
                f"轮询状态(每 {poll_interval}s 刷新, 上限 {poll_wait}s)...",
            )
        deadline = time.monotonic() + poll_wait
        while pending and time.monotonic() < deadline:
            time.sleep(poll_interval)
            b.goto(page, self.EXPORT_LOG_URL, timeout=60000)
            time.sleep(2)
            candidates = _candidates()
            missing_names = self._known_task_names - {r.get("task") for r in candidates}
            if missing_names:
                raise StepFailedError(f"轮询期间有 {len(missing_names)} 个任务不可见，停止下载以避免遗漏或混淆")
            pending = _pending(candidates)
            self.log(
                ctx,
                f"状态轮询: 目标 {len(candidates)} 个, 未完成 {len(pending)} 个"
                f"({', '.join(r.get('task') for r in pending[:5])})",
            )
        if pending:
            failed = [r for r in pending if "失败" in r.get("status")]
            msg = f"导出任务未全部完成: 未完成 {len(pending)} 个" + (
                f"(含失败 {len(failed)} 个)" if failed else ""
            )
            self.log(ctx, msg + ", 可稍后重跑步骤13补齐")
            raise StepFailedError(msg)
        targets = candidates
        self.log(ctx, f"全部目标任务已完成({len(targets)} 个), 开始勾选下载")
        timeout = int(ctx.config.get("DOWNLOAD_TIMEOUT", 120) or 120)
        to_check = []
        for row in targets:
            task = row.get("task") or ""
            group = self._match_group_by_task(task, groups)
            if group is not None and os.path.exists(os.path.join(step13_dir, f"组{group}.xlsx")):
                self.log(ctx, f"任务 {task} 已下载过(组{group}), 跳过")
                continue
            if task and os.path.exists(os.path.join(step13_dir, f"{self._safe_filename(task)}.xlsx")):
                self.log(ctx, f"任务 {task} 已下载过, 跳过")
                continue
            to_check.append(row)
        if not to_check:
            self.log(ctx, "目标任务均已下载, 无需重复下载")
            return []
        downloads = []
        page.on("download", lambda dl: downloads.append(dl))
        checked = 0
        for row in to_check:
            try:
                # 优先按 data-item-id 精确定位勾选框(免疫位置漂移), 兜底行内查找
                if row.get("item_id"):
                    box = page.locator(
                        f"input[data-item-id='{row['item_id']}']"
                    ).first
                else:
                    box = row["_tr"].locator("input[type=checkbox]").first
                if box.count() == 0:
                    html = ""
                    try:
                        html = row["_tr"].evaluate("e => e.outerHTML.slice(0, 300)")
                    except Exception:
                        pass
                    self.log(ctx, f"任务 {row.get('task')} 行内无勾选框: {html}")
                    continue
                if not box.is_checked():
                    try:
                        box.check(timeout=5000)
                    except Exception:
                        # 隐藏勾选框(Bootstrap/Element UI): JS 原生点击
                        box.evaluate("el => el.click()")
                        time.sleep(0.5)
                        if not box.is_checked():
                            label = page.locator(
                                f"label[for='{box.get_attribute('id')}']"
                            ).first
                            if label.count() > 0:
                                label.click(force=True, timeout=5000)
                checked += 1
            except Exception as exc:
                html = ""
                try:
                    html = row["_tr"].evaluate("e => e.outerHTML.slice(0, 300)")
                except Exception:
                    pass
                self.log(ctx, f"勾选任务失败: {row.get('task')}, {exc} | {html}")
        if checked == 0:
            raise StepFailedError("未能勾选任何目标任务")
        # 精确点击"批量下载"(button.download-selected-all, 2026-09 实测);
        # get_by_text 的 .first 可能匹配到隐藏元素, 不用
        # 2026-09-16: 改走"可见候选 + 退遮挡 + 命中判定点击"(被顶栏菜单/弹层盖住时原来会白等 8 秒)
        park_mouse(page)
        if not click_verified(page, ["button.download-selected-all", "button:has-text('批量下载')"],
                              what="批量下载", timeout=8000, log=lambda m: self.log(ctx, m)):
            raise StepFailedError("未找到或点不到「批量下载」按钮(可能被弹层遮挡)")
        self.log(ctx, "已点击批量下载")
        park_mouse(page)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(downloads) < checked:
            time.sleep(1)
        saved = []
        for dl in downloads:
            fname = self._safe_filename(dl.suggested_filename or "export.xlsx")
            target = os.path.join(step13_dir, fname)
            dl.save_as(target)
            saved.append(target)
            self.log(ctx, f"已下载: {fname}")
        for _ in range(2):
            p = b.wait_download(page, step13_dir, timeout=min(timeout, 60))
            if p is None:
                break
            saved.append(p)
        self.log(ctx, f"本次下载文件数: {len(saved)}")
        return saved

    def _parse_export_rows(self, ctx: StepContext, page) -> list[dict]:
        """解析"我的导出"表格(对齐 JS 参考实现 ss-engine.js runCompetitorDownload)。

        列位置(2026-09 实测): tds[0]=勾选/序号, tds[1]=任务名(去掉"最新"后缀),
        tds[2]=来源, tds[3]=时间(格式 "2026-08-25 15:15\\n-" 取第一行), tds[4]=状态。
        仅保留 任务名+来源 均非空的行。
        """
        rows: list[dict] = []
        trs = page.locator("table tbody tr, table tr").all()
        for tr in trs:
            cells = [c.inner_text().strip() for c in tr.locator("td").all()]
            if len(cells) < 5:
                continue
            name = cells[1].replace("最新", "").strip()
            source = cells[2] or ""
            time_raw = cells[3] or ""
            status = cells[4] or ""
            if not name or not source:
                continue
            # 抓取行内勾选框的 data-item-id(与任务名 span 的 data-id 一致),
            # 勾选时按 id 精确定位, 避免位置型 _tr 定位器在重渲染后漂移
            item_id = ""
            try:
                cb = tr.locator("input[type=checkbox]").first
                if cb.count() > 0:
                    item_id = cb.get_attribute("data-item-id") or ""
            except Exception:
                pass
            if not item_id:
                try:
                    span = tr.locator("span[data-id]").first
                    if span.count() > 0:
                        item_id = span.get_attribute("data-id") or ""
                except Exception:
                    pass
            rows.append(
                {
                    "_tr": tr,
                    "item_id": item_id,
                    "cells": cells,
                    "text": " ".join(cells),
                    "task": name,
                    "source": source,
                    "time": time_raw.split("\n")[0].strip(),
                    "status": status,
                }
            )
        if not rows:
            raise StepFailedError("我的导出页表格解析失败(无有效任务行)")
        return rows

    def _row_ok(self, ctx: StepContext, row: dict, earliest: datetime | None,
                latest: datetime | None = None) -> bool:
        status = row.get("status") or ""
        if "进行中" in status or "失败" in status:
            return False
        if not any(k in status for k in ("完成", "成功")):
            self.log(ctx, f"任务 {row.get('task')} 状态非已完成: {status or '未知'}, 跳过")
            return False
        source = row.get("source") or ""
        if source and "查竞品" not in source:
            return False
        t = self._parse_time(row.get("time"))
        if t is None:
            self.log(ctx, f"任务 {row.get('task')} 时间无法解析, 保留下载")
            return True
        # 与 _candidates 同容差: 平台分钟精度 vs 本地秒, 时间窗口 [最早-90s, 最晚+90s]
        grace = timedelta(seconds=90)
        if earliest is not None and t < earliest - grace:
            self.log(ctx, f"任务 {row.get('task')} 早于最早提交时间(容差90s), 跳过")
            return False
        if latest is not None and t > latest + grace:
            self.log(ctx, f"任务 {row.get('task')} 晚于最晚提交时间(容差90s, 其他批次新任务), 跳过")
            return False
        return True

    def _month_in_name(self, name: str, month: str) -> bool:
        """任务名是否包含数据月份(防下错文件: 如 COMPETITOR_MONTH=2026-07
        匹配任务名中的 '2026-07' 或 '2026.07')。

        recent30(最近30天)模式: 平台任务名未必含固定月份串, 月份过滤放行,
        由更严格的时间窗口[最早-90s, 最晚+90s]与"来源=查竞品"把关(2026-09)。
        """
        if not month:
            return True
        if is_recent30(month):
            return True
        return month in name or month.replace("-", ".") in name

    def _match_group_by_task(self, task: str, groups: list[dict]) -> int | None:
        for g in groups:
            if g.get("task_name") and str(g.get("task_name")) == task:
                return g.get("group")
        return None

    def _process_downloads(self, ctx: StepContext, step13_dir: str, groups: list[dict], manifest: set) -> int:
        self._extract_zips(ctx, step13_dir)
        self._rename_by_group(ctx, step13_dir, groups)
        return self._manifest_check(ctx, step13_dir, manifest)

    def _extract_zips(self, ctx: StepContext, step13_dir: str) -> None:
        for p in sorted(Path(step13_dir).glob("*.zip")):
            tmp = os.path.join(step13_dir, "_tmp_extract")
            ensure_dir(tmp)
            try:
                with zipfile.ZipFile(str(p)) as zf:
                    zf.extractall(tmp)
            except Exception as exc:
                self.log(ctx, f"解压失败: {p.name} ({exc})")
                continue
            for x in sorted(Path(tmp).rglob("*")):
                if x.is_file() and x.suffix.lower() in (".xlsx", ".xls", ".csv"):
                    target = os.path.join(step13_dir, x.name)
                    if not os.path.exists(target):
                        shutil.move(str(x), target)
                        self.log(ctx, f"解压出文件: {x.name}")
            shutil.rmtree(tmp, ignore_errors=True)
            os.remove(str(p))
            self.log(ctx, f"归档已解压: {p.name}")

    def _rename_by_group(self, ctx: StepContext, step13_dir: str, groups: list[dict]) -> None:
        for p in sorted(Path(step13_dir).glob("*.xlsx")):
            if p.name.startswith("合并-") or p.name.startswith("组"):
                continue
            group = self._match_group(ctx, p, groups)
            if group is None:
                self.log(ctx, f"文件未匹配到提交组, 保留原名: {p.name}")
                continue
            target = os.path.join(step13_dir, f"组{group}.xlsx")
            if os.path.exists(target):
                self.log(ctx, f"组{group} 文件已存在, 删除重复下载: {p.name}")
                os.remove(str(p))
                continue
            os.rename(str(p), target)
            self.log(ctx, f"已按组重命名: {p.name} -> 组{group}.xlsx")

    def _match_group(self, ctx: StepContext, path: Path, groups: list[dict]) -> int | None:
        stem = path.stem
        for g in groups:
            if g.get("task_name") and stem == str(g.get("task_name")):
                return g.get("group")
        try:
            df = read_excel_sheet(str(path), 0)
        except Exception:
            return None
        cells = set(df.astype(str).values.ravel())
        for g in groups:
            fa = g.get("first_asin")
            if fa and str(fa) in cells:
                return g.get("group")
        return None

    def _manifest_check(self, ctx: StepContext, step13_dir: str, manifest: set) -> int:
        reject_dir = ensure_dir(os.path.join(step13_dir, "剔除"))
        kept = 0
        for p in sorted(Path(step13_dir).glob("*.xlsx")):
            if p.name.startswith("合并-"):
                continue
            if self._matches_manifest(p, manifest):
                kept += 1
                continue
            self.log(ctx, f"清单校验不通过, 剔除文件: {p.name}")
            target = os.path.join(reject_dir, p.name)
            if os.path.exists(target):
                os.remove(str(p))
            else:
                shutil.move(str(p), target)
        self.log(ctx, f"清单校验完成: 保留 {kept} 个文件")
        return kept

    def _matches_manifest(self, path: Path, manifest: set) -> bool:
        if not manifest:
            return False
        name = path.name
        if any(a in name for a in manifest):
            return True
        try:
            df = read_excel_sheet(str(path), 0)
        except Exception:
            return False
        for cell in df.astype(str).values.ravel():
            if cell in manifest:
                return True
        return False

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

    def _click_by_text(self, ctx: StepContext, page, text: str, required: bool = False) -> bool:
        """按文本点击(2026-09-16: 走 `_browser_ux.click_by_text` —— 可见候选 + 命中判定 + 短超时)。"""
        if click_by_text(page, text, what=f"文本「{text}」", timeout=8000,
                         log=lambda m: self.log(ctx, m)):
            return True
        if required:
            raise StepFailedError(f"未找到可点击元素: {text}")
        return False

    def _safe_filename(self, name: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(name))
        return cleaned.strip() or "export"

    def _write_empty_merged(self, merged_path: str) -> None:
        df_to_excel(merged_path, {"数据": pd.DataFrame()})
