"""步骤基类与上下文 (所有 21 个步骤的统一契约)。

每个步骤模块必须:
1. 继承 BaseStep, 定义 number/title/description
2. 声明本步产物 artifacts(实时目录相对文件名)与 archive_dirs(独立子目录)
3. 实现 run(ctx), 内部可调用 validate_upstream(ctx) 校验上游产物
4. 步骤标记 DONE 由编排器以"产物实际生成"为前提(7.3 硬性)

产物解析规则:
- 名称含 "/" 时按 cat_root 下子路径解析(如 "11_查流量来源/xxx.xlsx")
- 否则按类目根目录平铺文件名解析(自动带 <类目名>- 前缀的由步骤自行拼写)
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from core.browser import BrowserAutomation
from core.config import Config
from core.errors import StepFailedError
from core.http_client import HttpFetcher
from core.llm import LLMClient
from core.logger import get_logger
from core.mcp import MCPClient
from core.paths import Paths
from core.state import RunState
from core.usage import UsageMeter, build_meter


class StepContext:
    """步骤运行上下文: 配置/路径/日志/状态 + 客户端工厂。"""

    def __init__(
        self,
        config: Config,
        paths: Paths,
        logger=None,
        state: Optional[RunState] = None,
    ):
        self.config = config
        self.paths = paths
        self.log = logger or get_logger()
        self.state = state
        self._llm: Optional[LLMClient] = None
        self._http: Optional[HttpFetcher] = None
        self._mcp: Optional[MCPClient] = None
        # LLM 用量/费用累加器(懒构造: 首次用时才解析价格表并绑定落盘路径)
        self._usage: Optional[UsageMeter] = None
        # 本轮的账本身份(run_id): 由编排器在建立归档快照时设置 —— 用它而不是进程
        # 时间戳, 才能把"同一轮分多段跑完"的账合并成一本(见 PipelineRunner.snapshot_dir)
        self._usage_run_id: Optional[str] = None
        # 步骤内"等待"累计秒数(如备用账号窗口挂起): 不计入步骤活动耗时
        self._wait_seconds = 0.0

    def add_step_wait(self, seconds: float) -> None:
        """累计步骤内等待秒数(排除性计时, 不算进步骤耗时)。"""
        try:
            self._wait_seconds += max(0.0, float(seconds))
        except (TypeError, ValueError):
            pass

    def step_wait_seconds(self) -> float:
        return float(self._wait_seconds)

    def reset_step_wait(self) -> None:
        self._wait_seconds = 0.0

    @property
    def dry_run(self) -> bool:
        return self.config.dry_run

    def llm(self) -> LLMClient:
        if self._llm is None:
            # 传入本步的 meter → LLM 调用产生的 token/费用归属到当前步骤
            self._llm = LLMClient(self.config, meter=self.usage())
        return self._llm

    def set_run_id(self, run_id: str) -> None:
        """设置本轮的账本身份(编排器建立归档快照后立即调用)。

        必须在 meter 首次创建**之前**调用; 已经创建过就忽略并告警(说明调用时机错了)。
        """
        rid = str(run_id or "").strip()
        if not rid:
            return
        if self._usage is not None:
            self.log.warning(f"LLM 用量 meter 已创建, run_id={rid} 不会生效(调用时机过早/过晚)")
            return
        self._usage_run_id = rid

    def usage(self) -> UsageMeter:
        """本类目的 LLM 用量/费用累加器(懒构造)。

        首次调用会解析价格表(live 模式可能抓一次官网, 有 TTL 缓存与兜底),
        并绑定 `20_备份与监控/llm-usage.jsonl` / `llm-usage-steps.csv`。
        """
        if self._usage is None:
            self._usage = build_meter(self.config, self.paths, log=self.log,
                                      run_id=self._usage_run_id)
        return self._usage

    def begin_step(self, number: int) -> None:
        """编排器在某步开始前调用: 重置本步用量计数。"""
        self.usage().begin_step(number)

    def finish_step(self, number: int, status: str = "") -> str:
        """编排器在某步结束时调用: 落盘 + 打一行用量日志。返回该行文本。"""
        return self.usage().finish_step(number, status)

    def http(self, cookie: str | None = None) -> HttpFetcher:
        if self._http is None:
            self._http = HttpFetcher(self.config, cookie=cookie)
        return self._http

    def mcp(self) -> MCPClient:
        if self._mcp is None:
            self._mcp = MCPClient(self.config)
        return self._mcp

    def browser(
        self,
        site: str = "amazon",
        headed: bool = False,
        downloads_path: str | None = None,
        profile_dir: str | None = None,
        use_cdp: bool | None = None,
    ) -> BrowserAutomation:
        """站点: amazon -> AMZ_PROFILE, sellersprite -> SS_PROFILE。

        卖家精灵两种模式(2026-09 实测: 持久 Profile 会话在 headless 下不生效,
        工具页恒显示"未登录|游客"):
          ① CDP 后台模式(推荐, 配置 SS_CDP_URL 时): 直连用户已打开的 Chrome,
             复用其持久登录态, **不弹新窗口**(需求文档技术方案"CDP 直连");
          ② 有头兜底(未配 SS_CDP_URL): 自己启动有头浏览器, 会话正常但会弹窗。
        profile_dir: 覆盖 Profile 目录(备用账号独立 Profile 用)。
        use_cdp=False: 强制不走 CDP(备用账号换号用, 独立 Profile + 有头自动登录)。
        downloads_path: 下载保存目录(Playwright 自动接受下载, 无保存弹窗,
        文件直接落盘到该目录; 步骤 11/13 传各自子目录供 wait_download 轮询)。

        **多类目并行(2026-09-15)**: 卖家精灵浏览器是**按资源互斥**的 —— CDP 直连下
        "一个端口 = 一个 Chrome = 一份登录态/一份导出配额"。因此返回的是
        `core.ss_lock.SsBrowserLease`: 进入时先取**该资源**的锁(拿不到就等, 等待过程
        写进流水线日志, 超时明确报错), 退出时先关浏览器再放锁。
        **不同 CDP 端口(各自账号)的两个类目 = 两把不同的锁 → 真并行不等待**;
        同一端口的两个类目则排队串行(同一份登录态不能同时用)。亚马逊浏览器不受影响
        (profile 本来就按类目隔离, 见 `core/profile_isolate.py`)。
        """
        if site == "sellersprite":
            from core.ss_lock import (DEFAULT_WAIT_SECONDS, SsBrowserLease, primary_account,
                                      resource_key)

            profile = profile_dir or self.paths.profile_ss()
            cdp_url = str(self.config.get("SS_CDP_URL", "") or "").strip() or None
            # 2026-11-13: 配置里已不再保留 SS_CDP_URL —— 由系统自动分配空闲 CDP 端口
            #   Web 端启动时已注入；这里兜住 CLI(main.py run) 等其它入口。
            if not cdp_url and use_cdp is not False:
                try:
                    from core.ss_lock import pick_port
                    cdp_url = 'http://127.0.0.1:%d' % pick_port('')
                    self.config['SS_CDP_URL'] = cdp_url
                except Exception:
                    cdp_url = None
            wait = float(self.config.get("SS_BROWSER_LOCK_WAIT", DEFAULT_WAIT_SECONDS) or 0)
            if cdp_url and use_cdp is not False:
                browser = BrowserAutomation(
                    self.config,
                    profile_dir=profile,
                    dry_run=self.config.dry_run,
                    cdp_url=cdp_url,
                )
            else:
                browser = BrowserAutomation(
                    self.config,
                    profile_dir=profile,
                    headed=True,
                    dry_run=self.config.dry_run,
                    downloads_path=downloads_path,
                )
            if self.config.dry_run:
                return browser              # dry-run 不真开浏览器/不连 CDP, 无需抢锁
            label = f"{self.config.cat or 'pipeline'}·卖家精灵"
            res = resource_key(cdp_url or "", profile)
            return SsBrowserLease(browser, wait_seconds=wait, label=label,
                                  log=self.log.info, warn=self.log.warning,
                                  resource=res, account=primary_account(self.config))
        profile = profile_dir or self.paths.profile_amz()
        return BrowserAutomation(
            self.config,
            profile_dir=profile,
            headed=headed,
            dry_run=self.config.dry_run,
            downloads_path=downloads_path,
        )

    def resolve_artifact(self, name: str) -> str:
        """把步骤产物名解析为绝对路径。

        ★ 接线（2026-10-10）：**定义层文件**（属性集 / 规则 / 学习映射 / 词表聚类报告）
          走 `catalog.resolve_definition_file` —— 实现「**自己的目录第一优先**，
          自己目录没有再从共享沉淀层（`_catalog/<类目>/`）继承」。
          **其他名字原样返回**（交付表 / 稳定性结果 / 输入 json …），行为逐字不变。
        """
        if os.path.isabs(name):
            return name
        rel = name.replace("\\", "/")
        try:
            from tools.attr_stability.catalog import resolve_definition_file
            return str(resolve_definition_file(self.paths.cat_root, rel))
        except Exception:                                        # noqa: BLE001 — 兜底原行为
            return str(self.paths.cat_root / rel)


class BaseStep(ABC):
    """步骤抽象基类。"""

    number: int = -1
    title: str = ""
    description: str = ""
    artifacts: list[str] = []
    archive_dirs: list[str] = []
    # 归档附加产物: 会随步骤复制进归档目录, 但**不参与 DONE 校验/产物缺失判定**
    # (可能不存在, 如步骤10 无注入词时不写 injected-keywords.txt; 2026-09)
    extra_archive_files: list[str] = []

    def validate_upstream(self, ctx: StepContext) -> None:
        """校验上游产物(存在性), 缺失抛 StepDataError 阻断。默认不校验。"""

    @abstractmethod
    def run(self, ctx: StepContext) -> None:
        """执行本步骤。失败抛异常, 由编排器标记 FAILED 并阻断。"""

    def log(self, ctx: StepContext, msg: str) -> None:
        ctx.log.info(f"[{self.__class__.__name__}] {msg}")

    def now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def skip_when(self, ctx: StepContext) -> bool:
        """返回 True 时该步标记 SKIP(不执行/无产物)。默认不跳过。"""
        return False

    def validate_artifacts(self, ctx: StepContext) -> None:
        """产物质量校验(断点保护增强, 7.3)。

        断点续跑前对已 DONE 步骤调用: 产物虽存在但质量不合格(如全 error 记录)
        时抛异常, 编排器将该步骤重置为待执行重新跑。
        默认不校验; 子类按需覆写。
        """

    def bind_cat(self, ctx: StepContext) -> None:
        """绑定类目名到 _cat(供 artifacts 属性动态拼接文件名)。

        在 validate_upstream 未调用(如手动快照)但需要读取 artifacts 时使用。
        """
        if not getattr(self, "_cat", None):
            self._cat = ctx.config.cat

    def check_mcp_key(self, ctx: StepContext) -> None:
        """卖家精灵 MCP 密钥有效性检查(真实模式, 步骤5/6 调用)。

        2026-09-09 修正: 卖家精灵 tools/list 会**恒含哨兵工具**
        (secret_expired/secret_unauthorized/secret_invalid/secret_no_remaining,
        调用它们才返回对应错误)——不能因列表出现 secret_expired 就判密钥过期。
        判定 = 列表含任一**真实所需工具**(keyword_research_trends/aba_research_trend/
        google_trend/keyword_miner)即视为有效; 只剩哨兵/无工具才报错。
        """
        try:
            tools = ctx.mcp().check()
        except Exception as exc:
            raise StepFailedError(f"卖家精灵 MCP 连接失败: {exc}")
        required = {"keyword_research_trends", "aba_research_trend",
                    "google_trend", "keyword_miner"}
        if any(t in tools for t in required):
            return
        if not tools:
            raise StepFailedError("卖家精灵 MCP 无可用工具, 请检查 SS_MCP_KEY 权限")
        if "secret_expired" in tools:
            raise StepFailedError("卖家精灵 MCP 密钥(SS_MCP_KEY)已过期, 请到卖家精灵后台重新生成")
        raise StepFailedError(
            f"卖家精灵 MCP 工具列表异常(仅哨兵/无所需工具): {tools[:10]}"
        )

    def missing_artifacts(self, ctx: StepContext) -> list[str]:
        return [a for a in self.artifacts if not os.path.exists(ctx.resolve_artifact(a))]
