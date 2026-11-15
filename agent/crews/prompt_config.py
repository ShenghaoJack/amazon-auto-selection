"""提示词外置(Phase 4/5)加载器。

作用: 让报告/问答 crew 的角色、任务、路由、类别、工具说明从 **YAML 配置**读取,
人工可直接编辑而无需改 Python。

优先级(低 → 高):
    1. 代码内建默认 `DEFAULT_*`(agent/crews/agents.py)
    2. 全局配置  `agent/prompts.yaml`
    3. 类目覆盖(可选) `data/<CAT>/prompts.yaml`

规则:
- 字段级覆盖: YAML 里只写你要改的字段, 其余自动回退默认(不会整份替换);
- 列表按 key(角色) / agent_key(任务) / category key(类别) 合并, 可新增条目;
- YAML 缺失/解析失败/结构不合法 → 该节回退默认, 不抛错(保证稳健)。

用法:
    from agent.crews.prompt_config import effective_report_roles, effective_qa_categories
    roles = effective_report_roles(cat)          # dicts
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import agents as _agents

# 全局提示词文件(agent/prompts.yaml)
USER_PROMPT_PATH: Path = Path(__file__).resolve().parent.parent / "prompts.yaml"


# ---------------------------------------------------------------- 读取
def _load_yaml(path) -> dict:
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}
    except OSError:
        return {}


def _deep_merge(base: dict, over: dict) -> dict:
    """dict 深合并(over 覆盖 base)。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_prompts(cat: str | None = None, global_path=None, data_root: str = "data") -> dict:
    """读取(全局 + 可选类目覆盖)提示词, 返回原始 dict。"""
    data = _load_yaml(global_path or USER_PROMPT_PATH)
    if cat:
        cat_path = Path(data_root) / cat / "prompts.yaml"
        over = _load_yaml(cat_path)
        if over:
            data = _deep_merge(data, over)
    return data


# ---------------------------------------------------------------- 合并
def _merge_list_by_key(defaults: list[dict], overrides, key_field: str) -> list[dict]:
    """按 key_field 合并列表: 覆盖已有项字段、追加 YAML 新增项、保持默认顺序。"""
    if not isinstance(overrides, list):
        return defaults
    by_key = {d.get(key_field): dict(d) for d in defaults if d.get(key_field)}
    order = [d.get(key_field) for d in defaults if d.get(key_field)]
    for ov in overrides:
        if not isinstance(ov, dict):
            continue
        k = ov.get(key_field)
        if not k:
            continue
        patch = {kk: vv for kk, vv in ov.items() if vv is not None}
        if k in by_key:
            by_key[k] = {**by_key[k], **patch}
        else:
            by_key[k] = dict(ov)
            order.append(k)
    return [by_key[k] for k in order if k in by_key]


def effective_report_roles(cat: str | None = None, global_path=None, data_root: str = "data") -> list[dict]:
    return _merge_list_by_key(_agents.DEFAULT_REPORT_ROLES,
                              load_prompts(cat, global_path, data_root).get("report_roles"), "key")


def effective_report_tasks(cat: str | None = None, global_path=None, data_root: str = "data") -> list[dict]:
    return _merge_list_by_key(_agents.DEFAULT_REPORT_TASKS,
                              load_prompts(cat, global_path, data_root).get("report_tasks"), "agent_key")


def effective_qa_router(cat: str | None = None, global_path=None, data_root: str = "data") -> dict:
    over = load_prompts(cat, global_path, data_root).get("qa_router")
    if isinstance(over, dict):
        return {**_agents.DEFAULT_QA_ROUTER_ROLE, **{k: v for k, v in over.items() if v is not None}}
    return dict(_agents.DEFAULT_QA_ROUTER_ROLE)


def effective_qa_categories(cat: str | None = None, global_path=None, data_root: str = "data") -> list[dict]:
    return _merge_list_by_key(_agents.DEFAULT_QA_CATEGORIES,
                              load_prompts(cat, global_path, data_root).get("qa_categories"), "key")


def effective_tool_descriptions(cat: str | None = None, global_path=None, data_root: str = "data") -> dict:
    """返回 {工具名: 描述} 覆盖(供 build_tools 之外挂化工具说明用)。"""
    raw = load_prompts(cat, global_path, data_root).get("tool_descriptions")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


# ---------------------------------------------------------------- 组合
def build_prompt_spec(cat: str | None = None, data_root: str = "data") -> dict:
    """一次取回全部生效提示词规格。"""
    return {
        "report_roles": effective_report_roles(cat, data_root=data_root),
        "report_tasks": effective_report_tasks(cat, data_root=data_root),
        "qa_router": effective_qa_router(cat, data_root=data_root),
        "qa_categories": effective_qa_categories(cat, data_root=data_root),
        "tool_descriptions": effective_tool_descriptions(cat, data_root=data_root),
    }
