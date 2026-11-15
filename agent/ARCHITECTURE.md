# 类目分析助手 · 架构与分步实现（crewAI + Flask）

> 目标：在**现有 20 步流水线(0-19)产物之上**新增一层**多智能体「类目分析助手」**，
> 用 crewAI 编排分析/问答，用 Flask 对外提供 API 与流式对话。
> **严格不触碰** `steps/*`、`core/orchestrator.py` 以及任何现有流水线产物结构。

---

## 0. 目标 / 非目标

### 目标
- 以 `data/<CAT>/` 下的真实交付产物为**唯一事实来源**，对某个类目做：
  1. **一键深度分析报告**（综合 KPI、类目门禁、品牌格局、关键词机会、竞品画像、建议结论）；
  2. **自然语言问答**（"为什么这个类目门禁①不达标？"、"Top3 品牌分别是什么定位？"）。
- 每次回答都能**追溯到具体产物字段**（带 sources 引用），不靠 LLM 编数字。
- 独立成包 `agent/`，自有依赖清单与启动脚本，不影响主流水线的"零新增依赖"。

### 非目标
- 不改、不重跑 20 步；不新增到 `requirements.txt`；不修改流水线产物 schema。
- 不做抓取/竞价/配额类操作（那属于现有流水线；本层只读分析）。
- 不追求完全自由的"自主 Agent"——**确定性 + 可追溯 + 可控成本**优先。

---

## 1. 总体架构（分层）

```
┌─────────────────────────────────────────────────────────────────────┐
│  前端(可选)  agent/web/              聊天 UI：类目选择 + 流式回答 + 引用 │
└──────────────────────▲───────────────────────────────┬─────────────┘
                       │ REST / SSE                     │ fetch
┌──────────────────────┴───────────────────────────────▼─────────────┐
│  Flask 服务层  agent/server.py                                      │
│   /api/agent/categories   列举类目(含可用产物/月份)                  │
│   /api/agent/summary      一次性深度分析(报告 crew)                  │
│   /api/agent/chat (SSE)   自然语言问答(问答 crew, 流式)              │
│   /api/agent/sources      最近一次回答的引用清单                     │
│   会话/缓存/线程池/单运行锁/审计日志                                 │
└──────────────────────▲───────────────────────────────┬─────────────┘
                       │ 调用 crew                       │ 读产物(只读)
┌──────────────────────┴───────────────────────────────▼─────────────┐
│  crewAI 层  agent/crews/                                             │
│   Agent: DataResearcher / KeywordAnalyst / MarketAnalyst /           │
│          LeadAnalyst / ReportWriter / QARouter                       │
│   Task:  顺序进程(报告) / 层级进程(问答)                              │
│   工具:  只经由 GroundedTools 访问数据(防幻觉)                        │
└──────────────────────▲───────────────────────────────┬─────────────┘
                       │ 工具只读                       │ 只读产物
┌──────────────────────┴───────────────────────────────▼─────────────┐
│  落地层  agent/context/                                            │
│   CategoryContext：加载 config + 各产物 → 规范化 DataFrame/块         │
│   记录 provenance(哪个工具读了哪块) → 生成 sources                     │
└──────────────────────▲───────────────────────────────┬─────────────┘
                       │ 复用 core.*                    │ 读文件
┌──────────────────────┴───────────────────────────────▼─────────────┐
│  现有流水线(不动)：data/<CAT>/…   +   core/{config,paths,io_utils}.py │
└─────────────────────────────────────────────────────────────────────┘
```

**核心设计原则：落地(grounding)，不靠 LLM 想象。**
LLM 永远不是数字来源；所有指标只能来自 GroundedTool 返回的数据。服务层从
工具调用记录里组装 `sources`，回答自带引用。

---

## 2. 依赖清单（独立，不污染主流水线）

新增 `agent/requirements.txt`（主 `requirements.txt` **保持不动**）：

