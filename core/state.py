"""步骤状态与断点续跑。

对应需求: 步骤状态含时间戳(开始/结束/时长), 每步 DONE/SKIP 时写入,
供断点续跑与耗时统计; 步骤标记 DONE 必须以产物实际生成为前提 (7.3 硬性)。

状态文件: 20_备份与监控/step-status.txt
耗时记录: 20_备份与监控/step-duration.csv
"""
from __future__ import annotations

import csv
import os
from datetime import datetime

from .io_utils import atomic_write_text
from steps.ids import PIPELINE_STEP_NUMBERS, require_step

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_DONE = "DONE"
STATUS_SKIP = "SKIP"
STATUS_FAILED = "FAILED"
STATUS_INTERRUPTED = "INTERRUPTED"  # 运行被中断(Ctrl+C / 进程终止), 区别于完成的结束时间

_DONE_OK = {STATUS_DONE, STATUS_SKIP}


def _span_seconds(start: str, end: str) -> float | None:
    """两时间戳的墙钟秒差; 缺失/非法/非正返回 None。"""
    if not start or not end:
        return None
    try:
        s = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        t = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        sec = (t - s).total_seconds()
    except ValueError:
        return None
    return sec if sec > 0 else None


class RunState:
    """管理单轮运行中 0-20 各步骤的状态与耗时。"""

    def __init__(self, status_file: str, duration_csv: str):
        self.status_file = status_file
        self.duration_csv = duration_csv
        self.steps: dict[int, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.status_file):
            return
        # 2026-09-11: 用 utf-8-sig 读取 —— 状态文件是纯文本, 一旦被记事本 / Windows PowerShell 5.1
        # 的 `Set-Content -Encoding utf8` 改写就会带上 BOM; 若按 utf-8 读, 首行会变成 "\ufeff0|DONE|…",
        # `int(parts[0])` 解析失败 → **步骤 00 被静默丢掉** → first_unfinished() 返回 0 →
        # 主循环从 0 无条件重跑全部 18 步(实战: 2026-09-11 因此白跑一轮)。
        # utf-8-sig 会自动吃掉 BOM, 无 BOM 时行为完全不变。
        with open(self.status_file, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) < 4:
                    continue
                try:
                    num = int(parts[0])
                except ValueError:
                    continue
                entry = {
                    "status": parts[1],
                    "start": parts[2] or "",
                    "end": parts[3] or "",
                    "acc": 0,
                    "fails": 0,
                    "ints": 0,
                }
                if len(parts) > 4 and parts[4]:
                    entry["reason"] = parts[4]
                if len(parts) > 5:
                    try:
                        entry["acc"] = int(float(parts[5]))
                    except (TypeError, ValueError):
                        entry["acc"] = 0
                if len(parts) > 6:
                    try:
                        entry["fails"] = int(float(parts[6]))
                    except (TypeError, ValueError):
                        entry["fails"] = 0
                if len(parts) > 7:
                    try:
                        entry["ints"] = int(float(parts[7]))
                    except (TypeError, ValueError):
                        entry["ints"] = 0
                self.steps[num] = entry

    def start_step(self, number: int, end_hint: str = "") -> None:
        """开始执行某步; end_hint: 上次强杀残留(无结束时刻)的近似中断结束时间。

        耗时累计(acc, 用户口径 2026-09): 步骤一旦开始就计时; 中断/失败即暂停,
        暂停前已执行时长并入 acc 保留在卡片; 重跑从暂停值**继续计时**。
        强杀残留(RUNNING 无 end): 计 1 次中断, 并以 end_hint(日志最后活动时间,
        缺省=本轮检测/重跑时刻)作为近似结束点并入 acc, 使页面中断的段也能续计。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prev = self.steps.get(number, {})
        prev_status = prev.get("status", "")
        if prev_status == STATUS_RUNNING and not prev.get("end"):
            # 强杀残留: 转成一次"中断", 结束时间取近似值后并入累计
            prev["ints"] = int(prev.get("ints", 0) or 0) + 1
            prev["status"] = STATUS_INTERRUPTED
            # end 优先用 end_hint(该步日志最后真实活动时间); 找不到时按 start 计
            # (span=0 只保留 acc) —— 2026-09 修复: 若回退"本轮检测时刻", 会把
            # 上次 start → 本轮启动 之间未运行的闲置时段误并入 acc(虚增累计时长)。
            prev["end"] = end_hint or prev.get("start") or now
            prev_status = STATUS_INTERRUPTED
        if prev_status in (STATUS_INTERRUPTED, STATUS_FAILED):
            acc = float(prev.get("acc", 0) or 0)
            span = _span_seconds(prev.get("start", ""), prev.get("end", ""))
            if span:
                acc += span
            prev["acc"] = acc
        prev.update({"status": STATUS_RUNNING, "start": now})
        # 本轮开始: 清掉上一轮残留的结束时间/原因, 避免与"完成结束/中断时间"混淆
        prev.pop("end", None)
        prev.pop("reason", None)
        self.steps[number] = prev
        self._save()

    def mark_interrupted(self, number: int, reason: str = "") -> None:
        """把进行中的步骤标记为中断(结束时间=中断时间, 区别于 DONE 的完成时间)。

        触发: Ctrl+C 等可捕获中断(真实中断时间); 进程被强制终止的残留 RUNNING
        由 interruptions() 在下次启动时报告(中断时间≈检测时刻)。
        """
        self._bump(number, "ints")           # 累计中断次数(卡片统计, 重跑不清零)
        self._finish(number, STATUS_INTERRUPTED, reason)

    def _bump(self, number: int, field: str) -> None:
        """累计计数(如 fails/ints): +1 并持久化(卡片 FAILED/INTERRUPTED 次数)。"""
        entry = self.steps.get(number, {})
        entry[field] = int(entry.get(field, 0) or 0) + 1
        self.steps[number] = entry
        self._save()

    def interruptions(self) -> list[dict]:
        """返回"上次运行中断"的步骤信息(不修改状态), 供启动时打日志区分中断/完成。

        stale_running: 上次进程被终止, 状态残留 RUNNING 且无结束时间(中断时间≈检测时刻);
        interrupted: 已明确标记 INTERRUPTED(带中断时间 end)。
        """
        out: list[dict] = []
        for num in sorted(self.steps):
            e = self.steps[num]
            status = e.get("status")
            if status == STATUS_RUNNING and not e.get("end"):
                out.append(
                    {
                        "number": num,
                        "start": e.get("start", ""),
                        "end": "",
                        "kind": "stale_running",
                    }
                )
            elif status == STATUS_INTERRUPTED:
                out.append(
                    {
                        "number": num,
                        "start": e.get("start", ""),
                        "end": e.get("end", ""),
                        "kind": "interrupted",
                        "reason": e.get("reason", ""),
                    }
                )
        return out

    def mark_done(self, number: int) -> None:
        self._finish(number, STATUS_DONE)

    def mark_skip(self, number: int) -> None:
        self._finish(number, STATUS_SKIP)

    def mark_failed(self, number: int, reason: str = "") -> None:
        self._bump(number, "fails")          # 累计失败次数(卡片统计, 重跑不清零)
        self._finish(number, STATUS_FAILED, reason)

    def mark_pending(self, number: int) -> None:
        """把步骤重置为 PENDING(断点保护: 已 DONE 但产物质量不合格时)。

        清除上一轮 start/end/reason: PENDING 不带旧时间, 重跑由 start_step 记新开始。
        """
        entry = self.steps.get(number, {})
        entry["status"] = STATUS_PENDING
        entry.pop("start", None)
        entry.pop("end", None)
        entry.pop("reason", None)
        self.steps[number] = entry
        self._save()

    def _finish(self, number: int, status: str, reason: str = "") -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = self.steps.get(number, {"start": ""})
        entry["status"] = status
        entry["end"] = now
        if reason:
            entry["reason"] = reason
        self.steps[number] = entry
        self._save()
        self._save_duration_csv()

    def _save(self) -> None:
        lines = []
        for num in sorted(self.steps):
            e = self.steps[num]
            acc = int(float(e.get("acc", 0) or 0))
            fails = int(e.get("fails", 0) or 0)
            ints = int(e.get("ints", 0) or 0)
            # 固定 8 列: num|status|start|end|reason|acc|fails|ints(向后兼容旧 4-7 列文件)
            # 2026-09-13: 写入前净化字段 —— reason 来自异常文本(str(exc)[:300]), 可能含 "|" 或换行,
            # 直接拼接会让读回侧(split("|") 按位置取值)错位: 多一个 "|" 时 acc/fails/ints 顺位错读(且
            # int() 失败被吞成 0), 含换行时整行被劈开、带着真实计数的后半行被当坏行丢弃, 并且下一次
            # _save 会用这份已错位的内存状态整文件重写把错误固化。
            def _cell(value) -> str:
                return (str(value if value is not None else "")
                        .replace("|", "/").replace("\r", " ").replace("\n", " "))
            line = (f"{num}|{e['status']}|{_cell(e.get('start'))}|{_cell(e.get('end'))}"
                    f"|{_cell(e.get('reason'))}|{acc}|{fails}|{ints}")
            lines.append(line)
        atomic_write_text(self.status_file, "\n".join(lines) + "\n")

    def _save_duration_csv(self) -> None:
        rows = []
        for num in sorted(self.steps):
            e = self.steps[num]
            start = e.get("start", "")
            end = e.get("end", "")
            acc = float(e.get("acc", 0) or 0)
            total = acc
            if start and end:
                span = _span_seconds(start, end)
                if span is not None:
                    total += span
            duration = f"{total:.1f}" if (total or (start and end)) else ""
            rows.append({"步骤": num, "状态": e.get("status", ""), "开始": start, "结束": end, "时长秒": duration})
        if not rows:
            return
        os.makedirs(os.path.dirname(self.duration_csv), exist_ok=True)
        with open(self.duration_csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=["步骤", "状态", "开始", "结束", "时长秒"])
            writer.writeheader()
            writer.writerows(rows)

    def status_of(self, number: int) -> str:
        return self.steps.get(number, {}).get("status", STATUS_PENDING)

    def duration_seconds(self, number: int) -> float | None:
        """该步骤累计耗时(秒): acc(历史中断/失败段) + 本轮 结束(end)-开始(start);
        同秒完成返回 0(不视为缺失); 无开始/结束且无历史段返回 None。"""
        e = self.steps.get(number, {})
        acc = float(e.get("acc", 0) or 0)
        start, end = e.get("start", ""), e.get("end", "")
        if start and end:
            try:
                s = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
                t = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
                return acc + max(0.0, (t - s).total_seconds())
            except ValueError:
                return None
        return acc if acc else None

    def duration_text(self, number: int) -> str:
        """该步骤耗时的人类可读文本(如 '3分25秒' / '1小时5分10秒'); 无法计算返回 ''。"""
        sec = self.duration_seconds(number)
        if sec is None:
            return ""
        return format_duration(sec)

    def cumulative_seconds(self, now: datetime | None = None,
                           end_hint_for=None) -> float:
        """全部步骤的**累计执行时长**(秒) —— 与 webui 看板「累计执行时长」同口径。

        口径(2026-09-15 统一): 每个步骤累加 `acc(历史中断/失败段) + (end - start)`;
        **正在运行**的步骤(有 start、无 end)按 `acc + (now - start)` 实时计入;
        未开始的步骤不计。它统计的是**真正在执行的时间**, 因此:
        - 被强杀/中断后重跑的段**会**并入(重跑接续计时, 不丢已跑时间);
        - 进程段之间"没有在跑"的空档、以及被强杀后日志停止到进程真正退出之间的
          闲置时段**不会**计入(那是 _span_seconds/日志活动的口径, 不是壁钟)。

        用途: 流水线跑完时在日志里报"本轮总耗时" —— 若只报 `now - 进程启动时刻`,
        在"中断→续跑→再中断→再续跑"这类分多段跑完的轮次里会严重少报
        (实例 2026-09-15: 分 3 段跑完 18 步, 单进程段口径只报 4h33m,
        累计口径为 12h51m, 与页面一致)。

        now: 运行中步骤的计时终点(默认取当前时刻); 传固定值便于测试与前后端对账。
        end_hint_for: 可选 `(num) -> "YYYY-mm-dd HH:MM:SS" | None`。页面在"进程已被
            强杀、状态文件仍残留 RUNNING"时, 用该步**日志最后活动时间**近似中断时刻
            (只计到真正在跑的那一刻, 不把"日志停了、进程还没退"的闲置段算进去)。
            看板渲染会把 webui 的同名函数传进来, 保证前后端连该边界情形也同口径;
            流水线自己的完成行不用(那时步骤只会是 DONE/SKIP/FAILED)。
        """
        now_dt = now or datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        total = 0.0
        for num in self.steps:
            e = self.steps[num]
            acc = float(e.get("acc", 0) or 0)
            start, end = e.get("start", ""), e.get("end", "")
            if start and not end and end_hint_for:
                # "残留 RUNNING"边界: 优先用日志最后活动时间(无则回退 now)
                try:
                    hint = end_hint_for(num) or ""
                except Exception:  # noqa: BLE001 — 画像/日志读取失败不得影响统计
                    hint = ""
                if hint and hint >= start:
                    total += acc + (_span_seconds(start, hint) or 0.0)
                    continue
            if start and end:
                span = _span_seconds(start, end)
                total += acc + (span or 0.0)
            elif start:
                # 运行中: 按 start → now 实时计入
                span = _span_seconds(start, now_str)
                total += acc + (span or 0.0)
            else:
                total += acc
        return total

    def is_finished(self, number: int) -> bool:
        return self.status_of(number) in _DONE_OK

    def first_unfinished(self, from_step: int = 0) -> int | None:
        """返回 from_step 起第一个未完成步骤(断点续跑入口)。"""
        require_step(from_step)
        for num in PIPELINE_STEP_NUMBERS:
            if num < from_step:
                continue
            if not self.is_finished(num):
                return num
        return None

    def summary(self) -> str:
        lines = []
        for num in sorted(self.steps):
            e = self.steps[num]
            dur = self.duration_text(num)
            lines.append(
                f"步骤{num:02d} [{e.get('status','')}] 开始={e.get('start','')} "
                f"结束={e.get('end','')} 耗时={dur or '-'}"
            )
        return "\n".join(lines)


def format_duration(seconds: float) -> str:
    """秒 → 人类可读耗时文本: '45秒' / '3分25秒' / '1小时5分10秒'。"""
    sec = max(0.0, float(seconds))
    if sec < 60:
        return f"{sec:.0f}秒"
    total_min = int(sec // 60)
    rem_sec = int(sec % 60)
    if total_min < 60:
        return f"{total_min}分{rem_sec}秒"
    hours = total_min // 60
    mins = total_min % 60
    return f"{hours}小时{mins}分{rem_sec}秒"
