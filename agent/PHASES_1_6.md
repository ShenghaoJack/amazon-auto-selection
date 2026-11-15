# Agent 层 Phase 1–6 流程结构图

> 目标：基于现有流水线产物，用 crewAI + Flask 做"类目分析助手"。本层**不触碰** `steps/*`、`core/orchestrator.py`。

## 一、整体数据流（分层结构）

```
[数据输入]
  data/<CAT>/config.yaml             ← 类目标识/站点/月份/LLM密钥/阈值
  data/<CAT>/*.xlsx|json|txt         ← 20步(0-19)产出的交付物(关键词报告/变体/流量拆分/分析报告/属性标注/监控)
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Phase 1 · CategoryContext(落地层)                                      │
│   输入: config.yaml + 各产物 → 输出: 13个数据块 + 加工块 + provenance    │
└───────────────────────┬──────────────────────────────────────────────┘
                        │ 只读、规范化、缺文件降级
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Phase 2 · GroundedTool(数据访问器)                                     │
│   输入: CategoryContext → 输出: get_*_text(ctx)->str ×9 个紧凑摘要      │
│                              build_tools(ctx)->crewai Tool[]           │
└───────────────────────┬──────────────────────────────────────────────┘
                        │ 摘要文本 + 记录provenance
                        ▼
                ┌───────┴────────┐
                │ Phase 3·build_llm│  ← 复用 Config 的 LLM 配置
                │  输入: Config    │
                │  输出: crewai LLM│
                └───────┬────────┘
                        ▼
┌───────────────────────┬───────────────────────────────┬────────────────┐
│ Phase 4 · 报告Crew(顺序进程)      │ Phase 5 · 问答Crew(层级进程)        │
│  输入: ctx + config + llm        │  输入: ctx + config + 问句 + llm    │
│  run_summary_mock(ctx)            │  ask_mock(ctx, 问句)                │
│  build_report_crew(ctx,cfg,llm)   │  build_qa_crew(ctx,cfg,llm)        │
│  run_summary(...) -> 报告+sources │  ask(...) -> 答案+sources           │
└───────────────────────┬───────────────────────────────┴────────────────┘
                        │ 真跑时把 llm 换成"硬化版"
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Phase 6 · 硬化LLM(增强项)                                               │
│   输入: Config(DRY_RUN→mock / 真实)                                     │
│   输出: HardenedLLM / build_hardened_llm()->crewai兼容LLM               │
│         保证: 非思考 + JSON + 线程看门狗 + trust_env=False + 多温重试    │
└──────────────────────────────────────────────────────────────────────┘
                        │
                        ▼
        (Phase 7·Flask服务层 —— 计划中: 将 run_summary/ask 暴露为 HTTP API)
```

## 二、两个运行路径(每个 crew 都有)

| 路径 | 入口 | 依赖 | 说明 |
|---|---|---|---|
| **Mock 冒烟** | `run_summary_mock` / `ask_mock` | 无 crewAI/无 LLM | 用规则分类+工具证据组装骨架，验证结构与顺序 |
| **真实运行** | `run_summary` / `ask`(经 `build_*_crew`) | crewAI + LLM key | `kickoff()` 真正执行 Agent 推理 |

---

## 三、逐 Phase：功能 / 输入 / 输出

### Phase 1 · CategoryContext（落地层）
- **功能**：只读加载一个类目的配置与交付产物，规范化为结构化数据块；记录 provenance；缺文件优雅降级。
- **输入**：`data/<CAT>/config.yaml` + `data/<CAT>/` 下的 xlsx/json/txt 产物
- **输出**（`CategoryContext` 对象）：
  - 13 个数据块：`keywords(keyword_overview/keyword_summary/keyword_seasonality)`、`variants`、`split(split_original)`、`report_sheets`、`attributes`、`bestsellers`、`candidate_keywords`、`keyword_data`、`screen_results`、`monitoring`
  - 加工块：`kpi_rows`、`gate_rows`、`brand_rank_rows`、`qualified_keywords`、`attribute_freq`
  - 元信息：`overview()`、`available()`、`provenance`