```text
crewai>=0.108          # 多智能体编排（内含 litellm/pydantic-v2 依赖树）
flask>=3.0
flask-cors>=4.0
# pandas/openpyxl 已随主 requirements 存在，此处可重复也可省略
```

> ⚠️ crewAI 依赖树较重（litellm、pydantic v2 等）。建议给"助手层"单独建
> 虚拟环境/安装到隔离环境，避免与主流水线依赖冲突。安装后固定一个已验证版本，
> 例如 `pip install crewai==<已验证版本>`，并把版本号写进本文件顶部。

---

## 3. Phase 0 — 环境与骨架

```bash
# 隔离虚拟环境（推荐）
python -m venv .venv-agent
.venv-agent\Scripts\activate
pip install -r agent/requirements.txt

# 冒烟：确认 crewAI 可导入
python -c "from crewai import Crew, Agent, Task, Process, LLM; print(crewai.__version__)"
```

新增目录骨架（全部新文件，不与现有模块重名）：

```
agent/
├── __init__.py
├── requirements.txt
├── ARCHITECTURE.md
├── README.md
├── server.py                     # Flask 入口
├── agent.yaml                    # 助手专属配置(LLM/crew 参数/记忆开关/超时)
├── context/
│   ├── __init__.py
│   └── category_context.py       # 产物加载器(只读) + provenance
├── tools/
│   ├── __init__.py
│   └── artifact_tools.py         # @tool 数据访问器(防幻觉)
├── crews/
│   ├── __init__.py
│   ├── llm_factory.py            # 由 config 构造 crewai LLM(可选硬化)
│   ├── agents.py                 # 共享 Agent 定义
│   ├── report_crew.py            # 顺序分析 crew + run_summary()
│   └── qa_crew.py                # 层级问答 crew + ask()
└── web/
    ├── index.html                # 极简聊天 UI
    └── app.js
```

---

## 4. Phase 1 — 落地层 `CategoryContext`（只读产物加载器）

职责：给定类目配置，定位并加载所有相关产物为规范化结构；**缺文件优雅降级**；
缓存 + 基于 mtime 失效。复用 `core.config.Config`、`core.paths.Paths`、
`core.io_utils.read_excel_sheet`、`read_json`。

`agent/context/category_context.py`（骨架）：

