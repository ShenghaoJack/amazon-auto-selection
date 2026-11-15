"""提示词外置测试(prompt_config)。

验证: 从 YAML 读取、字段级覆盖、列表可新增、缺失/解析失败回退默认、工具说明。

注: 临时 YAML 写到工作区下的独立目录(沙箱下 OS TEMP 不可写, pytest tmp_path 亦然)。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from agent.crews import prompt_config as pc

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def prompt_tmp():
    d = Path(ROOT) / f"__agent_prompt_test__{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield str(d)
    shutil.rmtree(d, ignore_errors=True)


def _write(dirpath, name, text) -> str:
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def test_effective_report_roles_default(prompt_tmp):
    roles = pc.effective_report_roles(global_path=_write(prompt_tmp, "p.yaml", ""))
    assert [r["key"] for r in roles] == ["researcher", "keyword", "market", "lead"]
    assert roles[0]["role"] == "类目数据研究员"


def test_effective_report_tasks_default(prompt_tmp):
    tasks = pc.effective_report_tasks(global_path=_write(prompt_tmp, "p.yaml", ""))
    assert [t["agent_key"] for t in tasks] == ["researcher", "keyword", "market", "lead"]


def test_effective_qa_default(prompt_tmp):
    router = pc.effective_qa_router(global_path=_write(prompt_tmp, "p.yaml", ""))
    assert router["role"] == "问答路由"
    cats = pc.effective_qa_categories(global_path=_write(prompt_tmp, "p.yaml", ""))
    assert [c["key"] for c in cats] == ["kpi", "keyword", "market", "general"]


def test_field_level_override(prompt_tmp):
    y = _write(prompt_tmp, "p.yaml", """report_roles:
  - key: lead
    goal: 只给结论, 100字内。
""")
    roles = pc.effective_report_roles(global_path=y)
    lead = next(r for r in roles if r["key"] == "lead")
    assert lead["goal"] == "只给结论, 100字内。"            # 被覆盖
    assert "最终决策者" in lead["backstory"]                # 保留默认 backstory
    assert lead["tool_names"] == []                        # 保留默认 tool_names
    rs = next(r for r in roles if r["key"] == "researcher")
    assert rs["goal"] == "从交付产物中提取准确、可引用的核心指标"  # 其它角色不受影响


def test_add_role_in_yaml(prompt_tmp):
    y = _write(prompt_tmp, "p.yaml", """report_roles:
  - key: risk
    role: 风险分析师
    goal: assess risk
    backstory: x
    tool_names: [get_monitoring]
""")
    roles = pc.effective_report_roles(global_path=y)
    keys = [r["key"] for r in roles]
    assert "risk" in keys and "researcher" in keys
    assert keys.index("risk") == len(keys) - 1              # 新增追加到末尾


def test_override_tool_descriptions(prompt_tmp):
    y = _write(prompt_tmp, "p.yaml", """tool_descriptions:
  get_kpis: "返回该类目核心KPI。"
""")
    desc = pc.effective_tool_descriptions(global_path=y)
    assert desc["get_kpis"] == "返回该类目核心KPI。"


def test_missing_yaml_returns_defaults(prompt_tmp):
    roles = pc.effective_report_roles(global_path=os.path.join(prompt_tmp, "no.yaml"))
    assert len(roles) == 4


def test_bad_yaml_returns_defaults(prompt_tmp):
    y = _write(prompt_tmp, "bad.yaml", "not: [valid: yaml")
    roles = pc.effective_report_roles(global_path=y)
    assert len(roles) == 4                                 # 回退默认, 不抛错


def test_build_prompt_spec():
    spec = pc.build_prompt_spec("salt-pepper-shaker-sets")
    for key in ("report_roles", "report_tasks", "qa_router", "qa_categories", "tool_descriptions"):
        assert key in spec
    assert len(spec["report_roles"]) == 4
