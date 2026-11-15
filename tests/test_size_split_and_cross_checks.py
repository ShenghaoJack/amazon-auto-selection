# -*- coding: utf-8 -*-
"""尺寸拆三列 + 跨属性冲突（2026-10-08 用户要求）。

背景：
  · 模型本来就产出 `尺寸_拆列.长/.宽/.高` + `尺寸_依据`，闸门也校验它
    （rules.json 的 top_keys 里就有），但交付表的列只由规格书属性驱动
    → 这些被当"过程字段"丢掉，从没进过交付表。
  · `主体材质=竹木` 却有 28 行 `产品类型=实木砧板`：蕴含式关系抓不到
    （竹木 也配 实木砧板），必须用**互斥**约束。
"""
from __future__ import annotations

import pytest

from steps.step17_attr_annotation import Step17AttrAnnotation
from tools.attr_stability.cross_checks import (
    check_row, check_rows, conflicts_report, load_cross_checks,
)

NORM = Step17AttrAnnotation._norm_size_dim


# --------------------------------------------------------------- 尺寸单位规范化
@pytest.mark.parametrize("raw,expected", [
    ("38.1cm", "38.1cm"),          # 已规范 → 原样
    ("44.5", "44.5cm"),            # 裸数字补单位（实测 49~52 行）
    ("1.5", "1.5cm"),
    ("0.1cm", "0.1cm"),
    ('15"', "38.1cm"),             # 英寸 → cm
    ("12in", "30.48cm"),
    ("5mm", "0.5cm"),              # 毫米 → cm
    ("44.5，36.8，30.5", "44.5，36.8，30.5cm"),   # 多值：单位只写末尾
    ("30.48, 25.4", "30.48，25.4cm"),             # 英文逗号也认，统一输出中文逗号
    ("", ""),
    ("   ", ""),
])
def test_norm_size_dim(raw, expected):
    assert NORM(raw) == expected


@pytest.mark.parametrize("raw", ["大号", "Large", "about 30"])
def test_norm_size_dim_never_guesses(raw):
    """解析不出就原样返回 —— 绝不猜、绝不清空。"""
    assert NORM(raw) == raw


def test_norm_size_dim_removes_sentinel():
    """哨兵不是尺寸值 → 留空（否则交付表里会出现「厚=未明确」这种格子）。"""
    for s in ("未明确", "未标注", "不适用", "未知", "None"):
        assert NORM(s) == ""


def test_norm_size_dim_lone_que_becomes_empty():
    """★ **单独一个 `缺`** 也要留空。

    按提示词规则：「整列一块都没有该维度」应写 `""`；`缺` 只用于
    **多值串里某一块**缺该维度（`43.18，35.56，缺cm`）。
    实测交付表里出现过 `厚=缺`（2 格），与自己的提示词规则矛盾 ——
    是代码审核关卡的「值都以单位结尾」检查抓出来的。
    """
    assert NORM("缺") == ""
    assert NORM("  缺  ") == ""
    # 多值串里的 `缺` 是合法的，必须保留（不能整串清空）
    assert NORM("43.18，35.56，缺") == "43.18，35.56，缺cm"


# --------------------------------------------------------------- 尺寸拆列抽取
class _Res:
    def __init__(self, cells):
        self.cells = cells


def _cell(value, mode=None, kind="value"):
    return {"mode": mode if mode is not None else value, "kind": kind,
            "values": [{"value": value, "by": ["a1"]}]}


def test_extract_maps_gao_to_gao():
    """★ 2026-10-20: 交付第三列由「厚」改为「高」；模型输出的 `高`/`厚` 都归到「高」。"""
    res = _Res({"2|尺寸_拆列.长": _cell("38.1cm"),
                "2|尺寸_拆列.宽": _cell("30.48cm"),
                "2|尺寸_拆列.高": _cell("0.1cm")})
    sm, _ = Step17AttrAnnotation._extract_size_split(res)
    # 键必须是 int：cells 的键是字符串，而交付表写盘用 int 行号
    assert sm == {2: {"长": "38.1cm", "宽": "30.48cm", "高": "0.1cm"}}


