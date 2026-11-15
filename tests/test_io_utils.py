# 单元测试: 文件 I/O 工具
import json
import os

from core import io_utils


def test_append_jsonl_dedup(ws_tmp):
    path = os.path.join(ws_tmp, "a.jsonl")
    n1, s1 = io_utils.append_jsonl(path, [{"k": "1", "v": "a"}, {"k": "2", "v": "b"}], dedup_key="k")
    assert (n1, s1) == (2, 0)
    n2, s2 = io_utils.append_jsonl(path, [{"k": "2", "v": "b2"}, {"k": "3", "v": "c"}], dedup_key="k")
    assert (n2, s2) == (1, 1)
    records = io_utils.read_jsonl(path)
    assert [r["k"] for r in records] == ["1", "2", "3"]


def test_read_jsonl_tolerates_bad_lines(ws_tmp):
    path = os.path.join(ws_tmp, "bad.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"ok": true}\n')
        fh.write("this is not json\n")
        fh.write('{"ok": false}\n')
    records = io_utils.read_jsonl(path)
    assert len(records) == 2


def _write_bom(path: str, text: str) -> None:
    """按记事本 / PowerShell 5.1 `Set-Content -Encoding utf8` 的方式写(带 UTF-8 BOM)。"""
    with open(path, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write(text)


def test_read_txt_lines_strips_bom(ws_tmp):
    """带 BOM 的注入词/清单文件: 第一条不得带 \\ufeff(否则注入词匹配不上)。"""
    path = os.path.join(ws_tmp, "inject.txt")
    _write_bom(path, "grinder\nmill\n")
    lines = io_utils.read_txt_lines(path)
    assert lines == ["grinder", "mill"]
    assert not lines[0].startswith("\ufeff")


def test_read_json_strips_bom(ws_tmp):
    """带 BOM 的 JSON 不得静默回落 default。"""
    path = os.path.join(ws_tmp, "s.json")
    _write_bom(path, '{"a": 1}')
    assert io_utils.read_json(path, default={}) == {"a": 1}


def test_read_jsonl_strips_bom_first_line(ws_tmp):
    """带 BOM 的 JSONL: 第一条记录不能被当成坏行丢掉。"""
    path = os.path.join(ws_tmp, "b.jsonl")
    _write_bom(path, '{"k": "1"}\n{"k": "2"}\n')
    assert [r["k"] for r in io_utils.read_jsonl(path)] == ["1", "2"]


def test_writers_emit_no_bom(ws_tmp):
    """回归: 写出侧不得引入 BOM(否则下游用 utf-8 读会再次踩坑)。"""
    p1 = os.path.join(ws_tmp, "w.txt")
    io_utils.write_txt_lines(p1, ["a", "b"])
    p2 = os.path.join(ws_tmp, "w.json")
    io_utils.write_json(p2, {"k": "v"})
    p3 = os.path.join(ws_tmp, "w.jsonl")
    io_utils.append_jsonl(p3, [{"k": "v"}])
    for p in (p1, p2, p3):
        with open(p, "rb") as fh:
            assert fh.read(3) != b"\xef\xbb\xbf", f"{p} 不应带 BOM"


def test_merge_excel_align_columns(ws_tmp):
    import pandas as pd
    d1 = os.path.join(ws_tmp, "x")
    os.makedirs(d1, exist_ok=True)
    pd.DataFrame({"A": [1], "B": [2]}).to_excel(os.path.join(d1, "f1.xlsx"), index=False)
    pd.DataFrame({"B": [3], "C": [4]}).to_excel(os.path.join(d1, "f2.xlsx"), index=False)
    out = os.path.join(d1, "merged.xlsx")
    merged = io_utils.merge_excel_by_rows(d1, out, exclude_names=["merged.xlsx"])
    assert merged is not None
    assert set(merged.columns) >= {"A", "B"}
    assert len(merged) == 2


def test_merge_excel_respects_locked_target(ws_tmp, monkeypatch):
    """合并写出必须经 _unique_if_locked(目标被 Excel 打开时换时间戳名), 不得直接覆盖写。

    回归 2026-09-13: 原实现 `merged.to_excel(out_path)` 绕过占用保护 —— 用户开着交付物重跑
    步骤11/13 时会 PermissionError 崩掉整轮, 且非原子写有毁掉上一版交付物的风险。
    """
    import pandas as pd

    d1 = os.path.join(ws_tmp, "merge_locked")
    os.makedirs(d1, exist_ok=True)
    pd.DataFrame({"A": [1]}).to_excel(os.path.join(d1, "f1.xlsx"), index=False)
    out = os.path.join(d1, "merged.xlsx")
    alt = os.path.join(d1, "merged-20260101-000000.xlsx")   # 模拟"被占用 → 换时间戳名"
    monkeypatch.setattr(io_utils, "_unique_if_locked", lambda p: alt)
    merged = io_utils.merge_excel_by_rows(d1, out, exclude_names=["merged.xlsx"])
    assert merged is not None and len(merged) == 1
    assert os.path.exists(alt), "被占用时应写到换名后的目标"
    assert not os.path.exists(out), "不得写原目标"


def test_dedup_keep_best(ws_tmp):
    import pandas as pd
    df = pd.DataFrame(
        [
            {"parent": "P1", "variant": "V1", "title": "short", "price": None},
            {"parent": "P1", "variant": "V1", "title": "a much longer and more complete title", "price": 12.5},
        ]
    )
    out = io_utils.dedup_keep_best(df, keys=["parent", "variant"], weight_cols=["title"])
    assert len(out) == 1
    assert out.iloc[0]["price"] == 12.5


def test_write_json_atomic(ws_tmp):
    path = os.path.join(ws_tmp, "j.json")
    io_utils.write_json(path, {"a": [1, 2], "中文": "值"})
    assert json.load(open(path, encoding="utf-8"))["中文"] == "值"
