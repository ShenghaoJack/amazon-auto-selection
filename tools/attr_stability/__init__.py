# -*- coding: utf-8 -*-
"""属性标注稳定性工具：结构闸门 + 多运行一致性 + 交付装配

对外只暴露这些：
    RuleSource          从类目配置读规则（**不含任何类目词**）
    gate_run            结构闸门（pass/reject，不产出值）
    alone_rates         各运行的独异格率（诊断用）
    consistency         三类字段的一致率
    majority            多数表决
    deliverable_from_cells  装配交付表
    build_retry_brief   生成「指名到行」的重跑指令
    lint_category_words 闸门源码自检（不得含类目词）

    find_gaps / build_gap_fill_prompt / build_block_gap_prompt / merge_candidates
        定向追问（gap-fill）：只对**未明确**的格换问法再问一次，
        产出候选值+依据（不覆盖原值），成本远低于全量重跑

    normalize_form / converge_mechanical / convergence_report
        归类与标准化：形式归一 + 类型感知的受控值收敛（机械可判部分）
    build_classify_payload / apply_classification
        把收敛不了的交给大模型语义归类，并对返回值做词表校验

    Producer / FileProducer / CallableProducer / run_round / RoundResult
"""
from .attr_stability import (  # noqa: F401
    AttrSpec, RuleSource, GateIssue, GateResult,
    check_row, gate_run, check_source_value,
    flatten_row, cell_key, cell_kind,
    alone_rates, majority, union_cells, review_queue, consistency,
    deliverable_from_cells,
    build_retry_brief, lint_category_words,
    attr_spec_from_modern,
    DEFAULT_TOP_KEYS, DEFAULT_ALLOW_KEYS, DEFAULT_SOURCE_NAMES,
    FREE_TEXT_FIELDS, PROCESS_FIELDS,
    RESERVED_VALUES, MISSING_KEY, ERR_DATA, ERR_SCHEMA,
)
from .attr_runner import (  # noqa: F401
    Producer, FileProducer, CallableProducer,
    RoundResult, run_round, load_rows, load_run_files,
    DEFAULT_CHUNK,
)
from .catrules import (  # noqa: F401
    CategoryFiles, load_category, load_view, discover_categories, migrate_4to2,
    RULES_SUFFIX, LEDGER_SUFFIX,
)
from .gapfill import (  # noqa: F401
    GapPlan, find_gaps, build_gap_fill_prompt, build_block_gap_prompt,
    build_vision_prompt, merge_candidates, load_attr_notes, GAP_FILL_RULES,
    GAP_FILL_CORE, SOURCE_PRIORITY,
)
from .converge import (  # noqa: F401
    ConvergenceResult, normalize_form, converge_mechanical, convergence_report,
    build_classify_payload, apply_classification,
)
from .onboard import (  # noqa: F401
    build_vocab_review_prompt, apply_vocab_acceptance,
)

__all__ = [
    "AttrSpec", "RuleSource", "GateIssue", "GateResult",
    "check_row", "gate_run", "check_source_value",
    "flatten_row", "cell_key", "cell_kind",
    "alone_rates", "majority", "union_cells", "review_queue", "consistency",
    "deliverable_from_cells",
    "build_retry_brief", "lint_category_words",
    "DEFAULT_TOP_KEYS", "DEFAULT_ALLOW_KEYS", "DEFAULT_SOURCE_NAMES",
    "FREE_TEXT_FIELDS", "PROCESS_FIELDS",
    "RESERVED_VALUES", "MISSING_KEY", "ERR_DATA", "ERR_SCHEMA",
    "Producer", "FileProducer", "CallableProducer",
    "RoundResult", "run_round", "load_rows", "load_run_files",
    "DEFAULT_CHUNK",
    "CategoryFiles", "load_category", "load_view", "discover_categories",
    "migrate_4to2", "RULES_SUFFIX", "LEDGER_SUFFIX",
    "GapPlan", "find_gaps", "build_gap_fill_prompt", "build_block_gap_prompt",
    "build_vision_prompt", "merge_candidates", "load_attr_notes",
    "GAP_FILL_RULES", "GAP_FILL_CORE", "SOURCE_PRIORITY",
    "ConvergenceResult", "normalize_form", "converge_mechanical",
    "convergence_report", "build_classify_payload", "apply_classification",
    "build_vocab_review_prompt", "apply_vocab_acceptance",
]