```python
from __future__ import annotations
import os, json
from pathlib import Path
from functools import cached_property
import pandas as pd
from core.config import Config
from core.paths import Paths
from core.io_utils import read_excel_sheet, read_json, read_txt_lines

class CategoryContext:
    """只读地加载一个类目的配置与交付产物。缺文件不报错, 标记缺失。"""

    def __init__(self, config_path: str):
        self.config = Config.load(config_path)
        self.paths = Paths(self.config)
        self.cat = self.config.cat
        self._provenance: list[str] = []     # 记录读过的数据块, 供 sources

    # ---- 通用读取, 记录 provenance ----
    def _touch(self, block: str):
        if block not in self._provenance:
            self._provenance.append(block)

    def _excel(self, rel: str, sheet, block: str, default=None):
        self._touch(block)
        p = self.paths.cat_root / rel
        if not p.exists():
            return default
        try:
            return read_excel_sheet(str(p), sheet)
        except Exception:
            return default

    # ---- 各产物加载(懒加载 + 缓存)----
    @cached_property
    def keywords(self) -> pd.DataFrame | None:
        """步骤8 关键词报告 '关键词与季节性' sheet。"""
        return self._excel(f"{self.cat}-keyword-report.xlsx", "关键词与季节性", "keywords")

    @cached_property
    def variants(self) -> pd.DataFrame | None:
        """步骤10 父项变体明细(第一个 sheet)。"""
        return self._excel(f"{self.cat}-父项变体明细.xlsx", 0, "variants")

    @cached_property
    def split(self) -> pd.DataFrame | None:
        """步骤14 流量拆分 'AI已补充' sheet。"""
        return self._excel("14_流量拆分/流量拆分数据-需人工补充.xlsx", "AI已补充", "split")

    @cached_property
    def report(self) -> dict:
        """步骤15 初步类目分析报告: 读 4 个 sheet 为 {sheet_name: df}。"""
        self._touch("report")
        p = self.paths.cat_root / f"{self.cat}-初步类目分析报告.xlsx"
        if not p.exists():
            return {}
        import pandas as pd
        try:
            with pd.ExcelFile(p) as xl:
                return {s: xl.parse(s) for s in xl.sheet_names}
        except Exception:
            return {}

    @cached_property
    def attributes(self) -> pd.DataFrame | None:
        """步骤18 标准化交付(属性标注表)。"""
        return self._excel(f"18_标准化/{self.cat}-属性标注-标准化交付.xlsx", "属性标注", "attributes")

    @cached_property
    def bestsellers(self) -> dict | None:
        """步骤0 榜单 JSON。"""
        self._touch("bestsellers")
        return read_json(str(self.paths.cat_root / f"{self.cat}-bestseller-newrelease.json"))

    @cached_property
    def candidate_keywords(self) -> list[str]:
        """步骤4 关键词候选。"""
        self._touch("candidate_keywords")
        return read_txt_lines(str(self.paths.cat_root / f"{self.cat}-keywords-candidates.txt"))

    @cached_property
    def screen_results(self) -> list[dict]:
        """步骤7 筛选判定。"""
        self._touch("screen_results")
        return read_json(str(self.paths.cat_root / f"{self.cat}-screen-results.json")) or []

    @cached_property
    def monitoring(self) -> dict:
        """步骤19 状态/告警/体检(目录沿用 17_备份与监控; 供"数据成熟度"说明)。"""
        mdir = Path(self.paths.monitor_dir())
        self._touch("monitoring")
        step_status = {}
        sp = mdir / "step-status.txt"
        if sp.exists():
            for ln in sp.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = [x.strip() for x in ln.split("|")]
                if parts:
                    step_status[parts[0]] = parts[1] if len(parts) > 1 else ""
        return {
            "step_status": step_status,
            "alerts": tail_text(mdir / "monitor-alerts.txt"),
            "health": read_json(str(mdir / "health-report.json"), {}),
        }

    # ---- 通用汇总(供工具/前端使用) ----
    def overview(self) -> dict:
        return {
            "cat": self.cat, "site": self.config.get("SITE", ""),
            "market": self.config.market, "traffic_month": self.config.traffic_month,
            "artifacts": self._available(), "provenance": list(self._provenance),
        }

    def _available(self) -> list[str]:
        return [b for b in ("keywords","variants","split","report","attributes",
                            "bestsellers","candidate_keywords","screen_results")
                if getattr(self, b) not in (None, {}, [])]
```

> 说明：`_provenance` 会被服务层在读后取出，`overview()` 顺带返回。前端"引用"
> 就来自它（combine 到 sources）。

**列名归一化**：现有 `steps/step15.py`、`step16.py` 里有 `_norm_col/_find_col`，
为隔离起见在 `agent/context` 里各放一份（不 import steps）。列取值示例（已核实）：

- 关键词报告：`关键词/站内月搜索量/站内同比%/Google同比%/CPC/CPC下限/CPC上限/达标判定/Google季节性/站内季节性`
- 流量拆分（AI已补充）：`变体ASIN/标题/五点描述/SKU/详细参数/变体月销量/变体月销额/价格/品牌/配送方式/新老品/评分/卖家数/图片链接/主图`
- 属性标注：原表列 + `属性_<key>` + `属性_<key>_来源`

---

## 5. Phase 2 — GroundedTool（防幻觉的数据访问器）

`agent/tools/artifact_tools.py`：把数据以**文本摘要**形态喂给 agent（不喂整表，控制 token）。
每个工具都回传"我用的是哪块数据"，由上下文记录 provenance。

