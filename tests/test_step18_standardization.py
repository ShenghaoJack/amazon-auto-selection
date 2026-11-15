# 单元测试: 步骤18 标准化 —— 视觉补充并进 cells 一起收敛(2026-09-23)。
#
# 背景(用户指出): 视觉补充的值是**原始候选值**(可能是原文片段, 不必是受控值),
# 不能绕过标准化直接 std_map.update(vision) 塞进交付表 —— 否则原文片段不会被收敛成受控值。
# 正确做法: 视觉值注入 cells 作为额外提名, 与文本标注一起走 converge_mechanical + 语义归类。
# 新 schema(2026-09-24): 值格路径 = attr.<属性>, 来源格 = attr.<属性>_来源。
from steps.step18_standardization import Step18Standardization


def test_merge_vision_into_existing_cell_appends_nomination():
    """视觉值注入已有 cell: 作为额外提名追加(values 列表), 来源标记 vision, 去重。"""
    res = type("Res", (), {"cells": {
        "2|attr.材质": {"kind": "value", "values": [
            {"value": "未明确", "by": ["a1", "a2"]},
        ], "n_votes": 1},
    }})()
    Step18Standardization._merge_vision_into_cells(res, {(2, "材质"): "陶瓷"})

    cell = res.cells["2|attr.材质"]
    vals = [g["value"] for g in cell["values"]]
    assert "未明确" in vals and "陶瓷" in vals      # 视觉值作为新提名追加
    assert cell["values"][-1]["by"] == ["vision"]    # 来源标记
    assert cell["n_votes"] == 2


def test_merge_vision_skips_duplicate_value():
    """视觉值与已有提名相同 → 不重复追加。"""
    res = type("Res", (), {"cells": {
        "3|attr.颜色": {"kind": "value", "values": [{"value": "黑色", "by": ["a1"]}]},
    }})()
    Step18Standardization._merge_vision_into_cells(res, {(3, "颜色"): "黑色"})
    assert len(res.cells["3|attr.颜色"]["values"]) == 1


def test_merge_vision_creates_cell_when_absent():
    """文本侧无此格(极端情况) → 新建只含视觉提名的 cell。"""
    res = type("Res", (), {"cells": {}})()
    Step18Standardization._merge_vision_into_cells(res, {(5, "材质"): "不锈钢"})

    cell = res.cells["5|attr.材质"]
    assert cell["kind"] == "value"
    assert cell["values"] == [{"value": "不锈钢", "by": ["vision"]}]


def test_merge_vision_injects_source_cell():
    """视觉来源注入 attr.<属性>_来源 格(free 类)。"""
    res = type("Res", (), {"cells": {}})()
    Step18Standardization._merge_vision_into_cells(
        res, {(5, "材质"): "不锈钢"}, {(5, "材质"): "图片视觉"})

    scell = res.cells["5|attr.材质_来源"]
    assert scell["kind"] == "free"
    assert scell["values"] == [{"value": "图片视觉", "by": ["vision"]}]