### Phase 2 · GroundedTool（数据访问器）
- **功能**：把 Phase 1 的上下数据包成**紧凑文本**工具，喂给 agent；每个工具读取时记录 provenance（防幻觉）。
- **输入**：`CategoryContext`
- **输出**：
  - 纯函数（可测，零 crewAI）：`get_category_overview_text / get_kpis_text / get_gates_text / get_brand_ranking_text / get_keyword_table_text / get_qualified_keywords_text / get_variant_table_text / get_attribute_frequency_text / get_monitoring_text`（均 `(ctx) -> str`）
  - 包装器：`build_tools(ctx) -> [crewai Tool]`（懒导入 crewAI）

### Phase 3 · LLM 工厂
- **功能**：复用现有 `LLM_BASE_URL/API_KEY/MODEL`，构造 crewAI LLM；模型名归一化、参数组装、配置校验。
- **输入**：`Config`（含 LLM_API_KEY / LLM_MODEL / LLM_BASE_URL）
- **输出**：`build_llm(config, temperature, max_tokens) -> crewai LLM`；纯函数 `normalize_model` / `llm_kwargs` / `validate_config` / `describe_llm`

### Phase 4 · 报告 Crew（顺序进程）
- **功能**：定义角色/任务/工具指派 spec，构造**顺序进程** Crew，产出深度选品分析报告。
- **输入**：`CategoryContext` + `Config`(LLM)
- **输出**：
  - spec：`REPORT_ROLES`(4角色) / `REPORT_TASKS`(任务链) / `AGENT_TOOL_MAP` / `ROLE_BY_KEY`
  - Mock：`run_summary_mock(ctx) -> {report(mock), sources, steps}`
  - 真实：`build_report_crew(ctx, config, llm) -> Crew`；`run_summary(config, ctx, llm) -> {report, sources}`
- **角色链**：类目数据研究员(researcher) → 关键词分析师(keyword) → 市场竞争分析师(market) → 类目负责人(lead)

### Phase 5 · 问答 Crew（层级进程）
- **功能**：定义问题分类→分析师映射，构造**层级进程** Crew（路由 Agent 委派给分析师），产出自然语言答案。
- **输入**：`CategoryContext` + `Config`(LLM) + 问题字符串
- **输出**：
  - spec：`QA_ROUTER_ROLE` / `QA_CATEGORIES` / `CATEGORY_TO_ANALYST` / `CATEGORY_LABEL`
  - 纯函数：`classify_question(问句)->类别` / `analyst_for_category(类别)->分析师`
  - Mock：`ask_mock(ctx, 问句) -> {answer(mock), category, analyst, tools, mock}`
  - 真实：`build_qa_crew(ctx, config, llm) -> Crew`；`ask(config, ctx, 问句, llm) -> {answer, sources}`
- **分类→分析师**：kpi→researcher / keyword→keyword / market→market / general→lead

### Phase 6 · 硬化 LLM（增强项）
- **功能**：把主流水线那套硬化逻辑复制到 agent 层，并让 crewAI LLM 走这条硬化路径。
- **输入**：`Config`（`DRY_RUN=true`→内置 mock；否则真实）
- **输出**：
  - `HardenedLLM`（看门狗/非思考/JSON/多温重试/trust_env=False 的客户端）
  - `build_hardened_llm(config) -> crewAI 兼容 LLM`
  - 纯函数：`messages_to_prompt` / `build_llm_payload` / `extract_json` / `describe_hardened`

---

## 四、依赖沿革(从 Phase 1 到 6)

```
Phase1: 产物 → 数据块                 (零依赖, 只读)
Phase2: 数据块 → 工具摘要              (零依赖纯函数)
Phase3: Config → crewai LLM           (crewAI 懒加载)
Phase4: 上下文 → 报告(顺序)            (spec+mock 零依赖; 真跑需crewAI)
Phase5: 上下文+问句 → 答案(层级)       (spec+mock 零依赖; 真跑需crewAI)
Phase6: 给真跑的 LLM 加硬化            (纯逻辑零依赖; 集成需crewAI)
```