```python
from __future__ import annotations
from crewai.tools import tool

def _to_rows(df, cols, n=20) -> str:
    """列名命中时取前 n 行, 转成紧凑文本; 缺失列跳过。"""
    if df is None or df.empty:
        return "暂无数据"
    def norm(c): return "".join(ch for ch in str(c).lower() if ch.isalnum())
    present = {norm(c): c for c in df.columns}
    use = [present[norm(c)] for c in cols if norm(c) in present and norm(c)]
    sub = df[use].head(n)
    return sub.to_csv(index=False)

def build_tools(ctx):
    @tool("get_category_overview")
    def get_category_overview() -> str:
        """返回该类目基本概况(类目/站点/数据月份/可用产物)。"""
        return _fmt(ctx.overview())

    @tool("get_kpis")
    def get_kpis() -> str:
        """返回核心 KPI 表(变体总数/总月销量/总月销额/价格均值与中位数/品牌数)。"""
        r = ctx.report.get("综合分析")
        return _to_rows(r, ["板块","项目","数值"], n=20) if r is not None else "暂无KPI"

    @tool("get_gates")
    def get_gates() -> str:
        """返回四个类目门禁的判定结果(达标/不达标/数据不足)。"""
        r = ctx.report.get("综合分析")
        if r is None: return "暂无门禁"
        gate = r[r["板块"].astype(str) == "类目门禁"]
        return _to_rows(gate, ["项目","数值","判定"], n=10)

    @tool("get_brand_ranking")
    def get_brand_ranking() -> str:
        """返回品牌销额排名(Top10, 含月销量/月销额/占比)。"""
        r = ctx.report.get("综合分析")
        if r is None: return "暂无品牌排行"
        br = r[r["板块"].astype(str) == "品牌销额Top10"]
        return _to_rows(br, ["项目","品牌","变体月销量","变体月销额","销额占比%"], n=10)

    @tool("get_keyword_table")
    def get_keyword_table(limit: int = 15) -> str:
        """返回关键词表(搜索量/同比/CPC/达标判定/季节性), limit 为取样条数。"""
        return _to_rows(ctx.keywords, ["关键词","站内月搜索量","站内同比%","CPC","达标判定"], n=min(int(limit), 20))

    @tool("get_qualified_keywords")
    def get_qualified_keywords() -> str:
        """返回判定为'达标'的关键词清单。"""
        df = ctx.keywords
        if df is None or df.empty: return "暂无关键词"
        def norm(c): return "".join(ch for ch in str(c).lower() if ch.isalnum())
        cols = {norm(c): c for c in df.columns}
        vc = cols.get("达标判定"); kc = cols.get("关键词")
        if vc is None or kc is None: return "无判定列"
        hit = df[df[vc].astype(str) == "达标"]
        return ", ".join(str(x) for x in hit[kc].head(30))

    @tool("get_variant_table")
    def get_variant_table(limit: int = 10) -> str:
        """返回变体明细(ASIN/品牌/价格/月销量/月销额/新老品)。"""
        return _to_rows(ctx.variants, ["变体ASIN","品牌","价格","变体月销量","新老品"], n=min(int(limit), 15))

    @tool("get_attribute_frequency")
    def get_attribute_frequency() -> str:
        """统计属性标注里各属性值的分布(找卖点/差异化机会)。"""
        df = ctx.attributes
        if df is None or df.empty: return "暂无属性标注"
        attr_cols = [c for c in df.columns if c.startswith("属性_") and not c.endswith("_来源")]
        out = []
        for c in attr_cols[:8]:
            out.append(f"{c}: " + df[c].value_counts().head(5).to_dict())
        return "\n".join(out) or "无属性列"

    @tool("get_monitoring")
    def get_monitoring() -> str:
        """返回数据成熟度: 各步骤状态与告警(说明数据是否可靠/完整)。"""
        return _fmt(ctx.monitoring)

    return [get_category_overview, get_kpis, get_gates, get_brand_ranking,
            get_keyword_table, get_qualified_keywords, get_variant_table,
            get_attribute_frequency, get_monitoring]
```