def test_extract_keys_are_int():
    """★ 键类型统一成 int —— 否则列建出来也填不进值（实测踩过）。"""
    res = _Res({"7|尺寸_拆列.长": _cell("38.1cm"), "7|尺寸_依据": _cell("依据")})
    sm, basis = Step17AttrAnnotation._extract_size_split(res)
    assert list(sm) == [7] and list(basis) == [7]
    assert all(isinstance(k, int) for k in sm)


def test_extract_falls_back_when_mode_empty():
    """`尺寸_依据` 的 mode 常为空（无多数）→ 取第一个非空提名。"""
    res = _Res({"2|尺寸_依据": {"mode": "", "kind": "free",
                                "values": [{"value": "五点描述15\"x12\""},
                                           {"value": "另一份依据"}]}})
    _, basis = Step17AttrAnnotation._extract_size_split(res)
    assert basis[2] == '五点描述15"x12"'


def test_extract_ignores_unknown_dims():
    res = _Res({"2|尺寸_拆列.长": _cell("38.1cm"),
                "2|尺寸_拆列.斜": _cell("7")})
    sm, _ = Step17AttrAnnotation._extract_size_split(res)
    assert sm == {2: {"长": "38.1cm"}}


def test_extract_normalizes_units():
    res = _Res({"2|尺寸_拆列.长": _cell("44.5")})
    sm, _ = Step17AttrAnnotation._extract_size_split(res)
    assert sm[2]["长"] == "44.5cm"


# --------------------------------------------------------------- 跨属性冲突
DECL = load_cross_checks({"cross_checks": {"互斥": [
    {"说明": "竹子不是实木",
     "左": {"属性": "主体材质", "含": ["竹"]},
     "右": {"属性": "产品类型", "不得含": ["实木", "木"]}},
    {"说明": "木质不是竹",
     "左": {"属性": "主体材质", "含": ["木"], "不得含": ["竹"]},
     "右": {"属性": "产品类型", "不得含": ["竹"]}},
]}})


def test_decl_loaded():
    assert list(DECL) == ["互斥"] and len(DECL["互斥"]) == 2


def test_conflict_detected():
    hits = check_row({"主体材质": "竹木", "产品类型": "实木砧板"}, DECL, 2)
    assert len(hits) == 1
    assert hits[0]["左值"] == "竹木" and hits[0]["右值"] == "实木砧板"


def test_consistent_row_not_flagged():
    assert check_row({"主体材质": "竹木", "产品类型": "竹制砧板"}, DECL) == []
    assert check_row({"主体材质": "柚木", "产品类型": "实木砧板"}, DECL) == []
    assert check_row({"主体材质": "实木", "产品类型": "实木砧板"}, DECL) == []


def test_combo_value_is_matched_by_component():
    """组合值按分量匹配：`钛金属+小麦秸秆PP` 里既没有「竹」也没有「木」，
    所以本文件的 2 条规则不该命中它；真正管它的是 shipped rules.json 里的「金属」规则。"""
    assert check_row({"主体材质": "钛金属+小麦秸秆PP", "产品类型": "不锈钢砧板"}, DECL) == []
    assert check_row({"主体材质": "钛金属+小麦秸秆PP", "产品类型": "实木砧板"}, DECL) == []


