"""唯一配置入口。

对应需求文档 3.5 配置清单。所有类目参数/密钥/路径集中在此,
换类目只修改 config.yaml 的类目参数, 不修改代码。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

from .errors import ConfigError
from .prices import DEFAULT_PRICES_URL

# ★★ **部署期密钥：环境变量优先**（2026-10-12 用户口径）。
#
# 为什么：系统要上服务器，而**令牌不能进源码/仓库**（源码一分享即泄露），
#   也不该让每个类目的 `config.yaml` 各存一份（14 个类目 = 14 份，换令牌要改 14 处）。
#   环境变量是标准做法；而 `webui` 起的流水线是**子进程**，且
#   `subprocess.Popen(cmd, env=dict(os.environ)…)` → **继承看板服务的环境**
#   → **启动看板前设一次，所有类目共用**。
#   Linux: `Environment="LLM_API_KEY=…"`（systemd）／`docker run -e …`
#   Windows: `setx LLM_API_KEY "…"`（永久）或 `set LLM_API_KEY=…`（本次会话）
#
# ⚠ 只在 `Config.load()`（读**真实配置文件**跑任务）时生效，**不在 `Config({...})`** 时生效 ——
#   否则单元测试里构造的配置会被本机环境变量悄悄改掉（假失败/假通过）。
#   环境变量为空 → **不动**，回落到 `config.yaml`（向后兼容：老部署不改也能跑）。
ENV_OVERRIDE_KEYS: tuple[str, ...] = ("LLM_API_KEY", "VISION_API_KEY", "SS_MCP_KEY",
                                      "PANGOLIN_MCP_KEY")


def apply_env_overrides(data: dict) -> dict:
    """把环境变量里的**密钥**覆盖进配置（空的不动）。就地改并返回 `data`。"""
    for k in ENV_OVERRIDE_KEYS:
        v = str(os.environ.get(k, "") or "").strip()
        if v:
            data[k] = v
    return data


def fix_cookie_quoting(path: str | os.PathLike) -> bool:
    """修复 config.yaml 中 COOKIE 行的引号冲突(实测: 新 COOKIE 常含双引号,
    如 `x-main="hVql..."`, 与双引号包裹的值冲突导致 YAML 解析失败)。

    修复方式: 把 COOKIE 值改为**单引号包裹**(内部单引号双写转义),
    双引号在单引号字符串内是普通字符, 不再冲突。
    返回是否发生了修复; 未找到 COOKIE 行/无需修复返回 False。
    """
    path = Path(path)
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    lines = text.splitlines()
    changed = False
    for i, ln in enumerate(lines):
        if not ln.startswith("COOKIE:"):
            continue
        raw = ln[len("COOKIE:"):].strip()
        if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
            value = raw[1:-1]
            if raw[0] in value:
                escaped = value.replace("'", "''")
                lines[i] = f"COOKIE: '{escaped}'"
                changed = True
        break
    if not changed:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return True


class Config:
    """配置包装器。

    以 dict 承载全部配置项, 提供类型化访问与列表/字典解析辅助。
    默认值遵循需求文档 3.5 配置清单的"示例/默认"列。
    """

    DEFAULTS: dict = {
        "DATA_ROOT": "data",
        "DRY_RUN": False,
        "CAT": "",
        "LIST_URLS": "",
        "SITE": "美国",
        "MARKET": "amazon.com",
        "TRAFFIC_MONTH": "",
        "COMPETITOR_MONTH": "",
        "TREND_MONTH": "",
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_MODEL": "deepseek-chat",
        "VISION_API_KEY": "",
        "VISION_BASE_URL": "https://api.deepseek.com",
        "VISION_MODEL": "deepseek-v4-flash-vision-exp",
        # (2026-10-12 方案 C：`ATTR_KEYS`/`ATTR_DESC` **已真删**)
        #   它们只是「人工属性 / 手改说明」的**耐久补丁** —— 因为 `step16` 原来每次都
        #   **整份重写 spec**，只有登记在这两个键里的东西才活得过下一轮。
        #   现在 `step16` 改成**以「上一份 spec」为基底、只增不减、人工改过的不覆盖**
        #   （`step16._merge_prev_spec`）→ **spec 成为唯一真值**，补丁不再需要。
        #   顺带除掉了 `ATTR_DESC` 那个脆弱格式：它的**值里带全角`；`**，而 `；` 正是分隔符
        #   （实测解析出假键 `否`）。
        "FILTER_REQUIREMENT": "排除与本产品无关的",
        "SCREEN_REQUIREMENT": (
            "月搜索量>=3万, 站内同比>=-20%, Google同比>=-20%, CPC<3"
        ),
        "SS_MCP_URL": "https://mcp.sellersprite.com/v2/mcp",
        "SS_MCP_KEY": "",
        "PYTHON": "python",
        # ★ 2026-10-12 升级默认值：**15 个类目全都写了同一个非默认值**（`data/profiles/...`）
        #   → 与其让 15 份 config 各抄一遍，不如把它变成默认（本次已从各类目 config 删掉该行）。
        #   ⚠ 这两个键**必须**在这里改：`amz_profile_dir`/`ss_profile_dir` 属性虽然也有
        #     `or os.path.join(profile_dir, ...)` 兜底，但 `data.get()` 拿到的是**本字典的字面量**
        #     （不是 None）→ 兜底**不会触发**。所以"只删配置行、不改默认"会退回旧路径 ✗
        #     （旧路径不是共享目录 → **按类目隔离会失效**，见 core/profile_isolate.py:215-219）
        "SS_PROFILE": "data/profiles/ss-profile",
        "SS_ACCOUNT": "",
        "SS_PASSWORD": "",
        # 多类目并行: 卖家精灵浏览器是全局资源(CDP 共用一个 Chrome + 共享导出配额),
        # 全局锁等不到时最长等多久(秒; 默认 1800). 见 core/ss_lock.py
        "SS_BROWSER_LOCK_WAIT": 1800,
        "AMZ_PROFILE": "data/profiles/amz-profile",   # ★ 同上：升级为 15 类目统一在用的值
        # (2026-10-12 删除 `SS_ASIN_PREFIX`)：文档写着"卖家精灵 ASIN 清单文件名前缀(默认=CAT)"
        #   给步骤 11/12/13 用，但**全仓没有任何代码读它**（只有这里和 `_sim` 副本的定义）——
        #   是**死键**：14 个 config 里全是空串，改它行为不变，却一直显示在 Web 表单的
        #   「其他参数(高级配置)」兜底组里，看着像在生效。按用户口径删除。
        "COOKIE": "",
        # COOKIE 预检查的行为已**写死在代码里**(2026-10-08 用户定调: 检查照做, 失败放行、交给兜底链):
        #   `core/orchestrator.py::_precheck_cookie()` 固定 = 探测 + 自动定性 + CRIT 告警 + **不阻断**;
        # 因此不再有 `COOKIE_CHECK` / `COOKIE_CHECK_BLOCK` 两个键(配置里写了也会被忽略并提示废弃)。
        "WECOM_WEBHOOK": "",
        "GATE_MIN_QUALIFIED": 2,
        # FR-15 类目门禁④ 阈值(2026-09-11 起可配): 新品行的唯一 BuyBox卖家数 > 本值 → 达标
        "GATE_NEW_SELLERS": 10,
        "SEARCH_VOLUME_THRESHOLD": 30000,
        "MOM_YOY_MIN": -20.0,
        "GOOGLE_YOY_MIN": -20.0,
        "CPC_MAX": 3.0,
        "BATCH_FILTER": 10,
        # 2026-09-08(用户选定阶梯方案): BATCH_FILTER = 批判定起点(默认 10),
        # 失败/漏判降档: 集中重发(同批) -> 拆2条/批 -> 1条/批逐条重试;
        # FILTER_JUDGE_TRIES = 1条/批阶段单条重试次数; FILTER_JUDGE_FALLBACK =
        # 多次仍失败兜底(keep=保留标"待复核"不误杀 / drop=剔除宁缺勿滥);
        # BATCH_FILTER<=1 = 全逐条(显式配置)
        "FILTER_JUDGE_TRIES": 3,
        "FILTER_JUDGE_FALLBACK": "keep",
        # 2026-09 提速: FILTER_JUDGE_WORKERS = 首轮/集中重发两档的并发批数(1=串行)。
        # 只并发"批判定健康路径", 拆小/1条/批/最终兜底仍串行(失败尾巴量小, 兜底不变)。
        "FILTER_JUDGE_WORKERS": 3,
        "BATCH_PARENT": 20,
        "BATCH_VARIANT": 1000,
        "CANDIDATE_LIMIT": 20,
        "CORE_KEYWORD_COUNT": 5,
        # 长尾条数: LONGTAIL_PER_DIM = 每维条数(2026-09-13 起为准, 5 维 -> 默认 25 条);
        # LONGTAIL_COUNT = 旧键(总条数), 仅在未配置 LONGTAIL_PER_DIM 时按 //5 折算, 保留兼容
        "LONGTAIL_COUNT": 20,
        "LONGTAIL_PER_DIM": 5,
        "CORE_SEARCH_PAGES": 5,
        "LONGTAIL_SEARCH_PAGES": 4,
        "REQUEST_RETRY": 2,
        # ★ 2026-10-12 升级：以下 3 个抓取参数，**15 个类目全都写了同一个非默认值**
        #   （1 / 2 / 4）→ 直接变成默认，省掉 15 份 config 各抄一遍。
        #   为什么要"慢"：`FETCH_WORKERS=1` 单线程 + 2~4 秒间隔，是实测不被限流的值。
        "REQUEST_MIN_INTERVAL": 2,
        "REQUEST_MAX_INTERVAL": 4,
        "FETCH_WORKERS": 1,
        "FETCH_ROUND_PAUSE": 60,
        # 2026-09-16 用户定调"太多参数了, 我只需要个兜底的就行" → 直连重试细节内化:
        # 直连阶段固定 = 首轮 + 1 轮重试(原默认 3 轮), 间隔 60 秒; 失败一律靠 FALLBACK 兜底。
        "FETCH_STALL_ROUNDS": 1,
        # 兜底总开关(唯一需要用户关心的键): auto=直连抓不到就用本机浏览器补回来(默认);
        # off=完全不用浏览器(只直连, 失败如实记为失败)。步骤01 与步骤10 共用它。
        "FALLBACK": "auto",
        # 浏览器补抓轮数 / 轮间冷却(2026-09-16 用户要求"补抓后还有失败的可以再进行一轮补抓"):
        # 直接请求轮用尽后残余全部转本机浏览器补抓, 补抓后仍有失败会自动再补一轮
        # (默认 2 轮, 每 ASIN 每轮最多 1 次 URL 形态), 轮间冷却默认 30 秒
        "BROWSER_FALLBACK_ROUNDS": 2,
        "BROWSER_ROUND_PAUSE": 30,
        # 有头兜底轮数(2026-09-29 用户要求: "如果无头的无效再使用有头的"):
        # 无头 Chrome 更容易被亚马逊识别(实测: 同一 IP/Profile 下无头吃验证码页, 桌面 Chrome 正常),
        # 所以**无头补抓轮用尽后仍有未解决**时, 再开一个**可见窗口**补 1 轮(窗口由程序自动开关)。
        # 0 = 关闭有头兜底; FALLBACK: off 时整条浏览器兜底都不启用(此键无效)。
        "BROWSER_HEADED_ROUNDS": 1,
        "FETCH_LONG_EVERY": 25,
        "FETCH_LONG_MIN": 8,
        "FETCH_LONG_MAX": 15,
        "DOWNLOAD_TIMEOUT": 120,
        "SCREENSHOT_EVERY": 5,
        "STUCK_MINUTES": 10,
        "CONSECUTIVE_FAIL_WARN": 5,
        "RECENT_SUCCESS_RATIO": 0.5,
        "HTTP_TIMEOUT": 30,
        "LLM_TIMEOUT": 300,     # LLM 单请求超时秒 ★2026-10-12 升级: 原 60，但 15 个类目全用 300
                                # (长判定 prompt 服务端偶发慢响应 → 60 会误判超时; 300 更稳)
        "LLM_NO_THINK": True,
        # ============ LLM 用量/费用监控(2026-09-28 新增, Step 1) ============
        # Token 数不自己数: 官方口径是"以模型返回的 usage 为准"。下面这些键只影响
        # **计价与落盘**; 全部有默认值, 类目 config.yaml 不写也生效。
        # LLM_PRICES_MODE: live=实时抓官网(默认) / cache=只用磁盘缓存不联网 /
        #                  builtin=只用代码内置快照 / manual=只用 LLM_PRICES 手填
        "LLM_PRICES_MODE": "live",
        "LLM_PRICES_URL": DEFAULT_PRICES_URL,
        # 价格缓存有效期(小时): 到期才重新抓, 避免每个类目进程各抓一次
        "LLM_PRICES_TTL_HOURS": 24,
        # 币种校验: 中文页是"元"、英文页是"$"且数字不同 —— 不符则报错兜底, 不猜
        "LLM_PRICES_CURRENCY": "CNY",
        # 手动价表(最高优先; 元/百万 token)。形如:
        #   {deepseek-flash: {in_hit: 0.02, in_miss: 1.0, out: 4.0}}
        # 也支持 {off: {...}, peak: {...}} 两档写法。
        "LLM_PRICES": {},
        "LLM_PRICES_TIMEOUT": 15,
        # 是否把用量明细/步骤汇总写到 20_备份与监控/(false=只累加不落盘)
        "LLM_USAGE_LOG": True,
        # 账本增长控制(2026-09-28): llm-usage.jsonl 是 append-only, 且**位于被归档的
        # 监控目录里** → 每轮归档都会复制一份(实测约 1.5-2 MB/轮), 不控会一直长。
        # 超过 LLM_USAGE_MAX_MB 时轮转为 llm-usage-<run_id>.jsonl, 只保留最近
        # LLM_USAGE_KEEP 份(不压缩)。0 = 关闭轮转。
        "LLM_USAGE_MAX_MB": 20,
        "LLM_USAGE_KEEP": 5,
    }

    def __init__(self, data: dict):
        merged = copy.deepcopy(self.DEFAULTS)
        merged.update(data or {})
        self.data = merged

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            # 后台自动修复 COOKIE 引号冲突(新 COOKIE 含双引号时 YAML 解析失败),
            # 修复成功后重试一次; 仍失败或非 COOKIE 问题则抛原始错误
            if fix_cookie_quoting(path):
                print(
                    "[警告] 检测到 config.yaml 的 COOKIE 含双引号导致 YAML 解析失败, "
                    "已自动修复为单引号包裹"
                )
                with open(path, "r", encoding="utf-8-sig") as fh:
                    data = yaml.safe_load(fh) or {}
            else:
                raise ConfigError(f"配置 YAML 解析失败(非 COOKIE 引号问题): {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"配置文件格式错误(应为映射): {path}")
        # ★ 部署期密钥：**环境变量优先**（见 `ENV_OVERRIDE_KEYS` 的说明）。
        #   只在这一条路上做 —— `Config({...})` 不受影响（测试/内联构造保持原样）。
        return cls(apply_env_overrides(data))

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value

    def as_dict(self) -> dict:
        return copy.deepcopy(self.data)

    def _split_list(self, key: str) -> list[str]:
        raw = str(self.data.get(key, "") or "")
        parts = [p.strip() for p in raw.replace("；", ";").split(";")]
        return [p for p in parts if p]

    def _split_desc(self, key: str) -> dict:
        result = {}
        for item in self._split_list(key):
            if "=" in item:
                k, v = item.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    @property
    def cat(self) -> str:
        return str(self.data.get("CAT", "") or "").strip()

    @property
    def list_urls(self) -> list[str]:
        raw = str(self.data.get("LIST_URLS", "") or "")
        lines = [ln.strip() for ln in raw.replace("；", ";").split(";")]
        return [ln for ln in lines if ln]

    @property
    def market(self) -> str:
        return str(self.data.get("MARKET", "") or "amazon.com")

    @property
    def trend_month(self) -> str:
        return str(self.data.get("TREND_MONTH", "") or "").strip()

    @property
    def traffic_month(self) -> str:
        return str(self.data.get("TRAFFIC_MONTH", "") or "").strip()

    @property
    def competitor_month(self) -> str:
        return str(self.data.get("COMPETITOR_MONTH", "") or "").strip()

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("DRY_RUN", False))

    @property
    def data_root(self) -> str:
        root = str(self.data.get("DATA_ROOT", "data") or "data")
        return root

    @property
    def profile_dir(self) -> str:
        return str(self.data.get("PROFILE_DIR", "") or "data/profiles")

    @property
    def amz_profile_dir(self) -> str:
        return str(self.data.get("AMZ_PROFILE") or os.path.join(self.profile_dir, "amz-profile"))

    @property
    def ss_profile_dir(self) -> str:
        return str(self.data.get("SS_PROFILE") or os.path.join(self.profile_dir, "ss-profile"))

    def resolve_path(self, value: str) -> str:
        """把配置中的相对路径解析为相对项目根目录的绝对路径。"""
        if not value:
            return value
        if os.path.isabs(value):
            return value
        root = Path(__file__).resolve().parent.parent
        return str(root / value)

    def resolved_data_root(self) -> str:
        return self.resolve_path(self.data_root)