> 每个 `@tool` 都返回**紧凑文本**而非整表，并把"读过的块"通过 `ctx` 记录到
> `_provenance`（示例里 `_to_rows/_fmt` 内部应调用 `ctx._touch(块名)`；为简洁略。
> 关键是不让 agent 拿到未经筛选的原始大表）。

---

## 6. Phase 3 — LLM 工厂（复用现有密钥/模型, 可选硬化）

`agent/crews/llm_factory.py`：

```python
from crewai import LLM

def build_llm(config, temperature: float = 0.2, timeout: int = 120):
    """复用流水线同一套 LLM 配置(OpenAI 兼容), 通过 litellm 交给 crewAI。"""
    model = str(config.get("LLM_MODEL", "deepseek-chat") or "deepseek-chat")
    # DeepSeek 在 litellm 里通常写作 deepseek/<model>; 若已是 "provider/model" 则原样用
    if "/" not in model:
        model = f"deepseek/{model}"
    return LLM(
        model=model,
        base_url=str(config.get("LLM_BASE_URL", "https://api.deepseek.com")),
        api_key=str(config.get("LLM_API_KEY", "")),
        temperature=temperature,
        max_tokens=4096,
    )
```

**可选硬化（Phase 6）**：crewAI 的 LLM 不会自动做"非思考 + JSON + watchdog + 多温重试"。
要复用 `core/llm.py` 的可靠性，可子类化 crewai 的 `BaseLLM`，把 `call` 转发到
`LLMClient.chat/chat_json`（注入 `trust_env=False`、超时看门狗、非思考）。初始版本
先用 `build_llm()` 跑通，硬化作为增强项。

---

## 7. Phase 4 — 报告 Crew（顺序进程）

`agent/crews/agents.py`（共享 Agent 定义）：

```python
from crewai import Agent

def build_report_agents(ctx, llm):
    tools = build_tools(ctx)
    return [
        Agent(role="类目数据研究员", goal="从交付产物中提取准确、可引用的核心指标",
              backstory="你只依据工具返回的数据作答, 不臆造任何数值。",
              tools=tools[:3], llm=llm, verbose=True),
        Agent(role="关键词分析师", goal="解读关键词机会(搜索量/同比/CPC/达标词/季节性)",
              backstory="专注关键词表, 指出机会词与衰减词。",
              tools=[t for t in tools if getattr(t, "name", "") in
                     ("get_keyword_table","get_qualified_keywords","get_category_overview")],
              llm=llm, verbose=True),
        Agent(role="市场竞争分析师", goal="解读品牌集中度/类目门禁/价格带/渠道格局",
              backstory="专注门禁与品牌排行, 判断类目进入难度。",
              tools=[t for t in tools if getattr(t, "name", "") in
                     ("get_gates","get_brand_ranking","get_variant_table","get_attribute_frequency")],
              llm=llm, verbose=True),
        Agent(role="类目负责人", goal="综合各分析师结论, 形成机会/风险/可执行的选品建议",
              backstory="你是最终决策者, 任何结论必须能追溯到数据, 明确标注不确定处。",
              allow_delegation=False, llm=llm, verbose=True),
    ]
```

`agent/crews/report_crew.py`：

