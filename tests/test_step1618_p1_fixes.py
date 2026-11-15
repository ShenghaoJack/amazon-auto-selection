# -*- coding: utf-8 -*-
"""P1(精简版)修复的回归测试(2026-10-14)。

只覆盖"不做就会出错"的 4 项 + 3 项文案一致性:
  · B1' `_save_workbook_atomic` / `write_json_atomic` —— 定义层与交付前文件不留半成品
  · B2  步骤18 上游新鲜度(definition_revision / source_sha256)
  · B3  步骤17 标注必需列缺失 → 硬失败
  · A4  `STEP_DIR_NAMES[16]` 与 step16 实际落盘目录一致
  · C2  契约表: `18_标准化/词表聚类报告.json` 归属步骤18(实际写出者 17/归档者 18)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Config                                            # noqa: E402
from core.errors import StepDataError                                     # noqa: E402
from core.paths import STEP_DIR_NAMES, Paths                              # noqa: E402
from steps.base import StepContext                                        # noqa: E402
from steps.step16_attribute_annotation import Step16AttributeAnnotation   # noqa: E402
from steps.step17_attr_annotation import (                                # noqa: E402
    REQUIRED_INPUT_FIELDS,
    Step17AttrAnnotation,
    UPSTREAM_SPLIT,
    _save_workbook_atomic,
)
from steps.step18_standardization import Step18Standardization            # noqa: E402


def _ctx(tmp_dir, extra=None) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": str(tmp_dir), "DRY_RUN": True, "MARKET": "amazon.com"}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    paths.cat_root.mkdir(parents=True, exist_ok=True)
    return StepContext(cfg, paths)


# --------------------------------------------------------------- B1' 原子写
def test_save_workbook_atomic_roundtrip(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "属性标注"
    ws.append(["a", "b"])
    ws.append([1, 2])
    target = tmp_path / "sub" / "x.xlsx"
    _save_workbook_atomic(wb, str(target))
    assert target.is_file()
    chk = openpyxl.load_workbook(target)
    assert chk["属性标注"].max_row == 2
    chk.close()
    assert not list(target.parent.glob("*.tmp")), "临时文件必须被清理"


def test_save_workbook_atomic_keeps_old_file_on_failure(tmp_path):
    """保存中途失败时: 原文件必须**保持不动**, 且不残留临时文件(不留半成品)。"""
    target = tmp_path / "x.xlsx"
    good = openpyxl.Workbook()
    good.active.append(["ok"])
    _save_workbook_atomic(good, str(target))
    original = target.read_bytes()

    class _Boom:
        def save(self, path):
            Path(path).write_bytes(b"partial-garbage")     # 模拟写了半截
            raise OSError("disk full")

    with pytest.raises(OSError):
        _save_workbook_atomic(_Boom(), str(target))
    assert target.read_bytes() == original, "失败时不得覆盖原文件"
    assert not list(tmp_path.glob("*.tmp")), "失败后不得残留临时文件"


def test_write_json_atomic_leaves_valid_json(tmp_path):
    from tools.attr_stability.spec_io import write_json_atomic
    p = tmp_path / "rules.json"
    write_json_atomic(str(p), {"category": "t", "attributes": []})
    assert json.loads(p.read_text(encoding="utf-8"))["category"] == "t"


# --------------------------------------------------------------- B3 必需列
def test_required_input_fields_is_the_five_columns():
    assert REQUIRED_INPUT_FIELDS == ("标题", "五点描述", "详细参数", "图片链接", "SKU")


def _write_split_xlsx(path: Path, cols: list[str]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AI已补充"
    ws.append(cols)
    ws.append(["v"] * len(cols))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def test_load_rows_rejects_missing_required_column(tmp_path):
    ctx = _ctx(tmp_path)
    cols = ["父ASIN", "标题", "五点描述", "详细参数", "图片链接"]        # 缺 SKU
    _write_split_xlsx(Path(ctx.paths.cat_root) / UPSTREAM_SPLIT, cols)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    with pytest.raises(StepDataError) as ei:
        st._load_rows(ctx, str(tmp_path / "out"))
    assert "SKU" in str(ei.value) and "必需列" in str(ei.value)


def test_load_rows_accepts_complete_header(tmp_path):
    ctx = _ctx(tmp_path)
    cols = ["父ASIN", *REQUIRED_INPUT_FIELDS, "size"]
    _write_split_xlsx(Path(ctx.paths.cat_root) / UPSTREAM_SPLIT, cols)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    rows = st._load_rows(ctx, str(tmp_path / "out"))
    assert rows and rows[0]["SKU"] == "v"


# --------------------------------------------------------------- B2（已撤销）
# 原计划在 `validate_upstream` 里加 definition_revision/source_sha256 新鲜度校验, 但复核发现
# **步骤18 本来就有**陈旧检测: `Step17AttrAnnotation._verify_result_contract()`(step17:277-291,
# 内部走 `verify_annotation_contract`)已校验 spec 状态 + annotation_contract 的
# completed/spec_sha256/input_sha256/source_sha256 + 交付行一致性, 且
# `tests/test_step16_18_attribute_lifecycle.py::test_step18_rejects_stale_or_incomplete_annotation`
# 已覆盖 add/upstream/legacy/unfinished/tampered 五种突变。
# 因此不再重复实现(2026-10-14, 用户口径: 不过度复杂化) —— 该行为由既有测试守护。


# --------------------------------------------------------------- A4 目录名
def test_step16_dir_name_matches_realtime_dir(tmp_path):
    ctx = _ctx(tmp_path)
    st = Step16AttributeAnnotation()
    st.bind_cat(ctx)
    assert STEP_DIR_NAMES[16] == "16_产品属性标注", "paths 表必须写实际落盘目录名"
    assert st.archive_dirs == [STEP_DIR_NAMES[16]], "归档目录名与 paths 表要一致"


# --------------------------------------------------------------- C2 契约归属
def test_contract_puts_vocab_report_under_step18():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cc", str(Path(__file__).resolve().parents[1] / "scripts" / "check_contract.py"))
    cc = importlib.util.module_from_spec(spec)                            # type: ignore[arg-type]
    spec.loader.exec_module(cc)                                           # type: ignore[union-attr]
    assert "18_标准化/词表聚类报告.json" in cc.CONTRACT["18"]
    assert "18_标准化/词表聚类报告.json" not in cc.UPSTREAM["17"], \
        "词表聚类报告由步骤17 写出、步骤18 读入, 不该登记为步骤17 的输入"