def test_shipped_rules_declaration_catches_real_conflicts():
    """锁住**真正发布的那份声明**。

    2026-10-08 起这份声明**不再是人手写的**，而是步骤16 用 LLM 从属性集提出、
    并**用上一轮真实交付表过检**（只采纳 0 命中的）后写入的。所以本测试改为锁：
      ① 声明确实是「步骤16 生成」的（`_来源`）；
      ② 竹制 ↔ 实木 这类硬矛盾抓得到；
      ③ 组合值（双面不同材质）**不许误报** —— 这正是 LLM 第一版提错的地方。
    """
    import json
    from pathlib import Path

    rp = (Path(__file__).resolve().parents[1] / "data" / "Cutting Boards-US-202608"
          / "cutting_boards.rules.json")
    rules_obj = json.loads(rp.read_text(encoding="utf-8-sig"))
    shipped = load_cross_checks(rules_obj)
    assert shipped, "rules.json 里必须带 cross_checks 声明"
    assert "步骤16" in str((rules_obj.get("cross_checks") or {}).get("_来源") or ""), \
        "声明必须是步骤16 产出的；若人工改过请把 _来源 标成「人工」"

    # ② 硬矛盾必抓（用**收敛后**的受控值 —— 交付表里就是这些）
    assert check_row({"主体材质": "竹制", "产品类型": "实木砧板"}, shipped)
    assert check_row({"主体材质": "木制", "产品类型": "竹制砧板"}, shipped)

    # ③ 组合值不许误报：`不锈钢+塑料` 是双面不同材质，完全合法
    for m, t in (("竹制", "竹制砧板"), ("木制", "实木砧板"), ("不锈钢", "不锈钢砧板"),
                 ("塑料", "塑料软砧板垫"), ("不锈钢+塑料", "不锈钢砧板"),
                 ("钛金属", "钛金属砧板"), ("木制+树脂", "拼盘托盘")):
        assert check_row({"主体材质": m, "产品类型": t}, shipped) == [], (m, t)


def test_empty_and_sentinel_never_conflict():
    """空/哨兵不判 —— 否则会把「没填」误判成「填错」。"""
    for v in ("", "未明确", "未标注", "不适用", "未知", None):
        assert check_row({"主体材质": v, "产品类型": "实木砧板"}, DECL) == []
        assert check_row({"主体材质": "竹木", "产品类型": v}, DECL) == []


def test_gongzhu_rule_needs_non_bamboo():
    """`木` 规则必须排除竹：竹木不算「木质」。"""
    assert check_row({"主体材质": "竹木", "产品类型": "实木砧板"}, DECL)[0]["说明"] == "竹子不是实木"
    hits = check_row({"主体材质": "柚木", "产品类型": "竹制砧板"}, DECL)
    assert len(hits) == 1 and hits[0]["说明"] == "木质不是竹"


def test_check_rows_and_report():
    deliv = [{"xl_row": 2, "attributes": {"主体材质": "竹木", "产品类型": "实木砧板"}},
             {"xl_row": 3, "attributes": {"主体材质": "竹木", "产品类型": "竹制砧板"}}]
    hits = check_rows(deliv, DECL)
    rep = conflicts_report(hits)
    assert rep["冲突总数"] == 1
    assert rep["按属性对"] == {"主体材质↔产品类型": 1}
    assert rep["明细"][0]["行号"] == 2


def test_bad_declaration_is_dropped():
    assert load_cross_checks(None) == {}
    assert load_cross_checks({}) == {}
    assert load_cross_checks({"cross_checks": "不是对象"}) == {}
    assert load_cross_checks({"cross_checks": {"互斥": [
        {"左": {"属性": "A"}, "右": {"属性": "B"}},        # 缺 含/不得含
        {"左": {"属性": "A", "含": ["x"]}},                # 缺右
        "不是对象",
    ]}}) == {}