```python
from crewai import Task, Crew, Process
from .agents import build_report_agents
from .llm_factory import build_llm

def run_summary(config, ctx, llm=None):
    llm = llm or build_llm(config, temperature=0.2)
    agents = build_report_agents(ctx, llm)
    researcher, keyword, market, lead = agents

    tasks = [
        Task(description="提取并核验该类目核心指标(变体数/总销量/总销额/价格/品牌数/四类门禁判定)。"
                         "逐条列出数值并标注来源。只依据工具返回。",
             expected_output="结构化指标清单, 每条带数值与来源。", agent=researcher),
        Task(description="分析关键词表: 达标词、高搜索量词、同比与季节性信号、CPC 高低。",
             expected_output="关键词机会与风险的要点。", agent=keyword),
        Task(description="分析品牌格局与类目门禁: Top品牌集中度、价格带、新老品、属性差异化空间。",
             expected_output="市场结构判断与进入难度结论。", agent=market),
        Task(description="综合以上, 输出一份中文选品分析报告: 概览/机会/风险/结论与建议。"
                         "每个关键数字必须引用来源工具。",
             expected_output="Markdown 报告。", agent=lead),
    ]

    crew = Crew(agents=agents, tasks=tasks, process=Process.sequential,
                memory=True, verbose=True)
    result = crew.kickoff(inputs={"cat": ctx.cat})
    return {"report": result.raw, "sources": ctx.overview().get("provenance", [])}
```

---

## 8. Phase 5 — 问答 Crew（层级进程）

`agent/crews/qa_crew.py`：

```python
from crewai import Agent, Task, Crew, Process
from .agents import build_report_agents
from .llm_factory import build_llm

def ask(config, ctx, question, llm=None):
    llm = llm or build_llm(config, temperature=0.2)
    agents = build_report_agents(ctx, llm)
    researcher, keyword, market, lead = agents

    # 路由器: 判断问题领域(指标/关键词/供应链/建议)后委派给对应分析师
    router = Agent(role="问答路由", goal="判断问题类别并决定由哪个分析师回答, 汇总成最终答案。",
                   backstory="问题分级: 指标类→研究员; 关键词类→关键词分析师; 竞争/门禁→市场分析师; 综合→类目负责人。",
                   llm=llm, allow_delegation=True, verbose=True)

    main_task = Task(
        description=f"回答用户问题。先判定类别, 委派给对应分析师, 最后整合成简洁、可引用的中文答案。\n问题: {question}",
        expected_output="一段答案 + 关键数字的来源标注。", agent=router,
    )

    crew = Crew(agents=[router, researcher, keyword, market, lead],
                tasks=[main_task], process=Process.hierarchical,
                manager_llm=llm, memory=True, verbose=True)
    result = crew.kickoff(inputs={"cat": ctx.cat, "question": question})
    return {"answer": result.raw, "sources": ctx.overview().get("provenance", [])}
```

---

## 9. Phase 7 — Flask 服务层

`agent/server.py`：Flask 提供 API + SSE；每次调用在独立线程跑 crew（kickoff 是阻塞的），
用队列做流式；按 `(cat,月)` 缓存 `CategoryContext`，监控 mtime 失效；单类目运行锁防止并发重跑。

```python
import queue, threading, os, json
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from core.config import Config
from context.category_context import CategoryContext
from crews.report_crew import run_summary
from crews.qa_crew import ask

app = Flask(__name__); CORS(app)
_ctx_cache: dict = {}; _ctx_lock = threading.Lock(); _run_locks: dict = {}

def get_context(config_file: str) -> CategoryContext:
    key = os.path.abspath(config_file)
    with _ctx_lock:
        ctx = _ctx_cache.get(key)
        if ctx is None:
            ctx = CategoryContext(config_file); _ctx_cache[key] = ctx
        return ctx

def _run_lock(cat: str):
    with _ctx_lock:
        if cat not in _run_locks: _run_locks[cat] = threading.Lock()
        return _run_locks[cat]

@app.get("/api/agent/categories")
def categories():
    # 扫描 data/*/config.yaml, 由 CategoryContext 读取可用产物
    ...

@app.post("/api/agent/summary")
def summary():
    body = request.get_json(force=True)
    config_file = body["config_file"]
    ctx = get_context(config_file)
    with _run_lock(ctx.cat):
        result = run_summary(ctx.config, ctx)      # 报告 crew
    return jsonify({"ok": True, "report": result["report"], "sources": result["sources"]})

@app.post("/api/agent/chat")
def chat():
    """SSE 流式问答(简单实现: 跑完后整段返回; 进阶用队列逐块 yield)。"""
    body = request.get_json(force=True)
    config_file = body["config_file"]; question = body["question"]
    ctx = get_context(config_file)
    def gen():
        yield "data: " + json.dumps({"type": "start"}) + "\n\n"
        with _run_lock(ctx.cat):
            out = ask(ctx.config, ctx, question)
        for chunk in _chunk(out["answer"]):          # 简单分块模拟流式
            yield "data: " + json.dumps({"type": "delta", "text": chunk}) + "\n\n"
        yield "data: " + json.dumps({"type": "done", "sources": out["sources"]}) + "\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream")

@app.get("/api/agent/health")
def health():
    return jsonify({"ok": True})
```

