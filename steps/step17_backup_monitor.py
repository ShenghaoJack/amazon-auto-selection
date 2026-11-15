"""步骤20 备份 + 最终体检(监控/预警/归档提示) —— **流水线最后一步**。

编号沿革: 本类原为 `number = 19`; 2026-10-09 用户定调"VOC 紧跟步骤18, 备份与体检放最后",
于是 VOC 取 19、本步改为 20(FR-20)。目录名仍沿用遗留的「20_备份与监控」(按路径引用遍及
日志/告警/账本/状态文件, 只改号不改名)。

输入: 全部类目产物(含步骤19 VOC 工作簿) + 归档(编排器每步已完成即时同步, 本步骤不重复做)
输出: 20_备份与监控/health-report.json(体检结果; progress/status/duration 等监控
      文件由编排器/state 自动维护, 本步骤只读)

逻辑:
  1. 备份归并: 运行中即时同步由编排器自动完成; 手动完整快照用 python main.py archive,
     本步骤只做提示, 不执行快照
  2. 体检-抓取质量: 读 <CAT>-dual-details.jsonl / <CAT>-results.jsonl 统计
     总/成功/失败/坏行/状态分布; 风控(bot_blocked/429/503)>=3 或 404>=10 -> WARN
  3. 体检-连续失败: 进度日志尾部连续失败 >= CONSECUTIVE_FAIL_WARN(默认5) -> CRIT
  4. 体检-最近成功率: 最近 RECENT_LOG_LINES(默认20) 条中失败占比
     > RECENT_SUCCESS_RATIO(默认0.5) -> WARN
  5. 体检-卡死检测: step-status.txt 存在 RUNNING 步骤, 且其结果文件
     STUCK_MINUTES(默认10) 分钟未更新 -> CRIT(疑似卡死, 提示检查网络)
  6. 体检-产物完整性: 关键产物存在性/关键词空词/重复ASIN/详情记录数/报告新鲜度
  7. 体检-步骤耗时: step-duration.csv 中耗时超过预设阈值(默认2小时)或
     超过本轮其他步骤均值 N 倍(默认3倍) -> WARN
  8. 体检-LLM用量与费用: 读 20_备份与监控/llm-usage.jsonl 中本轮 run_id 的
      step-summary 累加, 报出总费用/tokens/逐步骤金额/价格来源
      (2026-09-28 新增; **只记录不告警** —— 费用高低是经营判断, 不适合自动 WARN/CRIT)
  9. 预警输出: WARN/CRIT -> append_alert + push_alerts(Webhook 未配置静默) + CRIT popup
  10. 退出码: 0=正常 / 1=有警告 / 2=严重, 写入 health-report.json 并在日志记录;
     本步骤自身失败才抛异常, 体检发现的预警不抛异常(否则会阻断后续归档)

容错: 监控源优先新目录(20_备份与监控), 缺失时兼容历史目录(类目根平铺);
     单检查项内部异常降级为 WARN, 不阻断整个体检
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime
# ⚠ `Path` 是**运行时**要用的（L580 附近的 `Path(root).parent/.name`），必须真导入 ——
#   本文件有 `from __future__ import annotations`，只写在注解里不会报错，写进表达式才炸。
from pathlib import Path

from core import notify, validation
from core.io_utils import read_json, read_txt_lines, write_json
from steps.base import BaseStep, StepContext


def _is_risk_status(status: str) -> bool:
    """风控类状态: 验证码拦截 / 429 限流 / 503 服务不可用。"""
    return "bot_blocked" in status or "429" in status or "503" in status


def _is_not_found_status(status: str) -> bool:
    """404 类状态: 页面不存在或清单含无效 ASIN。"""
    return "not_found" in status or "404" in status


class Step17BackupMonitor(BaseStep):
    # 2026-10-09: 19 → 20 —— VOC 占用 19(紧跟步骤18), 本步收尾(体检才能覆盖 VOC 产物)
    number = 20
    title = "备份与最终体检"
    description = "FR-20: 全产物体检(抓取质量/连续失败/卡死/完整性/耗时)+预警推送+归档提示"
    artifacts = ["20_备份与监控/health-report.json"]
    archive_dirs = ["20_备份与监控"]

    _CHECKERS = [
        ("抓取质量", "_check_fetch_quality"),
        ("连续失败", "_check_consecutive_failures"),
        ("最近成功率", "_check_recent_success_ratio"),
        ("卡死检测", "_check_stuck"),
        ("产物完整性", "_check_artifact_completeness"),
        ("步骤耗时", "_check_step_duration"),
        ("LLM 用量与费用", "_check_llm_cost"),
        # ★ 属性一致性体检（2026-10-08 加）：把 `scripts/audit_attributes.py` 的
        #   不变量挂进流水线**后置检查** —— 这样"口径漂移/值域碎片化/派生不一致/
        #   声明未接通"这类问题每次跑完自己报出来，不必等人一条条发现。
        ("属性一致性", "_check_attribute_consistency"),
    ]

    _MONITOR_RESULTS = {
        0: ("<cat>-bestseller-newrelease-all.txt",),
        1: ("<cat>-dual-details.jsonl",),
        3: ("<cat>-dual-details-filtered.jsonl",),
        4: ("<cat>-keywords.txt", "<cat>-keywords-entities.txt"),
        5: ("<cat>-amazon-keyword-data.json",),
        7: ("<cat>-screen-results.json",),
        8: ("<cat>-keyword-report.xlsx",),
        9: ("<cat>-search-asins.json",),
        10: ("<cat>-results.jsonl",),
        11: ("11_查流量来源/合并-11_查流量来源.xlsx",),
        12: ("12_查竞品/<cat>-submitted.json",),
        13: ("13_查竞品下载/合并-13_查竞品下载.xlsx",),
        14: ("14_流量拆分/流量拆分数据-需人工补充.xlsx",),
        15: ("<cat>-初步类目分析报告.xlsx",),
        16: ("<cat>-attribute-spec.json",),
        17: ("17_属性标注/<cat>-属性标注-单遍原始.xlsx",),
        18: ("18_标准化/<cat>-属性标注-标准化交付.xlsx",),
    }

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始最终体检: CAT={cat} dry_run={ctx.dry_run}")
        self.log(ctx, "本步骤不主动建快照(编排器每步已即时同步); 如需完整快照请执行 python main.py archive")

        checks = [self._run_check(ctx, name, method) for name, method in self._CHECKERS]

        exit_code = 0
        if any(c["level"] == "CRIT" for c in checks):
            exit_code = 2
        elif any(c["level"] == "WARN" for c in checks):
            exit_code = 1

        report = {
            "checked_at": self.now(),
            "checks": checks,
            "exit_code": exit_code,
        }
        report_path = ctx.resolve_artifact(self.artifacts[0])
        write_json(report_path, report)
        self.log(ctx, f"体检报告已写入: {report_path}")

        self._emit_alerts(ctx, checks)

        ok_n = sum(1 for c in checks if c["level"] == "OK")
        warn_n = sum(1 for c in checks if c["level"] == "WARN")
        crit_n = sum(1 for c in checks if c["level"] == "CRIT")
        self.log(ctx, f"体检完成: 检查项 {len(checks)} 个(OK={ok_n} WARN={warn_n} CRIT={crit_n})")
        self.log(ctx, f"体检退出码: {exit_code}(0=正常 1=有警告 2=严重)")

    def _run_check(self, ctx: StepContext, name: str, method_name: str) -> dict:
        """执行单个检查项, 返回 {name, level, message}。"""
        fn = getattr(self, method_name)
        try:
            level, message = fn(ctx)
        except Exception as exc:
            level = "WARN"
            message = f"检查内部异常, 已降级为 WARN: {exc}"
        self.log(ctx, f"体检项[{name}] {level}: {message}")
        return {"name": name, "level": level, "message": message}

    def _emit_alerts(self, ctx: StepContext, checks: list[dict]) -> None:
        """预警输出: append_alert + 企业微信推送(未配置静默) + CRIT 弹窗。"""
        alerts_file = ctx.paths.monitor_file("monitor-alerts.txt")
        for c in checks:
            if c["level"] not in ("WARN", "CRIT"):
                continue
            notify.append_alert(alerts_file, c["level"], f"[{c['name']}] {c['message']}")
            if c["level"] == "CRIT":
                notify.popup_critical(f"[步骤17体检] {c['name']}: {c['message']}")
        webhook = ctx.config.get("WECOM_WEBHOOK", "")
        if webhook:
            notify.push_alerts(webhook, alerts_file)

    def _check_fetch_quality(self, ctx: StepContext) -> tuple[str, str]:
        """抓取质量: 统计总/成功/失败/坏行/状态分布, 风控与 404 超阈值预警。"""
        cat = ctx.config.cat
        sources = [
            ("详情抓取", f"{cat}-dual-details.jsonl"),
            ("变体抓取", f"{cat}-results.jsonl"),
        ]
        total = 0
        ok_count = 0
        bad = 0
        status_counts: dict[str, int] = {}
        risk_count = 0
        not_found_count = 0
        missing = []
        for label, name in sources:
            path = ctx.resolve_artifact(name)
            if not validation.warn_missing(path, name):
                missing.append(label)
                continue
            stats = self._scan_jsonl(path)
            total += stats["total"]
            ok_count += stats["ok"]
            bad += stats["bad"]
            for status, n in stats["status"].items():
                status_counts[status] = status_counts.get(status, 0) + n
                if _is_risk_status(status):
                    risk_count += n
                if _is_not_found_status(status):
                    not_found_count += n

        dist = ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) or "无"
        msg = (f"详情+变体抓取: 总记录 {total} 成功 {ok_count} 失败 {total - ok_count} "
               f"坏行 {bad} 状态分布({dist})")
        remaining = self._remaining_of(ctx, cat, total)
        if remaining is not None:
            msg += f" 清单剩余 {remaining}"
        issues = []
        if missing:
            issues.append("抓取源文件缺失: " + ",".join(missing))
        if risk_count >= 3:
            issues.append(f"风控失败 {risk_count} 条(验证码/bot_blocked/429/503), 建议检查网络环境")
        if not_found_count >= 10:
            issues.append(f"404 共 {not_found_count} 条, 清单可能含无效 ASIN")
        if issues:
            return "WARN", msg + "; " + "; ".join(issues)
        return "OK", msg

    def _scan_jsonl(self, path: str) -> dict:
        """扫描 JSONL: 返回 {total, ok, bad, status} 统计, 坏行计入 bad。"""
        stats = {"total": 0, "ok": 0, "bad": 0, "status": {}}
        if not os.path.exists(path):
            return stats
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        stats["bad"] += 1
                        continue
                    if not isinstance(rec, dict):
                        stats["bad"] += 1
                        continue
                    stats["total"] += 1
                    if rec.get("ok"):
                        stats["ok"] += 1
                    status = str(rec.get("status", "") or "").strip()
                    if not status:
                        status = "ok" if rec.get("ok") else "unknown"
                    stats["status"][status] = stats["status"].get(status, 0) + 1
        except OSError:
            pass
        return stats

    def _remaining_of(self, ctx: StepContext, cat: str, done: int) -> int | None:
        """抓取清单剩余量(清单缺失时返回 None)。"""
        path = ctx.resolve_artifact(f"{cat}-bestseller-newrelease-all.txt")
        if not os.path.exists(path):
            return None
        return max(0, len(read_txt_lines(path)) - done)

    def _check_consecutive_failures(self, ctx: StepContext) -> tuple[str, str]:
        """进度日志尾部连续失败检测, 连续失败 >= 阈值 -> CRIT。"""
        path = self._monitor_or_legacy(ctx, "pipeline-progress.log")
        threshold = int(ctx.config.get("CONSECUTIVE_FAIL_WARN", 5))
        if not validation.warn_missing(path, "pipeline-progress.log"):
            return "OK", "进度日志不存在, 跳过连续失败检查"
        tail = self._read_tail(path, 200)
        count = 0
        for line in reversed(tail):
            if self._is_fail_line(line):
                count += 1
            else:
                break
        if count >= threshold:
            return "CRIT", f"进度日志尾部连续失败 {count} 条(阈值 {threshold}), 疑似流程中断"
        return "OK", f"进度日志尾部连续失败 {count} 条(阈值 {threshold})"

    def _check_recent_success_ratio(self, ctx: StepContext) -> tuple[str, str]:
        """最近若干条日志失败占比超阈值 -> WARN。"""
        path = self._monitor_or_legacy(ctx, "pipeline-progress.log")
        ratio = float(ctx.config.get("RECENT_SUCCESS_RATIO", 0.5))
        window = int(ctx.config.get("RECENT_LOG_LINES", 20))
        if not validation.warn_missing(path, "pipeline-progress.log"):
            return "OK", "进度日志不存在, 跳过最近成功率检查"
        tail = self._read_tail(path, window)
        if not tail:
            return "OK", "最近日志为空, 跳过成功率检查"
        fails = sum(1 for ln in tail if self._is_fail_line(ln))
        fail_ratio = fails / len(tail)
        if fail_ratio > ratio:
            return "WARN", (f"最近 {len(tail)} 条日志失败 {fails} 条(占比 {fail_ratio:.0%}), "
                            f"超过阈值 {ratio:.0%}")
        return "OK", f"最近 {len(tail)} 条日志失败 {fails} 条(占比 {fail_ratio:.0%}, 阈值 {ratio:.0%})"

    def _is_fail_line(self, line: str) -> bool:
        """判断日志行是否为失败行; 排除本步骤自身的日志(避免自污染)。

        2026-09-28: **同时排除 LLM 记账行**。它们的文案里天然含"失败"二字
        (`LLM 用量监控: 价格来源 = 内置快照(抓取失败且无缓存)` /
        `步骤 NN LLM 用量: … | 失败尝试 N 次(未计费)`), 不排除就会被
        [连续失败] 与 [最近成功率] 两项检查当成失败 → **假告警推到企业微信**。
        这里用结构化前缀排除(与上面 [Step17BackupMonitor] 同款), 而不是改文案 ——
        "失败尝试 N 次"本身是有信息量的。
        """
        if "[Step17BackupMonitor]" in line:
            return False
        if "LLM 用量" in line:
            return False
        return "[ERROR]" in line or "失败" in line

    def _read_tail(self, path: str, max_lines: int) -> list[str]:
        """读取文件末尾最多 max_lines 行。"""
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        return lines[-max_lines:]

    def _check_stuck(self, ctx: StepContext) -> tuple[str, str]:
        """卡死检测: RUNNING 步骤且其结果文件长时间未更新 -> CRIT。"""
        statuses = self._read_step_status(ctx)
        running = sorted((n, e) for n, e in statuses.items() if e.get("status") == "RUNNING")
        if not running:
            return "OK", "无 RUNNING 状态步骤, 无需卡死检测"
        stuck_minutes = float(ctx.config.get("STUCK_MINUTES", 10))
        now = time.time()
        stuck_list = []
        for num, entry in running:
            if not self._MONITOR_RESULTS.get(num):
                continue
            start_ts = self._parse_time(entry.get("start", ""))
            start_age = (now - start_ts) / 60.0 if start_ts else None
            result_age = self._result_age(ctx, num, now)
            if start_age is not None and start_age > stuck_minutes and result_age > stuck_minutes:
                stuck_list.append(
                    f"步骤{num:02d}(已开始{start_age:.1f}分钟, 结果文件{result_age:.1f}分钟未更新)"
                )
            elif start_age is None and result_age > stuck_minutes:
                stuck_list.append(f"步骤{num:02d}(无开始时间, 结果文件{result_age:.1f}分钟未更新)")
        if stuck_list:
            return "CRIT", ("疑似卡死: " + "; ".join(stuck_list)
                            + f"(阈值 {stuck_minutes:.0f} 分钟), 建议检查网络环境")
        names = ",".join(f"{n:02d}" for n, _ in running)
        return "OK", f"RUNNING 步骤 {names} 无卡死迹象(阈值 {stuck_minutes:.0f} 分钟)"

    def _result_age(self, ctx: StepContext, num: int, now: float) -> float:
        """该步骤结果文件中最新 mtime 距今的分钟数(无结果文件视为无穷大)。"""
        cat = ctx.config.cat
        newest = 0.0
        for template in self._MONITOR_RESULTS.get(num, ()):
            name = template.replace("<cat>", cat)
            path = ctx.resolve_artifact(name)
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                continue
        if newest <= 0:
            return float("inf")
        return (now - newest) / 60.0

    def _parse_time(self, value: str) -> float | None:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
        except (TypeError, ValueError):
            return None

    def _read_step_status(self, ctx: StepContext) -> dict[int, dict]:
        """读取 step-status.txt(只读, 由编排器/state 维护)。"""
        path = self._monitor_or_legacy(ctx, "step-status.txt")
        result = {}
        if not os.path.exists(path):
            return result
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "|" not in line:
                        continue
                    parts = line.split("|")
                    try:
                        num = int(parts[0])
                    except ValueError:
                        continue
                    result[num] = {
                        "status": parts[1].strip() if len(parts) > 1 else "",
                        "start": parts[2].strip() if len(parts) > 2 else "",
                        "end": parts[3].strip() if len(parts) > 3 else "",
                    }
        except OSError:
            pass
        return result

    def _check_artifact_completeness(self, ctx: StepContext) -> tuple[str, str]:
        """产物完整性: 关键产物存在性/空词/重复ASIN/详情记录数/报告新鲜度。"""
        cat = ctx.config.cat
        fresh_days = float(ctx.config.get("REPORT_FRESH_DAYS", 7))
        issues = []
        notes = []
        key_files = [
            (f"{cat}-keywords.txt", "关键词文件"),
            (f"{cat}-keywords-entities.txt", "五维实体台账"),
            (f"{cat}-search-asins.json", "搜索结果ASIN"),
            (f"{cat}-父项变体明细.xlsx", "父项变体明细"),
            (f"{cat}-初步类目分析报告.xlsx", "初步类目分析报告"),
            (f"{cat}-attribute-spec.json", "属性集规格"),
            (f"18_标准化/{cat}-属性标注-标准化交付.xlsx", "属性标注标准化交付"),
        ]
        for name, label in key_files:
            path = ctx.resolve_artifact(name)
            if not validation.warn_missing(path, label):
                issues.append(f"{label}缺失")
                continue
            if name.endswith("-keywords.txt"):
                words = [ln for ln in read_txt_lines(path) if not ln.startswith("[")]
                if not words:
                    issues.append("关键词文件为空")
                else:
                    notes.append(f"关键词 {len(words)} 个")
            elif name.endswith("-search-asins.json"):
                data = read_json(path, {})
                if not isinstance(data, dict) or not data:
                    issues.append("搜索结果ASIN为空或格式异常")
            elif name.endswith(".xlsx"):
                age_days = (time.time() - os.path.getmtime(path)) / 86400.0
                if age_days > fresh_days:
                    issues.append(f"{label}新鲜度不足({age_days:.1f} 天前生成)")

        all_txt = ctx.resolve_artifact(f"{cat}-search-asins-all.txt")
        if os.path.exists(all_txt):
            asins = read_txt_lines(all_txt)
            dup = len(asins) - len(set(asins))
            notes.append(f"搜索ASIN清单 {len(asins)} 条 重复 {dup} 条")
        details_path = ctx.resolve_artifact(f"{cat}-dual-details.jsonl")
        if os.path.exists(details_path):
            notes.append(f"详情记录 {self._scan_jsonl(details_path)['total']} 条")
        msg = "关键产物(" + ", ".join(notes) + ")" if notes else "关键产物(无统计)"
        if issues:
            return "WARN", msg + "; " + "; ".join(issues)
        return "OK", msg

    def _check_step_duration(self, ctx: StepContext) -> tuple[str, str]:
        """步骤耗时: 超预设阈值或超本轮其他步骤均值 N 倍 -> WARN。"""
        path = self._monitor_or_legacy(ctx, "step-duration.csv")
        max_seconds = float(ctx.config.get("STEP_DURATION_MAX_SECONDS", 7200))
        mean_mult = float(ctx.config.get("STEP_DURATION_MEAN_MULT", 3.0))
        mean_floor = float(ctx.config.get("STEP_DURATION_MEAN_FLOOR", 600))
        if not validation.warn_missing(path, "step-duration.csv"):
            return "OK", "步骤耗时文件不存在, 跳过耗时检查"
        durations = []
        for row in self._read_duration_csv(path):
            try:
                seconds = float(str(row.get("时长秒", "") or "").strip())
            except (TypeError, ValueError):
                seconds = 0.0
            if seconds <= 0:
                continue
            try:
                num = int(row.get("步骤", 0))
            except (TypeError, ValueError):
                num = 0
            durations.append((num, str(row.get("状态", "") or ""), seconds))
        if not durations:
            return "OK", "步骤耗时无有效数据, 跳过耗时检查"
        issues = []
        notes = []
        for num, _status, seconds in durations:
            minutes = seconds / 60.0
            notes.append(f"步骤{num:02d}={minutes:.1f}分钟")
            others = [d for n2, _s, d in durations if n2 != num]
            baseline = (sum(others) / len(others)) if others else 0.0
            if seconds > max_seconds:
                issues.append(
                    f"步骤{num:02d} 耗时 {minutes:.1f} 分钟, 超过预设阈值 {max_seconds / 3600.0:.1f} 小时"
                )
            elif baseline > 0 and seconds > mean_mult * baseline and seconds > mean_floor:
                issues.append(
                    f"步骤{num:02d} 耗时 {minutes:.1f} 分钟, 超过其他步骤均值 "
                    f"{baseline / 60.0:.1f} 分钟的 {mean_mult:.1f} 倍"
                )
        # 总耗时(2026-09-04): 全部有耗时步骤求和, 供监控与告警一眼看到整体进度成本;
        # 步骤17(备份体检)通常瞬时(0s), 求和前剔除 0 秒行避免误导。
        total_sec = sum(seconds for _n, _s, seconds in durations)
        total_min = total_sec / 60.0
        total_h = total_sec / 3600.0
        msg = (f"步骤耗时总{total_h:.2f}小时({total_min:.1f}分钟): "
               + ", ".join(notes))
        if issues:
            return "WARN", msg + "; " + "; ".join(issues) + ", 建议检查是否卡住或数据量异常"
        return "OK", msg

    def _read_duration_csv(self, path: str) -> list[dict]:
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                return list(csv.DictReader(fh))
        except OSError:
            return []

    def _check_llm_cost(self, ctx: StepContext) -> tuple[str, str]:
        """LLM 用量与费用: **只记录, 不告警**(2026-09-28 用户定调)。

        为什么恒 OK: 费用高低是经营判断(模型单价会变、类目体量不同), 不适合自动
        WARN/CRIT —— 会误报的告警会让人不再信任整套告警。这里只把账写进
        health-report.json(体检卡片 / 类目上下文 / 归档都能看到)。需要阈值时再加
        一个配置键 + 一个分支即可。

        数据源 = `20_备份与监控/llm-usage.jsonl`(含轮转分卷)中**本轮 run_id** 的
        step-summary, 按步骤累加。取文件而非内存 meter 的理由: 同一轮可能跨多个进程段
        跑完(强杀续跑), 内存里只有本段, 文件里才是整轮的账; 单独重跑 `main.py step 19`
        时也能报出整轮金额。
        """
        from core.usage import channel_label, ledger_paths, load_records, summarize_run

        try:
            mdir = ctx.paths.monitor_dir()
        except Exception:                                  # noqa: BLE001
            return "OK", "监控目录不可用, 跳过 LLM 费用记录"

        records: list[dict] = []
        for path in ledger_paths(mdir):
            records.extend(load_records(path))
        if not records:
            return "OK", "LLM 用量账本不存在或为空(未落盘/无 LLM 调用), 跳过"

        try:
            run_id = ctx.usage().run_id
        except Exception:                                  # noqa: BLE001
            run_id = ""
        agg = summarize_run(records, run_id)
        t = agg["totals"]
        if not t["calls"] and not t["failed_attempts"]:
            return "OK", f"本轮({run_id or '未知'})无 LLM 调用记录, 跳过"

        steps = sorted(agg["steps"].items(), key=lambda kv: kv[1]["cost_cny"], reverse=True)

        def _live_channels(mapping: dict) -> dict:
            return {c: v for c, v in (mapping or {}).items()
                    if v.get("calls") or v.get("failed_attempts")}

        # 总账: 双通道时拆开(文本/图片是两个模型、两个 key; 图片 token 是服务端按
        # 尺寸折算进 prompt 的, 只看合计看不出图片花了多少)
        head = (f"LLM 费用总{t['cost_cny']:.4f}元"
                f"(累计{t['total_tokens']} tokens, {t['calls']} 次调用)")
        used = _live_channels(agg.get("totals_by_channel"))
        if len(used) > 1:
            head += "[" + " + ".join(
                f"{channel_label(ch)} {v['calls']}次/{v['cost_cny']:.4f}元"
                for ch, v in sorted(used.items())) + "]"

        # 逐步明细: 该步用了两个通道时附通道拆分
        parts = []
        for n, row in steps:
            if not row["calls"]:
                continue
            txt = f"步骤{int(n):02d}={row['cost_cny']:.4f}元({row['total_tokens']}tok)"
            chs = _live_channels(row.get("by_channel"))
            if len(chs) > 1:
                txt += "[" + " + ".join(
                    f"{channel_label(ch)} {v['cost_cny']:.4f}"
                    for ch, v in sorted(chs.items())) + "]"
            parts.append(txt)
        detail = ", ".join(parts) or "无"

        notes: list[str] = []
        if agg["price_source"]:
            notes.append(f"价格来源={agg['price_source']}")
        if t["unpriced_calls"]:
            notes.append(f"未定价 {t['unpriced_calls']} 次(模型不在官方价目表, 费用未计入)")
        if t["failed_attempts"]:
            notes.append(f"失败尝试 {t['failed_attempts']} 次(未计费)")
        tail = ("; " + "; ".join(notes)) if notes else ""
        return "OK", f"{head}: {detail}{tail}"

    def _check_attribute_consistency(self, ctx: StepContext) -> tuple[str, str]:
        """属性一致性体检：跑**与 `scripts/audit_attributes.py` 同一批不变量**。

        为什么放这里：这些是"跨步骤对不对得上"的问题（spec 的 hint vs rules 的受控值、
        交付表的事实值 vs 派生规则、声明键 vs 代码消费点），只有全流程跑完才看得见。
        放在最终体检里，**每次跑完自动报**，不用等人发现（用户 2026-10-08 的明确要求）。

        等级：任何一条不过 = WARN（不阻断归档）；交付表缺失 = WARN（**不是 OK** ——
        没检查不等于通过，这是我自己踩过的假绿灯）。
        """
        import json as _json
        import os as _os
        from glob import glob as _glob

        root = str(ctx.paths.cat_root)
        notes: list[str] = []

        # ★ 接线（2026-10-10）：原先用 `glob(root/"*.rules.json")` —— 定义层迁到
        #   `_catalog/<类目>/` 之后**这里会 glob 到空**，体检直接报"缺 rules.json 或 spec"。
        #   改走解析器（自己的目录优先 → 共享沉淀层继承）。
        try:
            from tools.attr_stability.catalog import resolve_rules_path, resolve_spec_path
            # ⚠ 用 `Path.parent/.name` 而不是 `os.path.dirname/basename`（格式审核：统一口径）——
            #   `root` 带尾斜杠时 `basename` 会得**空串**，`Path().name` 不会。
            _cr = Path(root)
            _root, _cat = str(_cr.parent), _cr.name
            _rp = resolve_rules_path(_root, _cat)
            _sp = resolve_spec_path(_root, _cat)
            rules_p = [str(_rp)] if _rp.is_file() else []
            spec_p = [str(_sp)] if _sp.is_file() else []
        except Exception:                                        # noqa: BLE001 — 兜底原行为
            rules_p = _glob(_os.path.join(root, "*.rules.json"))
            spec_p = _glob(_os.path.join(root, "*-attribute-spec.json"))
        if not (rules_p and spec_p):
            return "WARN", "缺 rules.json 或 spec，无法做属性一致性体检"
        rules = _json.loads(open(sorted(rules_p)[0], encoding="utf-8-sig").read())
        spec = _json.loads(open(sorted(spec_p)[0], encoding="utf-8-sig").read())
        r_by = {a["name"]: a for a in rules.get("attributes") or [] if a.get("name")}
        s_by = {a["name"]: a for a in spec.get("attributes") or [] if a.get("name")}

        # ① hint 的「典型取值」⊆ rules.values（例子越界会诱导模型答非法值）
        try:
            from tools.attr_stability.spec_sync import check_caliber
            bad = [n for n, a in s_by.items()
                   if ((r_by.get(n, {}).get("params") or {}).get("values") or [])
                   and check_caliber(a.get("hint"), r_by[n]["params"]["values"])]
            if bad:
                notes.append("hint 典型取值越界 %d 个属性%s" % (len(bad), bad[:4]))
        except ImportError:
            pass

        # ② 交付表事实校验：是否* 取值域 / 跨属性约束违例
        #   （2026-10-09：原先还有「厚↔厚度一致」，随 `厚度` 列与 `bands` 一起删除）
        xls = sorted(_glob(_os.path.join(root, "18_标准化", "*.xlsx")))
        if not xls:
            notes.append("无交付表 → ②③④ 未检查（**这不等于通过**）")
        else:
            try:
                import openpyxl
                wb = openpyxl.load_workbook(xls[-1], read_only=True, data_only=True)
                ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.active
                it = ws.iter_rows(values_only=True)
                header = [str(x or "") for x in next(it)]
                rows = [dict(zip(header, r)) for r in it]
                wb.close()
                EMPTY = {"", "未明确", "未标注", "不适用", "未知", "待确认", "None", "none"}
                off = {n: sorted({str(r.get(n) or "").strip() for r in rows} - (EMPTY | {"是", "否"}))
                       for n in header if n.startswith("是否")}
                off = {k: v for k, v in off.items() if v}
                if off:
                    notes.append("是否* 取值越界 %d 个属性%s" % (len(off), list(off)[:4]))
                # 分量数上限（用户口径 2026-10-08）：
                # 「主体材质默认 1 个；其他属性表明多材质时才写 2 个 `A+B`；不许 ≥3」
                mp_bad = []
                for n, a in r_by.items():
                    mp = (a.get("params") or {}).get("max_parts")
                    if not mp or n not in header:
                        continue
                    for r in rows:
                        v = str(r.get(n) or "").strip()
                        if v in EMPTY:
                            continue
                        parts = [x for x in v.split("+") if x.strip()]
                        if len(parts) > int(mp):
                            mp_bad.append("%s=%s(%d 分量)" % (n, v, len(parts)))
                            break
                if mp_bad:
                    notes.append("分量数超上限 %s" % mp_bad[:3])
                # 分量数 ↔ 信号属性 双向一致（用户口径 2026-10-08）：
                # 「是否双面不同材质=是 → 主体材质写 2 个 A+B；否则只写 1 个」
                try:
                    from tools.attr_stability.cross_checks import check_parts_dependency
                    dep_bad = check_parts_dependency(
                        [{"xl_row": i + 2, "attributes": r} for i, r in enumerate(rows)], rules)
                    if dep_bad:
                        notes.append("分量数与信号属性不一致 %d 处%s"
                                     % (len(dep_bad), [x["说明"][:30] for x in dep_bad[:2]]))
                except ImportError:
                    pass
                # 2026-10-09 用户口径：**`厚度` 列与 `bands` 都已删除**
                #   （「档位名没必要，这列可以删掉，让 AI 直接判」）——
                #   原先这里做「厚↔厚度 一致」体检（拿 bands 把精确值 `厚` 反查成档名再比对），
                #   现在两者都不存在，该体检作废。厚度信息走 `厚`（尺寸拆列的精确值）。
                #   ⚠ 不在这里补"厚度缺失"告警：删列是**用户决定的口径**，不是缺产物。
                try:
                    from tools.attr_stability.cross_checks import check_rows, load_cross_checks
                    decl = load_cross_checks(rules)
                    if decl:
                        hits = check_rows([{"xl_row": i + 2, "attributes": r}
                                           for i, r in enumerate(rows)], decl)
                        if hits:
                            notes.append("跨属性约束违例 %d 处" % len(hits))
                except ImportError:
                    pass
            except Exception as exc:                                  # noqa: BLE001
                notes.append("读交付表失败(%s)" % str(exc)[:40])

        # ③ bands 无缝无重叠（真实 rules.json）
        for n, a in r_by.items():
            bs = (a.get("params") or {}).get("bands")
            if not bs:
                continue
            try:
                edges = sorted((float(b[1]), float(b[2])) for b in bs)
            except (IndexError, TypeError, ValueError):
                notes.append("%s bands 形状非法" % n)
                continue
            for i in range(len(edges) - 1):
                if edges[i][1] < edges[i + 1][0]:
                    notes.append("%s bands 有洞 %.4g→%.4g" % (n, edges[i][1], edges[i + 1][0]))
                    break

        if not notes:
            return "OK", "属性一致性检查全部通过（口径/取值域/派生/约束）"
        return "WARN", "；".join(notes[:5]) + ("…" if len(notes) > 5 else "")

    def _monitor_or_legacy(self, ctx: StepContext, name: str) -> str:
        """监控源优先新目录, 缺失时兼容历史目录(类目根平铺)。"""
        new_path = ctx.paths.monitor_file(name)
        if os.path.exists(new_path):
            return new_path
        legacy = ctx.resolve_artifact(name)
        if os.path.exists(legacy):
            return legacy
        return new_path