# ------------------------------------------------- 端到端：真的写进 xlsx 了吗
def test_write_deliverables_actually_fills_size_columns(tmp_path):
    """★ 端到端回归：列建出来还不够，**值必须真的落进去**。

    实测踩过的坑：`size_map` 的键来自 cells（字符串 `"2"`），而写盘循环用 `row_by` 的
    整数行号 `2` → `.get()` 全部落空，三列建好了却一格值都没有。
    单看「列存在」的测试抓不到，必须读回 xlsx 看值。
    """
    import json
    import os
    import sys

    import openpyxl

    from core.config import Config
    from core.paths import Paths
    from steps.base import StepContext
    from steps.step17_attr_annotation import Step17AttrAnnotation

    root = str(tmp_path)
    cfg = Config({"CAT": "t", "DATA_ROOT": root, "DRY_RUN": True, "MARKET": "amazon.com"})
    ctx = StepContext(cfg, Paths(cfg))
    os.makedirs(ctx.paths.cat_root, exist_ok=True)

    # cells 的键是**字符串**行号（与真实 稳定性结果.json 一致）
    cells = {
        "2|尺寸_拆列.长": _cell("38.1cm"),
        "2|尺寸_拆列.宽": _cell("30.48cm"),
        "2|尺寸_拆列.高": _cell("1cm"),
        "2|尺寸_依据": {"mode": "", "kind": "free",
                        "values": [{"value": "详细参数17x12x1in，换算cm"}]},
    }
    res = _Res(cells)
    res.deliverable = [{"xl_row": 2, "attributes": {"尺寸规格": "38.1x30.48x1 cm"}}]
    rows = [{"xl_row": 2, "父ASIN": "B0TEST", "标题": "t"}]

    st = Step17AttrAnnotation()
    st._cat = "t"
    out_dir = os.path.join(root, "t", "18_标准化")
    os.makedirs(out_dir, exist_ok=True)
    st._write_deliverables(ctx, out_dir, rows, res, {}, raw_too=False)

    f = os.path.join(out_dir, "t-属性标注-标准化交付.xlsx")
    wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
    assert "来源" in wb.sheetnames
    ws = wb["属性标注"]
    it = ws.iter_rows(values_only=True)
    h = [str(x or "") for x in next(it)]
    rec = next(it)
    got = dict(zip(h, rec))
    wb.close()

    assert "尺寸规格" in h and "长" in h and "宽" in h and "高" in h
    # 列序：紧跟 尺寸规格
    assert h[h.index("尺寸规格") + 1:h.index("尺寸规格") + 4] == ["长", "宽", "高"]
    # ★ 关键断言：值真的在
    assert got["长"] == "38.1cm", got
    assert got["宽"] == "30.48cm", got
    assert got["高"] == "1cm", got

    # 来源 sheet 里三列共用 `尺寸_依据`
    wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
    ws2 = wb["来源"]
    it2 = ws2.iter_rows(values_only=True)
    h2 = [str(x or "") for x in next(it2)]
    r2 = next(it2)
    wb.close()
    assert r2[h2.index("长")] == "详细参数17x12x1in，换算cm"


def test_write_deliverables_registers_cross_conflicts(tmp_path):
    """声明命中时要落 `跨属性冲突.json`，且**不修改值**。"""
    import json
    import os
    from pathlib import Path

    from core.config import Config
    from core.paths import Paths
    from steps.base import StepContext
    from steps.step17_attr_annotation import Step17AttrAnnotation

    root = str(tmp_path)
    cat_root = Path(root) / "t"
    cat_root.mkdir(parents=True, exist_ok=True)
    (cat_root / "t.rules.json").write_text(json.dumps({
        "category": "t",
        "cross_checks": {"互斥": [
            {"说明": "竹子不是实木",
             "左": {"属性": "主体材质", "含": ["竹"]},
             "右": {"属性": "产品类型", "不得含": ["实木"]}}]},
        "attributes": [],
    }, ensure_ascii=False), encoding="utf-8")

    cfg = Config({"CAT": "t", "DATA_ROOT": root, "DRY_RUN": True, "MARKET": "amazon.com"})
    ctx = StepContext(cfg, Paths(cfg))
    res = _Res({})
    res.deliverable = [{"xl_row": 2, "attributes": {"主体材质": "竹木", "产品类型": "实木砧板"}}]
    rows = [{"xl_row": 2, "父ASIN": "B0TEST", "标题": "t"}]
    st = Step17AttrAnnotation()
    st._cat = "t"
    out_dir = os.path.join(root, "t", "18_标准化")
    os.makedirs(out_dir, exist_ok=True)

    n = st._register_cross_conflicts(ctx, out_dir, res)
    assert n == 1
    rep = json.loads((Path(out_dir) / "跨属性冲突.json").read_text(encoding="utf-8"))
    assert rep["冲突总数"] == 1
    assert rep["明细"][0]["行号"] == 2