> 生产提示：不要用 Flask 自带 dev server 跑长任务。用 `gunicorn`/`waitress` 或保持
> 单进程 + 线程池，并设单类目锁。MVP 用 `waitress-serve --port 8877 agent.server:app`。

---

## 10. 分步落地推进表

| 阶段 | 交付物 | 验收 | 是否触碰现有20步 |
|---|---|---|---|
| 0 | 骨架 + 依赖 + 导入冒烟 | `crewai` 可导入 | 否 |
| 1 | `CategoryContext` 产物加载器 | 读真实 `data/salt-pepper-shaker-sets/` 各产物返回正确列；缺文件不炸 | 否 |
| 2 | GroundedTool 组 | 直接调用各 tool 得到摘要文本 | 否 |
| 3 | `build_llm` | 返回 crewai LLM 实例 | 否 |
| 4 | 报告 Crew | 对真实类目跑出报告(先 mock LLM 验结构, 再真钱跑一次) | 否 |
| 5 | 问答 Crew | 对问题返回有来源的答案 | 否 |
| 6 | (可选) 复用 `core.llm` 硬化 | LLM 具备超时/非思考/JSON | 否 |
| 7 | Flask 服务 + 极简 UI | `categories/summary/chat/health` 可用, SSE 流式 | 否 |
| 8 | 测试 + 文档 | `tests/agent/` 含结构断言；`agent/README.md` | 否 |

---

## 11. 风险与权衡

| 风险 | 缓解 |
|---|---|
| **幻觉/编造数字** | 只允许通过 GroundedTool 取数；provenance→sources；提示词强制"只依据工具返回"。后端可加一道校验：答案中出现的数字应与某工具结果匹配，不符则告警。 |
| **crewAI API 漂移**(迭代快) | 在 `agent/requirements.txt` 锁定已验证版本；把 crewAI 调用集中到 `agent/crews/`，变更不外溢。 |
| **成本↑ / 延迟↑** | 多 agent 更贵；给每个任务的 `max_tokens` 设上限；喂给 LLM 的是**摘要**而非整表；提供 `--mock`/假 LLM 模式先验结构；报告/问答按需启用 memory。 |
| **非确定性/可复现性下降** | 用低温度(0.2)；把 LLM 仅用于"解读/组织"，判定与数字仍来自确定性产物；保留 `sources` 便于回溯。 |
| **并发** | 每类目一把锁(单运行)；kickoff 在独立线程；服务用 waitress/gunicorn 生产运行。 |
| **依赖冲突** | 助手层独立 venv + 独立 `agent/requirements.txt`；不写进主 `requirements.txt`。 |

---

## 12. 参考（crewAI 文档）

- Custom LLM: <https://docs.crewai.com/v1.15.5/ar/learn/custom-llm>
- Processes(sequential/hierarchical): <https://docs.crewai.com/v1.12.0/en/concepts/processes>
- Tasks: <https://github.com/crewAIInc/crewAI/blob/c0fa66d1/docs/edge/ar/concepts/tasks.mdx>

> 上述 API 以安装时锁定的 crewAI 版本为准；本文以 v0.108+ / v1.x 的
> `Agent/Task/Crew/Process/LLM` 语义给出，落地时按版本微调。
