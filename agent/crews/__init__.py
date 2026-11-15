# agent 层: crewAI 编排(Agent/Task/Crew/LLM)。报告 crew = 顺序进程; 问答 crew = 层级进程。
#
# - `build_llm` / `normalize_model` / `llm_kwargs` / `validate_config` / `describe_llm`
#   LLM 工厂(Phase 3), 复用主线现有 LLM 配置。
# - `REPORT_ROLES` / `REPORT_TASKS` / `run_summary_mock` / `build_report_crew` / `run_summary`
#   Phase 4 报告 Crew(顺序进程): mock 冒烟无需 crewAI, 真跑需 crewAI + LLM key。
from .llm_factory import (
    build_llm,
    describe_llm,
    llm_kwargs,
    normalize_model,
    validate_config,
)
from .agents import (
    AGENT_TOOL_MAP,
    REPORT_ROLES,
    REPORT_TASKS,
    ROLE_BY_KEY,
    build_report_agents,
    mock_run_gather,
    tool_text_bundle,
)
from .report_crew import (
    build_report_crew,
    run_summary,
    run_summary_mock,
)
from .qa_crew import (
    QA_CATEGORIES,
    QA_ROUTER_ROLE,
    analyst_for_category,
    ask,
    ask_mock,
    build_qa_crew,
    classify_question,
)
from .hardened_llm import (
    HardenedLLM,
    build_hardened_llm,
    build_llm_payload,
    describe_hardened,
    extract_json,
    messages_to_prompt,
)
from .prompt_config import (
    build_prompt_spec,
    effective_qa_categories,
    effective_qa_router,
    effective_report_roles,
    effective_report_tasks,
    effective_tool_descriptions,
    load_prompts,
)

__all__ = [
    # LLM 工厂(Phase 3)
    "build_llm",
    "describe_llm",
    "llm_kwargs",
    "normalize_model",
    "validate_config",
    # Agent spec / 构建(Phase 4)
    "REPORT_ROLES",
    "REPORT_TASKS",
    "AGENT_TOOL_MAP",
    "ROLE_BY_KEY",
    "build_report_agents",
    "mock_run_gather",
    "tool_text_bundle",
    # 报告 Crew(Phase 4)
    "build_report_crew",
    "run_summary",
    "run_summary_mock",
    # 问答 Crew(Phase 5)
    "QA_ROUTER_ROLE",
    "QA_CATEGORIES",
    "classify_question",
    "analyst_for_category",
    "ask_mock",
    "build_qa_crew",
    "ask",
    # 硬化 LLM(Phase 6)
    "HardenedLLM",
    "build_hardened_llm",
    "build_llm_payload",
    "describe_hardened",
    "extract_json",
    "messages_to_prompt",
    # 提示词外置(人工可编辑)
    "load_prompts",
    "build_prompt_spec",
    "effective_report_roles",
    "effective_report_tasks",
    "effective_qa_router",
    "effective_qa_categories",
    "effective_tool_descriptions",
]
