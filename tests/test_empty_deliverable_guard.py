# 单元测试: 「交付为空」硬拦 (2026-09-29)
#
# 背景(真实事故): Salt and Pepper 2026-09-29 —— 步骤17 的 a1 在**最后一批** LLM 超时
# 被整个丢弃、a2 过闸门失败, 于是 `run_round` 因「通过闸门的运行不足 2 个」提前返回
# **空交付**(deliverable=[] / cells={}); 步骤18 读到「0 属性」仍照常写交付表 —— 写出去的
# 是**上游原始表**(78 个输入列、0 个属性列), 而流水线报「全部步骤完成」。
#
# 为什么之前没拦住: 步骤17 ⑧ 的唯一硬拦判据是 `if not res.passed`, 而
# `strict_gate=False` 下 `res.passed` 的含义是「产出成功的运行」而非「过闸门的运行」,
# 因此 1 份产出即通过; deliverable 是否为空**从来没有被检查过**。
from __future__ import annotations

import json
import os

import pytest

from core.errors import StepDataError
import steps.step17_attr_annotation as s17
import steps.step18_standardization as s18


class _Cfg:
    cat = "Unit Test Cat-US-202608"


class _Ctx:
    """最小 StepContext 替身: 只需 config.cat 与 resolve_artifact。"""

    def __init__(self, root: str):
        self.config = _Cfg()
        self._root = root

    def resolve_artifact(self, name: str) -> str:
        return os.path.join(self._root, name)


def _write_result(root: str, deliverable) -> None:
    d = os.path.join(root, s18.STEP17_OUT_DIR)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, s18.RESULT_NAME), "w", encoding="utf-8") as fh:
        json.dump({"cells": {}, "deliverable": deliverable,
                   "vocab_candidates": []}, fh, ensure_ascii=False)


def test_step18_rejects_empty_deliverable(tmp_path):
    """步骤17 交付为空 → 步骤18 必须拒绝, 而不是写出空交付表。"""
    _write_result(str(tmp_path), deliverable=[])
    with pytest.raises(StepDataError) as ei:
        s18.Step18Standardization().run(_Ctx(str(tmp_path)))
    assert "交付为空" in str(ei.value)


def test_step18_does_not_create_output_dir_on_empty_deliverable(tmp_path):
    """空交付时不应留下 18_标准化 目录(空中不能当成"跑过了")。"""
    _write_result(str(tmp_path), deliverable=[])
    with pytest.raises(StepDataError):
        s18.Step18Standardization().run(_Ctx(str(tmp_path)))
    assert not os.path.exists(os.path.join(str(tmp_path), s18.STD_OUT_DIR))


def test_step18_passes_guard_when_deliverable_present(tmp_path):
    """有交付内容时**不能**被这道新闸门拦住(负向对照)。

    后续会因为缺少 rules.json / input.json 等继续报错, 但**不应**报「交付为空」。
    """
    _write_result(str(tmp_path), deliverable=[{"xl_row": 2, "attributes": {"材质": "竹木"}}])
    with pytest.raises(Exception) as ei:      # noqa: PT011 - 任何后续错误都可以
        s18.Step18Standardization().run(_Ctx(str(tmp_path)))
    assert "交付为空" not in str(ei.value)


# ---------------- 步骤17 ⑧ 的判据契约(源码级, 与 tests/test_check_contract.py 同法)
def _src() -> str:
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "steps", "step17_attr_annotation.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_step17_guards_on_deliverable_not_only_passed():
    """⑧ 必须按 `res.deliverable` 判定, 不能只依赖 `res.passed`。

    `res.passed` 在 strict_gate=False 下是「产出成功的运行」, 非空**不代表**有交付内容。
    """
    src = _src()
    assert "if not res.deliverable:" in src, "缺少「交付为空」硬拦"
    assert "闸门后无可交付内容" in src, "缺少可读的失败原因"
    # 判据必须来自 deliverable, 而不是拿 passed 的数量当交付量
    i_deliv = src.index("if not res.deliverable:")
    i_passed = src.index("if not res.passed:")
    assert i_deliv > i_passed, "两条硬拦的顺序应保持：先判 LLM 全失败, 再判交付为空"


def test_step17_reports_gate_passed_count_in_message():
    """失败信息要能区分「产出成功几份」和「过闸门几份」, 否则排查时看不出根因。"""
    src = _src()
    assert "gate_passed" in src
    assert "过闸门 %d 份" in src
