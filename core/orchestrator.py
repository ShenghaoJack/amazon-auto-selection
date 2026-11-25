"""流程编排器 (3.2 执行方式 / 7.3 硬性校验)。

- 全流程 0-20 / 从指定步骤续跑(断点续跑)
- 步骤标记 DONE 必须以产物实际生成为前提
- 失败必须阻断流程或显式告警, 禁止静默标成功
- 每步完成后即时同步归档(3.4 数据保存要求)
- 步骤耗时记录: 进入时记开始时间, DONE/SKIP 时记结束时间 (FR-17)
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from core import notify
from core.archive import sync_step_dir, sync_step_files
from core.config import Config
from core.errors import EmptyExportPause, WaitingForHuman
from core.logger import setup_logger
from core.paths import Paths
from core.state import RunState, _span_seconds, format_duration
from steps.ids import MONITOR_STEP, PIPELINE_STEP_NUMBERS, require_step
from core.usage import load_records, summarize_run
from steps.base import BaseStep, StepContext
from steps.registry import get_steps

# ---- 登录态依赖表 (2026-09-29): 按步骤精确预检查 ----
# 亚马逊登录态与卖家精灵登录态是两套完全独立的凭据, COOKIE 只被 HTTP 直连步骤用;
# 纯本地计算步骤(2/3/4/7/8/14-19)与纯接口步骤(5/6 走 MCP, 用 API key)既不弹浏览器
# 也不读 COOKIE —— 对它们做登录预检查只有副作用: 白拉起浏览器窗口, 还会因**无关的**
# COOKIE 失效/卖家精灵掉登录把纯本地步骤直接拦死(实例: 2026-09-29 单步跑步骤8 被拦)。
# 依赖关系与各步实际调用的浏览器一一对应(可用 `ctx.browser(` 全仓核对):
COOKIE_STEPS = frozenset({1, 10})                      # 亚马逊 COOKIE(纯 HTTP 直连)
AMAZON_LOGIN_STEPS = frozenset({0, 1, 9, 10})          # amazon 浏览器 Profile
SELLERSPRITE_LOGIN_STEPS = frozenset({0, 11, 12, 13})  # sellersprite 浏览器 Profile
# 步骤 0 同时拉起两套: step00.run 内部既调 ensure_amazon_login 也调 ensure_sellersprite_login


def _is_step_real_activity(line: str, num: int) -> bool:
    """日志行是否属于该步骤的**真实执行活动**(与 webui 展示口径一致)。

    只认:
    - `[StepNN...]` 步骤类日志(如 [Step10Variants])
    - `=== 步骤 NN … 开始 ===` 与 步骤结束行(完成/失败/跳过)

    排除 "上次运行被进程强杀/终止"、"断点续跑: …从步骤 NN 开始" 等
    **编排报告行**(它们也含 "步骤 NN" 字样, 但时间是本轮启动时刻,
    若当活动取会虚增 acc——2026-09 修复)。
    """
    if f"[Step{num:02d}" in line:
        return True
    if re.search(r"=== 步骤\s*%d\s+" % num, line):
        return True
    if re.search(r"步骤\s*%d\s+(?:完成|失败|跳过)" % num, line):
        return True
    return False


def finish_line(state: RunState, seg_delta, now: datetime | None = None,
                extra: str = "") -> str:
    """流水线完成行(2026-09-15 起耗时口径与页面统一)。

    `本轮总耗时` = **累计执行时长**(各步 acc + 段内时长之和, 与 webui 看板
    「累计执行时长」同一实现 `RunState.cumulative_seconds`); 另附两个参照数:
    - `本进程段`: 本次进程从启动到现在的墙钟(旧口径, 单段跑完时与总数接近);
    - `整轮墙钟`: 步骤00开始 → 现在(含中断后没在跑的空档)。

    为什么必须这样报(实例 2026-09-15): 同一轮被强杀两次、分 3 段跑完 18 步,
    只报本进程段会得到 4h33m, 而页面显示累计 12h51m —— 两个数看着打架。现在
    日志首数就是页面口径, 另两数解释差额来源。

    `extra`(2026-09-28): 尾部追加的自由文本(当前用于 LLM 用量/费用小结)。
    默认空串 → 完成行与旧版逐字节一致, 现有测试与文档引用不受影响。

    state: 运行状态(提供 cumulative_seconds); seg_delta: 本进程段已运行时长
    (timedelta); now: 计时终点(默认当前时刻, 便于测试)。
    """
    now_dt = now or datetime.now()
    cum_sec = state.cumulative_seconds(now_dt)
    seg_sec = seg_delta.total_seconds() if seg_delta is not None else 0.0
    first_start = state.steps.get(0, {}).get("start", "")
    wall = _span_seconds(first_start, now_dt.strftime("%Y-%m-%d %H:%M:%S"))
    extra_txt = f" | 本进程段 {format_duration(seg_sec)}"
    if wall:
        extra_txt += f" | 整轮墙钟 {format_duration(wall)}"
    return f"流水线全部步骤完成(本轮总耗时 {format_duration(cum_sec)}{extra_txt}{extra})"


class PipelineRunner:
    """按 0-20 顺序执行步骤并维护状态。"""

    def __init__(self, config: Config, dry_run: bool | None = None):
        if dry_run is not None:
            config.set("DRY_RUN", dry_run)
        self.config = config
        if not config.cat:
            raise ValueError("配置缺少 CAT(类目名)")
        self.paths = Paths(config)
        self.log = setup_logger(self.paths.monitor_file("pipeline-progress.log"))
        self.state = RunState(
            self.paths.monitor_file("step-status.txt"),
            self.paths.monitor_file("step-duration.csv"),
        )
        self.ctx = StepContext(config, self.paths, self.log, self.state)
        self._ss_workflow_usage = None

    def _hold_ss_workflow(self) -> None:
        """Keep a shared browser alive between steps 11, 12 and 13."""
        if self.config.dry_run or getattr(self, '_ss_workflow_usage', None) is not None:
            return
        from core.ss_auto_close import register
        from core.ss_lock import pick_port, resource_key
        url = str(self.config.get('SS_CDP_URL', '') or '').strip()
        if not url or url.lower() == 'auto':
            user = str(os.environ.get('PIPELINE_WORKBENCH_USER') or '').strip()
            profile = self.paths.profile_ss() if user else ''
            port = pick_port(user, profile_dir=profile) if profile else pick_port(user)
            url = f'http://127.0.0.1:{port}'
            self.config.set('SS_CDP_URL', url)
        self._ss_workflow_usage = register(resource_key(url, ''))

    def _release_ss_workflow(self) -> None:
        handle, self._ss_workflow_usage = getattr(self, '_ss_workflow_usage', None), None
        if handle:
            from core.ss_auto_close import unregister
            unregister(handle, self.log.info)

    def snapshot_dir(self) -> str:
        """本轮归档快照目录(所有步骤共享同一轮)。

        同时把**快照目录名**设为本轮 LLM 账本的 run_id(2026-09-28): 项目的语义是
        「归档复用该类目最新快照, 同一轮续跑不新建」 → 快照名就是"该类目本轮"的稳定
        标识。若按进程时间戳生成 run_id, "一轮被强杀后分 3 段跑完"会被拆成 3 本账,
        "本轮总共花了多少"就不存在了。
        """
        snap = self.paths.snapshot_dir()
        self._snapshot = snap
        self.ctx.set_run_id(os.path.basename(snap))
        return snap

    def _sync_after_step(self, step: BaseStep) -> None:
        if not getattr(self, "_snapshot", None):
            return
        step.bind_cat(self.ctx)
        files = [self.ctx.resolve_artifact(a) for a in step.artifacts]
        files += [self.ctx.resolve_artifact(a) for a in step.extra_archive_files
                  if os.path.exists(self.ctx.resolve_artifact(a))]
        sync_step_files(step.number, files, self._snapshot, self.paths)
        for d in step.archive_dirs:
            sync_step_dir(step.number, self.paths.resolve_cat_relative(d), self._snapshot, self.paths)
        self._sync_cfg_to_snapshot(self._snapshot)   # 类目配置随快照归档(2026-09)

    def _sync_cfg_to_snapshot(self, snap: str) -> None:
        """把 data/<CAT>/config.yaml 同步进归档快照根目录(配置跟着类目备份)。"""
        try:
            src = os.path.join(self.paths.cat_root, "config.yaml")
            if not os.path.isfile(src):
                return
            os.makedirs(snap, exist_ok=True)
            shutil.copy2(src, os.path.join(snap, "config.yaml"))
        except OSError:
            pass

    def reset_steps_from(self, n: int) -> list[str]:
        if n == 18:
            from tools.attr_stability.export_state import reset_guard
            with reset_guard(self.paths.cat_root):
                return self._reset_steps_from(n)
        return self._reset_steps_from(n)

    def _reset_steps_from(self, n: int) -> list[str]:
        """重置步骤 n..20(含全部下游): 删除对应产物与独立目录 + 重置状态。

        仅影响 n..20; 上游(0..n-1)数据与状态保留。
        返回被删除的文件/目录清单。监控目录的 step-status/duration/log 保留(仅清状态行);
        monitor-alerts.txt 同样保留但追加 [RESET] 边界行——历史预警不删、
        不随新运行重推企业微信(只推重置之后的新预警, 见 notify.push_alerts)。
        """
        require_step(n)
        removed: list[str] = []
        steps = {s.number: s for s in get_steps()}
        cat_root = Path(self.paths.cat_root).resolve()

        def local_reset_path(name):
            # 重置只能删除本运行路径，不能使用会继承共享文件的读取解析器。
            path = cat_root / name
            if not path.resolve().is_relative_to(cat_root):
                raise ValueError("重置路径超出本运行目录: %s" % path)
            return str(path)

        if n <= 16:
            from tools.attr_stability.spec_io import reset_attr_spec
            removed.extend(reset_attr_spec(cat_root))
        for num in PIPELINE_STEP_NUMBERS:
            if num < n:
                continue
            step = steps.get(num)
            if step is None:
                continue
            step.bind_cat(self.ctx)
            for artifact in step.artifacts:
                path = local_reset_path(artifact)
                if os.path.exists(path):
                    os.remove(path)
                    removed.append(path)
            # ★ 2026-10-14 修: 原来只删 `artifacts` + `archive_dirs`, **漏了 `extra_archive_files`**
            #   → 那批文件会跨轮残留(实测 `reset-step 17` 后 input.json / 视觉补充.json /
            #   runs/a1,a2.json / 18_标准化/词表聚类报告.json / 交付值校验.json /
            #   规则执行与输出校验.json / 跨属性冲突.json 全部存活)。危害: 下一轮会把**上一轮**的
            #   词表聚类报告当成"当前状态"读(步骤17 的增量判定、Web 面板、步骤18 都读它)。
            #   监控步骤(20)的 extra 是历史台账, 与 `20_备份与监控` 目录同样**故意保留**。
            if num != MONITOR_STEP and not (n == 18 and num == 18):
                for extra in step.extra_archive_files:
                    path = local_reset_path(extra)
                    if os.path.isfile(path):
                        os.remove(path)
                        removed.append(path)
            for subdir in step.archive_dirs:
                if n == 18 and num == 18 and subdir == "18_标准化/版本数据":
                    continue  # Preserve the complete review history and current version.
                if num == MONITOR_STEP and subdir == "20_备份与监控":
                    continue
                path = local_reset_path(subdir)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(path)
            if n == 18 and num == 18:
                # The preserved version must be explicitly exported again.
                for name in ("export.json", "export-operation.json"):
                    path = local_reset_path("18_标准化/版本数据/" + name)
                    if os.path.isfile(path):
                        os.remove(path)
                        removed.append(path)
        # ★ 2026-10-22 用户口径 B: 重置步骤时**同步清理归档里的对应步骤目录**。
        #   背景: 归档是"每步完成后 copy2 复制"出来的副本(见 core/archive.sync_step_files)。
        #   只清当前目录会让归档变成**陈旧镜像** —— 用户重置后卡片里仍显示文件(实测报障)。
        #   现在把归档快照里 n..20 的步骤目录一并删掉, 语义变成"重置 = 当前 + 归档一起清"。
        #   `MONITOR_STEP`(20/20_备份与监控) 仍然保留: 它是历史台账, 与重置前的预警同样不该丢。
        snap = self.paths.latest_snapshot()
        if snap and os.path.isdir(snap):
            from core.paths import STEP_DIR_NAMES
            for num in PIPELINE_STEP_NUMBERS:
                if num < n or num == MONITOR_STEP:
                    continue
                arch_dir = os.path.join(snap, STEP_DIR_NAMES.get(num, ""))
                if arch_dir and os.path.isdir(arch_dir):
                    if n == 18 and num == 18:
                        # Do not delete the archived versions/jobs when removing
                        # the stale workbook and export confirmation.
                        archive_files = [os.path.join(arch_dir, os.path.basename(a)) for a in steps[18].artifacts]
                        archive_files += [os.path.join(arch_dir, "版本数据", name)
                                          for name in ("export.json", "export-operation.json")]
                        for path in archive_files:
                            if os.path.isfile(path):
                                os.remove(path)
                                removed.append(path)
                        continue
                    shutil.rmtree(arch_dir, ignore_errors=True)
                    removed.append(arch_dir)
        status = self.paths.monitor_file("step-status.txt")
        if os.path.exists(status):
            kept = []
            for line in open(status, encoding="utf-8-sig"):
                line = line.strip()
                if not line:
                    continue
                try:
                    num = int(line.split("|")[0])
                except ValueError:
                    continue
                if num < n:
                    kept.append(line)
            with open(status, "w", encoding="utf-8") as fh:
                fh.write("\n".join(kept) + "\n")
        # 历史预警保留, 但写重置边界: 之后 push_alerts 只推重置后的新预警
        alerts = self.paths.monitor_file("monitor-alerts.txt")
        notify.append_alert(
            alerts, notify.RESET, f"步骤 {n}-{MONITOR_STEP} 已重置, 重置前的历史预警保留不推送"
        )
        self.log.info(f"已重置步骤 {n}-{MONITOR_STEP}, 删除 {len(removed)} 个产物")
        for p in removed:
            self.log.info(f"  删除: {p}")
        return removed

    def _precheck_login(self, steps: Iterable[int]) -> None:
        """执行前登录态预检查 —— **只检查将要执行的步骤真正依赖的那部分**。

        `steps` = 本轮将要执行的已注册步骤号集合(全流程/续跑包含最终体检),
        单步运行传 [num])。依赖表见模块级 `COOKIE_STEPS` /
        `AMAZON_LOGIN_STEPS` / `SELLERSPRITE_LOGIN_STEPS`:
        - 亚马逊 COOKIE : 步骤 1/10(纯 HTTP 直连)
        - 亚马逊登录态  : 步骤 0/1/9/10(amazon 浏览器 Profile)
        - 卖家精灵登录态: 步骤 0/11/12/13(sellersprite 浏览器 Profile)
        - 其余步骤(2-8/14-19: LLM 与纯本地计算、5/6 走 MCP 接口)一律**直接返回**,
          连日志都不打 —— 它们不弹浏览器、不读 COOKIE, 检查它们只会白起浏览器,
          并可能因无关的登录态失效把本地步骤拦死(2026-09-29 单步跑步骤8 实例)。

        复用步骤 0 的登录检查链: 亚马逊(Profile会话/COOKIE导入/手动兜底)
        + 卖家精灵(Profile会话/账号自动登录/手动兜底) + COOKIE有效性(HTTP直连),
        任一失败抛异常阻断。
        """
        if self.config.dry_run:
            return
        todo = {int(n) for n in steps}
        need_cookie = bool(todo & COOKIE_STEPS)
        need_amazon = bool(todo & AMAZON_LOGIN_STEPS)
        need_ss = bool(todo & SELLERSPRITE_LOGIN_STEPS)
        if not (need_cookie or need_amazon or need_ss):
            # 纯本地/纯接口步骤: 不做任何登录检查, 也不打日志(避免误导为"检查过了")
            return
        wanted = [name for name, need in (("COOKIE", need_cookie),
                                         ("亚马逊", need_amazon),
                                         ("卖家精灵", need_ss)) if need]
        self.log.info("登录态预检查: " + " + ".join(wanted))
        if need_cookie:
            self._precheck_cookie()
        if need_amazon or need_ss:
            from steps.step00_bestsellers import Step00Bestsellers
            s0 = Step00Bestsellers()
            if need_amazon:
                s0._allow_captcha_passthrough = bool(need_cookie and todo.issubset(COOKIE_STEPS))
                s0.ensure_amazon_login(self.ctx)
            if need_ss:
                self.log.info("开始检查卖家精灵浏览器和登录态")
                try:
                    s0.ensure_sellersprite_login(self.ctx)
                except Exception as exc:
                    self.log.error("卖家精灵登录态预检查失败: %s: %s", type(exc).__name__, exc)
                    raise
        self.log.info("登录态预检查通过")

    def _precheck_cookie(self) -> None:
        """COOKIE 有效性检查(HTTP 直连步骤 1/10 前置, 最先执行)。

        **行为写死(2026-10-08 用户定调)**: 检查照做、失败**放行**, 把"抓得到抓不到"交给兜底链
        `http 直连 → 无头浏览器补抓 → 有头浏览器补抓`。用户原话: "我觉得需要 COOKIE 检查需要放行,
        让系统自己兜底处理, http 直连 + 无头浏览器 + 有头浏览器"。因此**没有开关**:
        `COOKIE_CHECK` / `COOKIE_CHECK_BLOCK` 两个历史键已废弃(配置里写了会被忽略并打一行提示)。

        - 未配置 COOKIE  -> 跳过检查(保持匿名访问现状)
        - 网络异常/http_0 -> 仅告警不阻断(可能是瞬时网络抖动, 由步骤重试兜底)
        - 无法判定页      -> 仅告警不阻断(留人工抽查)
        - 失效/被风控     -> **自动定性 + CRIT 告警 + 企业微信, 然后放行本轮**(不抛异常)
        """
        for legacy in ("COOKIE_CHECK", "COOKIE_CHECK_BLOCK"):
            if legacy in getattr(self.config, "data", {}):
                self.log.warning(
                    f"配置键 {legacy} 已废弃(2026-10-08 起 COOKIE 预检查固定为"
                    f"『检查 + 定性 + 放行, 交给兜底链』, 无开关) —— 该键被忽略, 请从配置里删除"
                )
        from core.cookie_check import check_cookie
        result = check_cookie(self.config)
        status = result.get("status", "")
        if status == "ok":
            self.log.info(f"COOKIE 检查通过: {result.get('message', '')}")
            return
        if status == "no_cookie":
            self.log.info(f"COOKIE 未配置({result.get('message', '')}), 跳过检查, HTTP 直连将匿名访问")
            return
        if status == "network":
            self.log.warning(f"COOKIE 检查网络异常(不阻断): {result.get('message', '')}")
            return
        if status == "unrecognized":
            # 2026-09-13: 探测页无法识别(既无商品标题也无登录/风控标记) → 既不能当"通过", 也不该
            # 阻断整轮(与 network 同口径): 记警告 + 告警, 继续运行, 留人工抽查。
            self.log.warning(f"COOKIE 检查无法判定(不阻断, 建议人工抽查): {result.get('message', '')}")
            notify.append_alert(self.paths.monitor_file("monitor-alerts.txt"), notify.WARN,
                                f"COOKIE 检查无法判定(不阻断): {result.get('message', '')}")
            return
        msg = (f"COOKIE 检查失败({status}): {result.get('message', '')}"
               f"; 预检查不阻断(固定放行) —— 本轮直连大概率仍会被拦, "
               f"已交由兜底链处理: 直连 → 无头浏览器补抓 → 有头浏览器补抓")
        # 自动定性(2026-09-29): 只多打 2~3 个请求, 就能把"到底是 COOKIE 失效还是本机 IP/客户端形态被标记"
        # 直接写进日志与告警 —— 此前用户为了分辨这个要手工做"带/不带 COOKIE + 多站点对照", 来回折腾半小时。
        try:
            from core.cookie_check import diagnose_cookie
            diag = diagnose_cookie(self.config, result)
            for probe in diag.get("probes") or []:
                self.log.info(
                    f"  定性探测: {probe['site']:<14} "
                    f"{'带COOKIE' if probe['with_cookie'] else '匿名':<8} "
                    f"class={probe['status']:<12} kind={probe['kind']:<9} len={probe['len']}"
                )
            self.log.error(f"COOKIE 失败定性[{diag.get('verdict')}]: {diag.get('message')}")
            msg += f"; 定性结论: {diag.get('message')}"
        except Exception as exc:                       # noqa: BLE001 — 定性失败不得影响主流程
            self.log.warning(f"COOKIE 失败定性不可用(不影响放行): {type(exc).__name__}: {exc}")
        # 放行: 照样 CRIT 告警 + 企业微信(问题是真的, 该看见), 但**不阻断**本轮 —— 交给兜底链
        self.log.warning(msg)
        notify.append_alert(self.paths.monitor_file("monitor-alerts.txt"), notify.CRIT, msg)
        notify.push_wecom(self.config.get("WECOM_WEBHOOK", ""), notify.with_context(msg, self.config))
        self.log.warning("COOKIE 预检查放行: 继续本轮; 直连失败将由 FALLBACK=auto 的浏览器兜底接管"
                         "(无头补抓 → 有头补抓)")

    def _recheck_done_steps(self, start: int) -> int:
        """断点保护增强: 校验 0..start-1 已 DONE 步骤的产物质量。

        产物存在但质量不合格(如步骤5 全 error 记录)的步骤重置为 PENDING,
        返回新的最小续跑起点。
        """
        steps = {s.number: s for s in get_steps()}
        min_start = start
        for num in range(0, start):
            if not self.state.is_finished(num):
                continue
            step = steps.get(num)
            if step is None:
                continue
            step.bind_cat(self.ctx)
            try:
                step.validate_artifacts(self.ctx)
            except Exception as exc:
                self.log.warning(f"步骤 {num} 已 DONE 但产物质量不合格({exc}), 重置为待执行")
                self.state.mark_pending(num)
                min_start = min(min_start, num)
        return min_start

    def _backfill_synced_steps(self, start: int) -> None:
        """断点续跑/单步运行时, 把 0..start-1 已完成步骤的产物补同步进本轮归档。

        修复"归档缺早期步骤": 旧逻辑只在步骤实际执行后同步, 若本轮归档是
        从步骤 N 续跑时复用的最新目录, 早期已完成步骤(0..N-1)的产物不会
        进入该轮归档, 导致快照不完整。此处对已 DONE 步骤补一次同步(幂等,
        SKIP 无产物自动跳过)。
        """
        if not getattr(self, "_snapshot", None):
            return
        steps = {s.number: s for s in get_steps()}
        for num in range(0, start):
            if not self.state.is_finished(num):
                continue
            step = steps.get(num)
            if step is None:
                continue
            self._sync_after_step(step)
            self.log.info(f"补同步归档: 步骤 {num:02d} {step.title}")

    def run(self, from_step: int = 0, *, skip_finished: bool = False) -> int:
        """执行流水线, 返回退出码: 0=正常 1=有警告 2=严重。

        开始/结束/中断时间均记入进度日志: 本轮开始(流水线启动行) → 每步开始
        (=== 步骤 NN 开始 ===) → 每步完成/失败(结束时间, 行首时间戳);
        上次中断的步骤在启动时单独报告(中断时间), 与完成的结束时间明确区分;
        Ctrl+C 中断时把进行中步骤标记 INTERRUPTED(中断时间), 重跑自动续。
        """
        require_step(from_step)
        self.snapshot_dir()
        self.log.info(f"流水线启动: CAT={self.config.cat} dry_run={self.config.dry_run} from_step={from_step}")
        self._log_price_source()
        _t0 = datetime.now()
        self._report_interruptions()
        start = self.state.first_unfinished(from_step)
        if start is not None and start >= 9 and self._keyword_confirmation_pending():
            self.log.info("⏸ 步骤 8 已完成，请在关键词清单中确认核心大词和长尾词后继续。")
            return 3
        if start is not None and not skip_finished:
            start = self._recheck_done_steps(start)
            next_start = self.state.first_unfinished(start)
            if next_start is not None:
                start = next_start
        if start is None:
            self.log.info("所有步骤已完成, 无需执行")
            return 0
        gate = self._blocking_manual_gate(start)
        if gate:
            self.log.info("⏸ 等待人工处理: %s", gate['message'])
            return 3
        if start < 14 and (start > from_step or from_step > 0):
            if start > from_step:
                self.log.info(f"断点续跑: 步骤 {from_step}-{start - 1} 已完成, 从步骤 {start} 开始")
            else:
                self.log.info(f"从步骤 {start} 开始执行")
        steps = {s.number: s for s in get_steps()}
        self._backfill_synced_steps(start)
        try:
            for num in PIPELINE_STEP_NUMBERS:
                if num < start:
                    continue
                if num >= 9 and self._keyword_confirmation_pending():
                    self._log_run_summary("⏸ 步骤 8 完成，请确认关键词清单后继续")
                    return 3
                if skip_finished and self.state.is_finished(num):
                    if num == 13:
                        self._release_ss_workflow()
                    continue
                gate = self._blocking_manual_gate(num)
                if gate:
                    self.log.info("⏸ 等待人工处理: %s", gate['message'])
                    return 3
                step = steps.get(num)
                if step is None:
                    self.log.error(f"步骤 {num} 未注册")
                    return 2
                if num in (11, 12, 13):
                    self._hold_ss_workflow()
                # 步骤 0 自己完成两站登录检查；其余步骤只在实际执行前检查自身依赖。
                # 避免步骤 11 的卖家精灵问题阻断与之无关的步骤 9/10。
                if num != 0 and not self._precheck_step_login(num):
                    if getattr(self, '_login_waiting_for_admin', False):
                        self._log_run_summary(f"步骤 {num:02d} 等待管理员重新登录亚马逊")
                        return 3
                    self._log_run_summary(f"步骤 {num:02d} 执行前登录检查失败")
                    return 2
                code = self._run_one(step)
                if num == 13 and code == 0:
                    self._release_ss_workflow()
                if code == 3:
                    # ★ 等待人工确认（方案 A 的两个停点）：不是失败，正常"暂停"。
                    #   退出码 3 与 2（失败）区分，供页面/脚本判断"该去确认了"。
                    self._log_run_summary(
                        f"⏸ 流水线暂停(等待人工确认): 卡在步骤 {num:02d} {step.title}；"
                        f"确认后重跑本命令即从该步续跑")
                    return 3
                if code != 0:
                    self._log_run_summary(f"提前终止(步骤 {num:02d} {step.title} 失败/产物缺失)")
                    return code
        except KeyboardInterrupt:
            self._mark_current_interrupted()
            self._log_run_summary("被中断(Ctrl+C), 中断步骤已标记 INTERRUPTED")
            raise
        finally:
            self._release_ss_workflow()
        self._log_run_summary("全部步骤完成")
        # 耗时口径(2026-09-15 统一为"累计执行时长") : 必须与 webui 看板一致, 否则
        # "中断→续跑→再中断→再续跑"分多段跑完时, 只报本进程段会严重少报
        # (实例 2026-09-15: 同一轮分 3 段跑完 18 步, 单段口径 4h33m / 累计 12h51m)。
        self.log.info(finish_line(self.state, datetime.now() - _t0, extra=self._usage_suffix()))
        return 0

    def _log_price_source(self) -> None:
        """启动时报告价格来源(2026-09-28): 费用是照哪份价表算的必须一眼可见。

        抓取失败会回退到缓存/内置快照 —— 用户看到"内置快照"就知道费用只是估算、
        该去核对官网价了, 而不会把过期价当成实时价。
        """
        try:
            meter = self.ctx.usage()
            if not meter.enabled:
                self.log.info("LLM 用量监控: dry-run 模式, 本次不记账")
            else:
                self.log.info(f"LLM 用量监控: 价格来源 = {meter.prices.source}")
        except Exception as exc:  # noqa: BLE001 — 监控初始化失败不得阻断选品流程
            self.log.warning(f"LLM 价格表初始化失败(不阻断): {type(exc).__name__}: {exc}")

    def _log_step_usage(self, num: int, status: str) -> None:
        """某步结束时的用量一行。失败/跳过的步骤也走这里 ——

        失败前往往已经调用过 LLM(烧了 token), 不记就会系统性漏掉"失败重跑的成本"。
        """
        try:
            text = self.ctx.finish_step(num, status)
            if text:
                self.log.info(text)
        except Exception as exc:  # noqa: BLE001 — 记账失败不得影响步骤状态判定
            self.log.warning(f"步骤 {num:02d} 用量记账失败: {type(exc).__name__}: {exc}")

    def _write_usage_summary(self) -> None:
        """把本轮 LLM 用量/费用总账追加进明细文件(按 run_id 可直读)。"""
        try:
            self.ctx.usage().write_run_summary()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"LLM 用量总账写入失败: {type(exc).__name__}: {exc}")

    def _sync_monitor_to_snapshot(self) -> None:
        """收尾再同步一次监控目录(2026-09-28)。

        账本的 `run-summary` 是在步骤19 的归档同步**之后**才追加的 —— 不补这一下,
        归档里的 `llm-usage.jsonl` 会缺最后一行(归档是交付物, 应自包含)。
        """
        if not getattr(self, "_snapshot", None):
            return
        try:
            sync_step_dir(MONITOR_STEP, self.paths.monitor_dir(), self._snapshot, self.paths)
        except Exception as exc:  # noqa: BLE001 — 补同步失败不影响流程结论
            self.log.warning(f"监控目录补同步失败(不阻断): {type(exc).__name__}: {exc}")

    def _usage_suffix(self) -> str:
        """完成行尾部的 LLM 用量小结; 无账或读不到时返回空串(不编造)。

        取数走**账本文件按 run_id 聚合**而不是内存 meter: 一轮分多段跑完时,
        内存里只有本段, 文件里才是整轮的账(与体检第 7 项同源)。
        """
        try:
            meter = self.ctx.usage()
            if not meter.enabled or not meter.jsonl_path:
                return ""
            agg = summarize_run(load_records(meter.jsonl_path), meter.run_id)
            t = agg["totals"]
            if not t["calls"]:
                return ""
            return (f" | tokens {t['total_tokens']} "
                    f"费用估算 {t['cost_cny']:.4f} 元")
        except Exception:  # noqa: BLE001 — 监控不得影响完成行
            return ""

    def _report_interruptions(self) -> None:
        """启动时报告上次中断/强杀残留的步骤, 同时写入监控与告警(monitor-alerts.txt)。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        titles = {s.number: s.title for s in get_steps()}
        alerts = self.paths.monitor_file("monitor-alerts.txt")
        for it in self.state.interruptions():
            num = it["number"]
            title = titles.get(num, "")
            if it["kind"] == "stale_running":
                msg = (f"步骤 {num:02d} {title} 上次运行被进程强杀/终止(开始于 {it['start'] or '未知'}, "
                       f"无正常结束记录, 中断≈{now}), 本轮将重新执行该步骤")
                self.log.warning(msg)
                notify.append_alert(alerts, notify.WARN, msg)
            else:
                msg = (f"步骤 {num:02d} {title} 上次中断于 {it['end']}(开始于 {it['start']}; "
                       f"{it.get('reason') or '被中断'}), 本轮将重新执行该步骤")
                self.log.warning(msg)
                notify.append_alert(alerts, notify.WARN, msg)

    def _mark_current_interrupted(self) -> None:
        """Ctrl+C 等中断: 把进行中(RUNNING)步骤标记 INTERRUPTED, 结束时间=中断时间;
        同时写入监控与告警。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        titles = {s.number: s.title for s in get_steps()}
        alerts = self.paths.monitor_file("monitor-alerts.txt")
        # 步骤号从注册表派生(不再写死 range(20)): 新增步骤(如步骤19 VOC)自动纳入中断标记
        for num in sorted(titles):
            if self.state.status_of(num) == "RUNNING":
                self.state.mark_interrupted(num, f"运行被中断(Ctrl+C), 中断于 {now}")
                # 中断前的 LLM 用量也要落账(2026-09-29): 被中断的步骤同样烧了 token,
                # 不写 step-summary 就会被"只认汇总"的旧口径整段丢掉。被强杀时这里跑不到,
                # 由账本里即时落盘的 call 记录兜底(见 core/usage.py::summarize_category)。
                self._log_step_usage(num, "INTERRUPTED")
                msg = (f"步骤 {num:02d} {titles.get(num, '')} 已被标记 INTERRUPTED(中断时间 {now}); "
                       f"重新运行将从该步骤自动续跑")
                self.log.warning(msg)
                notify.append_alert(alerts, notify.WARN, msg)
                return
        msg = f"流程被中断(中断时间 {now}), 无进行中的步骤"
        self.log.warning(msg)
        notify.append_alert(alerts, notify.WARN, msg)

    def _step_duration_text(self, num: int) -> str:
        """步骤活动耗时文本: 墙钟耗时 - 等待累计(备用窗口等挂起不计入), 统一格式;
        无法计算返回 '-'. """
        wall = self.state.duration_seconds(num)
        if wall is None:
            return "-"
        wait = self.ctx.step_wait_seconds()
        exec_sec = max(0.0, wall - wait)
        return format_duration(exec_sec)

    def _run_one(self, step: BaseStep) -> int:
        num = step.number
        # 强杀残留(RUNNING 无结束): 以日志中该步最后活动时间近似中断时刻, 供重跑并入累计
        self.state.start_step(num, end_hint=self._stale_end_hint(num))
        self.ctx.reset_step_wait()  # 每步等待计数从 0 开始(等待不计入步骤耗时)
        self.ctx.begin_step(num)    # 每步用量计数从 0 开始
        self.log.info(f"=== 步骤 {num:02d} {step.title} 开始 ===")
        if step.skip_when(self.ctx):
            self.state.mark_skip(num)
            self.log.info(f"步骤 {num:02d} 跳过(SKIP)(耗时 {self._step_duration_text(num)})")
            self._log_step_usage(num, "SKIP")
            self._sync_after_step(step)
            return 0
        try:
            step.validate_upstream(self.ctx)
            step.run(self.ctx)
        except WaitingForHuman as exc:
            # ★★ 「等待人工确认」不是失败（2026-10-09，方案 A 的两个停点）：
            #   16→17 的属性集确认、17→18 的词表确认都走这里。
            #   关键区别：**不标 FAILED、不发 CRIT 告警、不弹窗** ——
            #   当成失败会让人去查一个根本不存在的故障。
            #   步骤状态留在 PENDING（= 未完成）→ 断点续跑语义天然正确：
            #   人工确认后重跑，就从这一步接着跑。
            self.state.mark_pending(num)
            msg = f"⏸ 等待人工确认: 步骤 {num:02d} {step.title} —— {exc}"
            self.log.info(msg)
            if isinstance(exc, EmptyExportPause):
                notify.append_alert(self.paths.monitor_file('monitor-alerts.txt'), notify.CRIT, msg)
                sent = notify.push_wecom(self.config.get('WECOM_WEBHOOK', ''), notify.with_context(msg, self.config))
                if not sent:
                    self.log.warning('企业微信提醒未发送成功：机器人未配置、网络异常或企业微信返回错误')
            elif num in (8, 15, 16, 18):
                confirm_msg = (
                    f"步骤 {num:02d} {step.title} 等待人工确认: "
                    f"{exc}(耗时 {self._step_duration_text(num)})"
                )
                self._push_manual_confirmation_wecom(confirm_msg)
            self._log_step_usage(num, "WAITING")
            return 3
        except Exception as exc:
            detail = str(exc).strip() or f"{type(exc).__name__}(异常无错误文本)"
            self.state.mark_failed(num, detail[:300])
            msg = f"步骤 {num:02d} {step.title} 失败: {detail}(耗时 {self._step_duration_text(num)})"
            self.log.error(msg)
            self._dump_step_traceback(num, step.title, exc)
            self._log_step_usage(num, "FAILED")
            notify.append_alert(self.paths.monitor_file("monitor-alerts.txt"), notify.CRIT, msg)
            notify.push_wecom(self.config.get("WECOM_WEBHOOK", ""), notify.with_context(msg, self.config))
            notify.popup_critical(msg)
            return 2
        missing = step.missing_artifacts(self.ctx)
        if missing:
            self.state.mark_failed(num, f"产物缺失: {missing}")
            msg = f"步骤 {num:02d} 产物未生成(禁止标成功): {missing}(耗时 {self._step_duration_text(num)})"
            self.log.error(msg)
            self._log_step_usage(num, "FAILED")
            notify.append_alert(self.paths.monitor_file("monitor-alerts.txt"), notify.CRIT, msg)
            return 2
        if num == 8 and not getattr(self, "_refreshing_keywords", False):
            try:
                from core import keyword_confirmation as kc
                kc.pending(self.paths.cat_root, kc.current(self.config.cat, self.paths.data_root))
            except Exception as exc:
                self.state.mark_failed(num, f"无法创建关键词确认停点: {exc}"[:300])
                self.log.error("无法创建关键词确认停点，步骤 8 未完成: %s", exc)
                return 2
        self.state.mark_done(num)
        dur_text = self._step_duration_text(num)
        self.log.info(f"步骤 {num:02d} 完成(DONE)(耗时 {dur_text})")
        if num == 13 and not self.config.dry_run:
            try:
                from core.ss_auto_close import request_close
                request_close(str(self.config.get('SS_CDP_URL', '') or ''), self.log.info)
            except Exception as exc:
                self.log.warning('步骤13完成，关闭浏览器时发生异常（不影响产物）: %s', type(exc).__name__)
        self._log_step_usage(num, "DONE")
        self._sync_after_step(step)
        if num == 8 and not getattr(self, "_refreshing_keywords", False):
            self.log.info("⏸ 步骤 8 已完成，请核对关键词清单并确认后继续。")
        confirm_msg = self._manual_confirmation_message(step, dur_text)
        if confirm_msg:
            self._push_manual_confirmation_wecom(confirm_msg)
        return 0

    def _manual_confirmation_message(self, step: BaseStep, dur_text: str) -> str | None:
        """步骤完成后若进入人工确认停点(步骤8/15/16/18), 返回待推送正文; 否则返回 None。"""
        num = step.number
        root = Path(self.paths.cat_root)
        if num == 8:
            if getattr(self, "_refreshing_keywords", False):
                return None
            if self._keyword_confirmation_pending():
                return (
                    f"步骤 {num:02d} {step.title} 等待人工确认: "
                    f"请核对并确认关键词清单后继续。(耗时 {dur_text})"
                )
            return None
        if num == 15:
            try:
                from core.step15_review import pending as report_pending
                is_pending = bool(report_pending(root))
            except Exception:
                is_pending = True
            if is_pending:
                return (
                    f"步骤 {num:02d} {step.title} 等待人工确认: "
                    f"请查看步骤15选品指标及AI建议，确认后继续。(耗时 {dur_text})"
                )
            return None
        if num == 16:
            try:
                from core.manual_gates import _load
                from tools.attr_stability.catalog import resolve_spec_path
                spec = resolve_spec_path(root.parent, root.name)
                confirmed = _load(spec).get("status") == "confirmed"
            except Exception:
                confirmed = False
            if not confirmed:
                return (
                    f"步骤 {num:02d} {step.title} 等待人工确认: "
                    f"请核对并确认步骤16属性集后继续。(耗时 {dur_text})"
                )
            return None
        if num == 17:
            try:
                from core.manual_gates import _export_confirmed
                from tools.attr_stability.catalog import resolve_spec_path
                spec = resolve_spec_path(root.parent, root.name)
                confirmed = bool(_export_confirmed(root, spec))
            except Exception:
                confirmed = False
            if not confirmed:
                return (
                    f"步骤 18 标准化 等待人工确认: "
                    f"步骤 {num:02d} {step.title}已完成，请确认当前标准化结果并导出交付后继续。(耗时 {dur_text})"
                )
            return None
        return None

    def _push_manual_confirmation_wecom(self, msg: str) -> bool:
        """向企业微信机器人推送人工确认提醒(失败/未配置不阻断流程)。"""
        webhook = str(self.config.get("WECOM_WEBHOOK", "") or "").strip()
        if not webhook:
            return False
        try:
            payload = notify.with_context(msg, self.config, include_run_id=False)
        except TypeError:
            payload = notify.with_context(msg, self.config)
        sent = notify.push_wecom(webhook, payload)
        if sent:
            self.log.info(f"已推送企业微信人工确认提醒: {msg}")
        else:
            self.log.warning("企业微信人工确认提醒未发送成功：网络异常或企业微信返回错误")
        return sent

    def _dump_step_traceback(self, num: int, title: str, exc: Exception) -> None:
        """步骤未捕获异常时落盘完整堆栈到日志与 20_备份与监控/last-error.log，避免空错误文本无法定位。"""
        import traceback
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        if not tb_text:
            return
        self.log.error("步骤 %02d %s 异常堆栈:\n%s", num, title, tb_text)
        try:
            err_file = Path(self.paths.monitor_file("last-error.log"))
            err_file.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = f"\n=== [{ts}] 步骤 {num:02d} {title} | {type(exc).__name__}: {exc} ===\n"
            with open(err_file, "a", encoding="utf-8") as fh:
                fh.write(header + tb_text + "\n")
        except Exception:
            pass

    def _precheck_step_login(self, num: int) -> bool:
        """检查单步依赖；失败时保留步骤状态并将原因写入持久日志。"""
        self._login_waiting_for_admin = False
        try:
            self._precheck_login([num])
            return True
        except WaitingForHuman as exc:
            self._login_waiting_for_admin = True
            self.log.info("步骤 %02d 等待人工处理: %s", num, exc)
            return False
        except Exception as exc:
            msg = (f"步骤 {num:02d} 执行前登录检查失败: "
                   f"{type(exc).__name__}: {exc}；该步骤尚未开始，请处理后续跑")
            self.log.exception(msg)
            notify.append_alert(self.paths.monitor_file("monitor-alerts.txt"), notify.WARN, msg)
            return False

    def _keyword_confirmation_pending(self) -> bool:
        from core import keyword_confirmation as kc
        gate = kc.read(self.paths.cat_root)
        if gate is None and self.state.status_of(8) == "DONE" and not self.state.is_finished(9):
            gate = kc.pending(self.paths.cat_root, kc.current(self.config.cat, self.paths.data_root))
        elif gate and gate.get("status") == "confirmed" and gate.get("confirmed_words"):
            current = kc.current(self.config.cat, self.paths.data_root)
            if kc.revision(current) != gate.get("confirmed_revision"):
                gate = kc.pending(self.paths.cat_root, gate["confirmed_words"])
        return bool(gate and gate.get("status") == "pending")

    def _blocking_manual_gate(self, target: int):
        from core.manual_gates import blocking_gate
        statuses = {n: self.state.status_of(n) for n in PIPELINE_STEP_NUMBERS}
        try:
            return blocking_gate(self.paths.cat_root, statuses, target)
        except Exception as exc:
            return {'kind':'error','message':f'人工确认状态无法核验：{exc}'}

    def confirm_keywords_and_resume(self, expected_revision: str) -> int:
        """确认当前词表，必要时更新 5–8，再只续跑未完成的后续步骤。"""
        from core import keyword_confirmation as kc
        gate = kc.read(self.paths.cat_root)
        if not gate or gate.get("status") != "pending":
            self.log.error("当前没有待确认的关键词清单，请刷新页面。")
            return 2
        current = kc.current(self.config.cat, self.paths.data_root)
        if current["stopped"] or kc.revision(current) != expected_revision:
            self.log.error("关键词清单已变化或处于人工停止状态，请刷新后重新确认。")
            return 3
        baseline = gate.get("baseline") or {}
        old = {str(w).strip().casefold() for w in baseline.get("core", [])}
        added = [w for w in current["core"] if str(w).strip().casefold() not in old]
        missing_metrics = kc.missing_metrics(self.paths.cat_root, self.config.cat, current["core"])
        changed = kc.revision(current) != kc.revision(baseline)
        refresh = (5, 6, 7, 8) if added or missing_metrics else ((8,) if changed or kc.report_outdated(self.paths.cat_root, self.config.cat) else ())
        if added or missing_metrics:
            self.log.info("新增或尚未挖词的核心词：%s；依次更新步骤 5–8。", ", ".join(dict.fromkeys(added + missing_metrics)))
        elif refresh:
            self.log.info("关键词清单已变更，更新步骤 8 报告。")
        self._refreshing_keywords = True
        try:
            for num in refresh:
                code = self.run_single(num)
                if code:
                    return code
        finally:
            self._refreshing_keywords = False
        after = kc.current(self.config.cat, self.paths.data_root)
        if kc.revision(after) != expected_revision:
            self.log.info("更新后生效关键词发生变化，请重新核对并确认。")
            return 3
        gate.update(status="confirmed", confirmed_revision=expected_revision,
                    confirmed_words=after,
                    confirmed_at=datetime.now().isoformat(timespec="seconds"))
        kc.write(self.paths.cat_root, gate)
        completed = [str(n) for n in PIPELINE_STEP_NUMBERS if n >= 9 and self.state.is_finished(n)]
        if changed and completed:
            self.log.warning("后续已完成步骤 %s 将保留原结果，可能仍使用旧关键词；如需更新请另行重跑。", ", ".join(completed))
        self.log.info("关键词清单已确认；继续运行步骤 9 之后尚未完成的步骤。")
        return self.run(from_step=9, skip_finished=True)

    def _stale_end_hint(self, num: int) -> str:
        """强杀残留步骤(RUNNING 无结束): 从 pipeline-progress.log 取该步**真实执行活动**
        的最后时间, 作为近似中断时刻(缺省由 state 保守按 start 计, 不加闲置段)。

        只认步骤的真实执行日志(与 webui 展示口径一致):
        - `[StepNN...]` 步骤类日志
        - `=== 步骤 NN ... 开始 ===` / 步骤结束行(完成/失败/跳过)
        排除"上次运行被进程强杀/终止""断点续跑: …从步骤 NN 开始"等**编排报告行**
        (2026-09 修复: 宽匹配会把编排告警行时间(本轮启动时刻)当成中断时刻,
        把上次 start → 本轮启动 之间未运行的闲置时段误并入 acc, 虚增累计时长)。
        """
        entry = self.state.steps.get(num, {})
        if entry.get("status") != "RUNNING" or entry.get("end"):
            return ""
        log = self.paths.monitor_file("pipeline-progress.log")
        if not os.path.exists(log):
            return ""
        try:
            size = os.path.getsize(log)
            off = max(0, size - 2_000_000)
            with open(log, "rb") as fh:
                if off:
                    fh.seek(off)
                    fh.readline()
                data = fh.read()
        except OSError:
            return ""
        text = data.decode("utf-8", errors="replace")
        best = ""
        for ln in text.splitlines():
            if not _is_step_real_activity(ln, num):
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
            if m:
                best = m.group(1)
        return best

    def _log_run_summary(self, note: str) -> None:
        """收尾运行小结: 20 步状态分组打印, 失败/中断步骤单列并给出重跑命令。

        机制可见性: 一眼看出本轮哪些步骤 完成/跳过/失败/中断/未执行,
        以及"单步重跑 / run 续跑"该怎么敲。
        """
        titles = {s.number: s.title for s in get_steps()}
        groups: dict[str, list[int]] = {}
        # 同上: 从注册表派生步骤号, 新增步骤自动进入状态分组统计
        for num in sorted(titles):
            st = self.state.status_of(num)
            groups.setdefault(st, []).append(num)
        parts = []
        for st in ("DONE", "SKIP", "FAILED", "INTERRUPTED", "RUNNING", "PENDING"):
            nums = groups.get(st)
            if nums:
                parts.append(f"{st}{len(nums)}: " + ",".join(f"{n:02d}" for n in nums))
        for st, nums in groups.items():
            if st not in ("DONE", "SKIP", "FAILED", "INTERRUPTED", "RUNNING", "PENDING"):
                parts.append(f"{st}{len(nums)}: " + ",".join(f"{n:02d}" for n in nums))
        self.log.info(f"运行小结[{note}]: " + "; ".join(parts))
        for num in groups.get("FAILED", []) + groups.get("INTERRUPTED", []):
            self.log.info(
                f"  -> 步骤 {num:02d} {titles.get(num, '')} 需重跑: "
                f"python main.py step {num} 单步重跑, 或 python main.py run 自动续跑"
            )
        self._write_usage_summary()
        self._sync_monitor_to_snapshot()

    def run_single(self, num: int) -> int:
        if num not in (11, 12, 13):
            return self._run_single_impl(num)
        self._hold_ss_workflow()
        try:
            return self._run_single_impl(num)
        finally:
            self._release_ss_workflow()

    def _run_single_impl(self, num: int) -> int:
        """单独运行某个步骤(独立审查入口)。"""
        if num >= 9 and self._keyword_confirmation_pending():
            self.log.info("⏸ 请先确认步骤 8 的关键词清单。")
            return 3
        gate = self._blocking_manual_gate(num)
        if gate:
            self.log.info("⏸ 等待人工处理: %s", gate['message'])
            return 3
        steps = {s.number: s for s in get_steps()}
        step = steps.get(num)
        if step is None:
            self.log.error(f"步骤 {num} 未注册")
            return 2
        cur = self.state.status_of(num)
        self.log.info(f"单步运行: 步骤 {num:02d} {step.title}(当前状态 {cur}; 仅执行该步)")
        if cur in ("FAILED", "INTERRUPTED"):
            self.log.info(f"  -> 单步重跑: 上次状态 {cur}, 本次重新执行该步")
        elif cur == "DONE":
            self.log.info("  -> 该步已 DONE, 单步运行会强制重新执行(覆盖旧产物)")
        # 单步运行只检查**这一步自己**依赖的登录态: 纯本地步骤(2/3/4/7/8/14-19)与
        # MCP 接口步骤(5/6)不弹浏览器也不读 COOKIE, 由 _precheck_login 依赖表直接跳过;
        # 步骤 0/1/9/10 查亚马逊, 0/11/12/13 查卖家精灵, 1/10 另查 COOKIE
        if not self._precheck_step_login(num):
            if getattr(self, '_login_waiting_for_admin', False):
                self._log_run_summary(f"步骤 {num:02d} 等待管理员重新登录亚马逊")
                return 3
            self._log_run_summary(f"步骤 {num:02d} 执行前登录检查失败")
            return 2
        if not getattr(self, "_snapshot", None):
            self.snapshot_dir()
        self._log_price_source()
        self._report_interruptions()
        self._backfill_synced_steps(num)
        try:
            code = self._run_one(step)
        except KeyboardInterrupt:
            self._mark_current_interrupted()
            raise
        if code == 3:
            # ★★ 修复（审核中-3，2026-10-10）：rc=3 是「⏸ 等待人工确认」，不是失败。
            #   原来这里 `if code != 0: 执行失败` 把"等人工"也报成"失败"（对照 `run()` 的
            #   `:378-384` 处理正确，`run_single` 这条 Web「单步执行」路径漏了）。
            self._log_run_summary(f"⏸ 单步 {num:02d} 暂停(等待人工确认)")
        elif code != 0:
            self._log_run_summary(f"单步 {num:02d} 执行失败")
        else:
            self.log.info(f"单步运行完成: 步骤 {num:02d} 当前状态={self.state.status_of(num)}")
            self._write_usage_summary()
        return code

    def reset_category(self) -> str:
        """重置类目: 先把现有实时数据归档为快照, 再清空实时目录。

        **LLM 账本不清零**(2026-09-29 用户定稿的记账规则: "如果重置了步骤, token 和花费
        不重置, 依然是累计"): 账本随实时目录一起被删的话, 累计金额就会归零 —— 所以这里把
        `llm-usage*.jsonl`(含轮转卷)与 `llm-usage-steps.csv` 先读进内存, 清空后再放回。
        归档里那份仍然保留(交付物自包含), 但看板读的是实时目录这一份。
        """
        snap = self.paths.new_snapshot_dir()
        self.log.info(f"重置类目: 归档现有数据到 {snap}")
        self.snapshot_all()
        import logging
        import shutil
        for handler in list(self.log.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()
                self.log.removeHandler(handler)
        kept = self._read_usage_ledger()
        if self.paths.cat_root.exists():
            shutil.rmtree(str(self.paths.cat_root), ignore_errors=True)
        self.paths.cat_root.mkdir(parents=True, exist_ok=True)
        if kept:
            self._restore_usage_ledger(kept)
            self.log.info(f"LLM 账本已保留({len(kept)} 个文件): 累计 token/费用不因重置清零")
        self.state = RunState(
            self.paths.monitor_file("step-status.txt"),
            self.paths.monitor_file("step-duration.csv"),
        )
        self.ctx.state = self.state
        return snap

    def _read_usage_ledger(self) -> dict[str, bytes]:
        """读走实时监控目录里的 LLM 账本(主文件 + 轮转卷 + CSV), 供重置类目后放回。"""
        out: dict[str, bytes] = {}
        try:
            folder = self.paths.monitor_dir()
        except Exception:                              # noqa: BLE001 — 取不到目录就当没有账本
            return out
        if not os.path.isdir(folder):
            return out
        try:
            names = os.listdir(folder)
        except OSError:
            return out
        for name in names:
            if not (name.startswith("llm-usage") and name.endswith((".jsonl", ".csv"))):
                continue
            try:
                with open(os.path.join(folder, name), "rb") as fh:
                    out[name] = fh.read()
            except OSError:
                continue
        return out

    def _restore_usage_ledger(self, kept: dict[str, bytes]) -> None:
        try:
            folder = self.paths.monitor_dir()
            os.makedirs(folder, exist_ok=True)
            for name, data in kept.items():
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(data)
        except OSError as exc:                         # noqa: BLE001 — 账本丢失不得阻断重置
            self.log.warning(f"LLM 账本保留失败(不阻断重置): {exc}")

    def snapshot_all(self) -> str:
        """手动完整快照: 所有步骤产物归位到 00-20 目录(每次新建一轮)。"""
        snap = self.paths.new_snapshot_dir()
        for step in get_steps():
            step.bind_cat(self.ctx)
            files = [self.ctx.resolve_artifact(a) for a in step.artifacts]
            files += [self.ctx.resolve_artifact(a) for a in step.extra_archive_files
                      if os.path.exists(self.ctx.resolve_artifact(a))]
            sync_step_files(step.number, files, snap, self.paths)
            for d in step.archive_dirs:
                sync_step_dir(step.number, self.paths.resolve_cat_relative(d), snap, self.paths)
        self._sync_cfg_to_snapshot(snap)   # 配置随类目进快照(2026-09)
        self.log.info(f"完整快照已生成: {snap}")
        return snap
