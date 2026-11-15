"""Declared rules must execute through step 18 and reach the saved workbook."""
import json
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step17_attr_annotation import Step17AttrAnnotation, UPSTREAM_SPLIT
from steps.step18_standardization import Step18Standardization
from tools.attr_stability.converge import normalize_measure
from tools.attr_stability.rule_pipeline import rule_configs
from tools.attr_stability.std_rules import ST_REVIEW, apply_rule


@pytest.mark.parametrize("raw,unit,expected", [
    ("1.2L", "ml", "1200ml"), ("1kg", "g", "1000g"),
    ("2inch", "cm", "5.08cm"), ("10mph", "m/s", "4.4704m/s"),
    ("2千瓦", "W", "2000W"), ("1.5Ah", "mAh", "1500mAh"),
    ("2m³/min", "m3/h", "120m3/h"), ("1,5cm", "mm", "15mm"),
    ("0.5-0.7cm", "mm", "5-7mm"),
])
def test_dimension_aware_numeric_conversion(raw, unit, expected):
    assert normalize_measure(raw, unit) == (expected, "ok")


def test_wrong_dimension_and_ambiguous_unit_do_not_become_numbers():
    assert normalize_measure("2inch", "g")[1] == "failed"
    assert normalize_measure("1oz", "unknown")[1] == "failed"
    with pytest.raises(ValueError, match="物理量"):
        rule_configs({"attributes": [{"name": "重量", "rule": "numeric",
            "params": {"unit": "g", "unit_factors": {"inch": 2.54, "g": 1}}}]})


def test_numeric_rule_preserves_fraction_and_uses_declared_subcolumns():
    cfg = {"name": "重量", "rule": "numeric", "params": {
        "unit": "g", "subcols": ["重量数值", "重量档位"], "unit_factors": {"kg": 1000, "g": 1},
        "bands": [[None, 10, "轻"], [10, None, "重"]]}}
    output, _, _, _ = apply_rule(cfg, "1.25g", {})
    assert output == {"重量数值": 1.25, "重量档位": "轻"}
    output, status, _, _ = apply_rule(cfg, "2inch", {})
    assert status == ST_REVIEW and output["重量数值"] == "未明确"


def _definitions():
    return {"attributes": [
        {"name": "容量", "rule": "numeric", "params": {"unit": "ml", "unit_factors": {"L": 1000, "ml": 1},
            "subcols": ["容量数值", "容量档位"], "bands": [[None, 1000, "小"], [1000, None, "大"]]}},
        {"name": "颜色", "rule": "color", "subcols": ["主色", "颜色明细"], "params": {"color_map": {"red": "红色", "blue": "蓝色"}}},
        {"name": "噪音", "rule": "noise", "subcols": ["分贝", "静音等级"], "params": {"unit": "dB", "level_threshold": 30, "min_plausible_db": 10}},
        {"name": "材质", "rule": "material", "subcols": ["主材质", "材质牌号", "材质明细"],
            "params": {"main_map": {"304钢": "不锈钢"}, "token_map": {"304钢": "不锈钢"}, "grade_hints": [["304", "304"]]}},
        {"name": "过滤", "rule": "layer_material", "subcols": ["层数", "滤材", "滤材种类数", "过滤类型"],
            "params": {"prefix_res": [r"\d+层过滤"], "strip_parens": True, "material_map": {"活性炭": "活性炭", "海绵": "海绵"}}},
        {"name": "模式", "rule": "count_modes", "subcols": ["模式数", "模式集"],
            "params": {"mode_map": {"喷泉": "喷泉", "水龙头": "水龙头"}, "count_from_modes": True}},
        {"name": "文字", "rule": "ocr", "subcols": ["OCR文字", "OCR类型"], "params": {"ocr_keys": [["品牌", "ACME"]]}},
        {"name": "功能", "rule": "enum", "subcols": ["功能标准"], "params": {"values": ["支持"], "aliases": {"supported": "支持"}}},
        {"name": "配件", "rule": "multi", "subcols": ["配件标准"], "params": {"values": ["盖", "刷"], "join_sep": "+"}},
        {"name": "说明", "rule": "open", "subcols": ["说明标准"], "params": {}},
    ], "vocab_status": "confirmed", "value_order": {"滤材": ["海绵", "活性炭"]}}


@pytest.mark.parametrize("bad_second_row", [False, True])
def test_actual_step17_step18_execute_all_rule_types_and_export_columns(tmp_path, monkeypatch, bad_second_row):
    cfg = Config({"CAT": "RuleWiring-US-202610", "DATA_ROOT": str(tmp_path), "DRY_RUN": False,
                  "ATTR_STAB_RUNS": 2, "ATTR_STAB_WORKERS": 1, "ATTR_STAB_MAX_RETRY": 0,
                  "ATTR_STAB_GEN_VOCAB": False, "ATTR_XC_AUTO": False, "LLM_USAGE_LOG": False})
    ctx = StepContext(cfg, Paths(cfg))
    root = Path(ctx.paths.cat_root)
    root.mkdir(parents=True)
    defs = _definitions()
    rules_path = root / "rule_wiring.rules.json"
    rules_path.write_text(json.dumps(defs, ensure_ascii=False), encoding="utf-8")
    spec = {"status": "confirmed", "cat": cfg.cat, "definition_revision": "wiring",
            "attributes": [{"name": a["name"], "hint": "按该产品原文判断"} for a in defs["attributes"]]}
    (root / f"{cfg.cat}-attribute-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    raw = {"容量": "1.2L", "颜色": "red+blue", "噪音": "25dB", "材质": "304钢",
           "过滤": "4层过滤（活性炭+海绵）", "模式": "喷泉+水龙头", "文字": "ACME; model X",
           "功能": "supported", "配件": "盖+刷", "说明": "hello   world"}
    input_file = Path(ctx.resolve_artifact(UPSTREAM_SPLIT))
    input_file.parent.mkdir(parents=True)
    wb = openpyxl.Workbook()
    wb.active.title = "AI已补充"
    wb.active.append(["标题", "五点描述", "详细参数", "图片链接", "SKU"])
    for index in range(2):
        wb.active.append(["ACME product 1.2L 25dB", "304钢 4层过滤（活性炭+海绵）", "喷泉+水龙头",
                          "https://example.com/a.jpg", f"sku-{index}"])
    wb.save(input_file)
    wb.close()
    def model(prompt, **kwargs):
        assert '"rows"' in prompt, "规则可确定的输入不应额外进行语义猜测"
        result = []
        for rid in (2, 3):
            attrs = {}
            for name, value in raw.items():
                if bad_second_row and rid == 3:
                    value = {"容量": "2inch", "噪音": "0dB", "颜色": "unknown-color",
                             "模式": "喷泉+unknown-mode"}.get(name, value)
                attrs.update({name: value, name + "_来源": "标题", name + "_依据": value})
            result.append({"xl_row": rid, "category_check": {"判定": "属于本类目"}, "attributes": attrs,
                           "尺寸_拆列": {"长": "", "宽": "", "高": ""}, "尺寸_依据": "无",
                           "suitable_scene_规范串": "", "词表外词": [], "pack_count_数值": None,
                           "跨列纠错": [], "冲突": []})
        return {"rows": result}
    ctx._llm = SimpleNamespace(chat_json=model)
    st17 = Step17AttrAnnotation()
    st17.validate_upstream(ctx)
    st17.run(ctx)
    from core.errors import WaitingForHuman
    with pytest.raises(WaitingForHuman):
        Step18Standardization().validate_upstream(ctx)
    defs = json.loads(rules_path.read_text(encoding="utf-8"))
    defs["vocab_status"] = "confirmed"
    rules_path.write_text(json.dumps(defs, ensure_ascii=False), encoding="utf-8")
    step = Step18Standardization()
    step.validate_upstream(ctx)
    step.run(ctx)
    output_file = root / "18_标准化" / f"{cfg.cat}-属性标注-标准化交付.xlsx"
    wb = openpyxl.load_workbook(output_file)
    try:
        headers = [c.value for c in wb["属性标注"][1]]
        assert len(headers) == len(set(headers))
        expected = {"容量数值": 1200, "容量档位": "大", "主色": "多色", "颜色明细": "红色+蓝色",
                    "分贝": 25, "静音等级": "静音", "主材质": "不锈钢", "材质牌号": "304",
                    "层数": 4, "滤材": "海绵+活性炭", "滤材种类数": 2, "模式数": 2,
                    "模式集": "喷泉+水龙头", "OCR文字": "ACME | model X", "OCR类型": "品牌",
                    "功能标准": "支持", "配件标准": "盖+刷", "说明标准": "hello world"}
        for name in raw:
            assert name in headers, "原属性列必须保留"
        for name, value in expected.items():
            column = headers.index(name) + 1
            for row in ((2,) if bad_second_row else (2, 3)):
                assert wb["属性标注"].cell(row, column).value == value, name
                assert wb["来源"].cell(row, column).value == "标题"
                assert wb["依据"].cell(row, column).value
        if bad_second_row:
            for name in ("容量数值", "容量档位", "主色", "分贝", "模式数"):
                assert wb["属性标注"].cell(3, headers.index(name) + 1).value == "未明确", name
    finally:
        wb.close()
    assert (root / "18_标准化" / "规则执行与输出校验.json").is_file()
    assert not list((root / "18_标准化").glob("tmp*.xlsx"))


def test_rule_column_collision_is_rejected_before_delivery():
    with pytest.raises(ValueError, match="多个属性"):
        rule_configs({"attributes": [{"name": "A", "rule": "open", "subcols": ["结果"]},
                                     {"name": "B", "rule": "open", "subcols": ["结果"]}]})


def test_legacy_numeric_without_unit_infers_only_unambiguous_quantity():
    definition = {"attributes": [{"name": "厚度", "type": "numeric", "values": ["未明确"]}]}
    cells = {"2|attr.厚度": {"values": [{"value": "2mm", "by": ["a1"]}]}}
    config = rule_configs(definition, cells=cells)[0]
    assert config["params"]["unit"] == "cm"
    assert config["subcols"] == ["厚度"]
    assert apply_rule(config, "2mm", {})[0]["厚度"] == 0.2
    cells["2|attr.厚度"]["values"].append({"value": "1kg", "by": ["a2"]})
    config = rule_configs(definition, cells=cells)[0]
    assert config["params"]["_unit_unresolved"]
    assert apply_rule(config, "2mm", {})[0]["厚度"] == "未明确"


def test_numeric_custom_units_and_band_dimensions():
    assert normalize_measure("50%", "%", unit_factors={"%": 1}) == ("50%", "ok")
    cfg = {"name": "压力", "rule": "numeric", "subcols": ["压力值", "压力档位"],
           "params": {"unit": "Pa", "bands": [[None, 100, "低速m/s"]]}}
    output, status, _, issues = apply_rule(cfg, "2kPa", {})
    assert output["压力值"] == 2000 and output["压力档位"] == "未明确"
    assert status == ST_REVIEW and issues


def test_numeric_explicit_derived_columns_are_still_respected():
    config = rule_configs({"attributes": [{"name": "容量", "rule": "numeric",
        "subcols": ["容量_数值", "容量_档位"],
        "params": {"unit": "mL", "bands": [[None, 500, "小"], [500, None, "大"]]}}]})[0]
    assert config["subcols"] == ["容量_数值", "容量_档位"]
    output, _, _, _ = apply_rule(config, "250mL", {})
    assert output["容量_数值"] == 250 and output["容量_档位"] == "小"


def test_delivery_hides_all_rule_columns_with_numeric_or_band_names():
    from tools.attr_stability.rule_pipeline import delivery_columns
    config = rule_configs({"attributes": [
        {"name": "重量", "rule": "numeric", "subcols": ["重量_数值", "重量_档位"], "params": {"unit": "g"}},
        {"name": "压力", "rule": "numeric", "subcols": ["测量_数值Pa", "压力等级"], "params": {"unit": "Pa"}},
        {"name": "模式", "rule": "count_modes", "subcols": ["模式数", "模式_档位清单"]},
    ]})
    assert delivery_columns(config[0]) == []
    assert delivery_columns(config[1]) == ["压力等级"]
    assert delivery_columns(config[2]) == ["模式数"]


def test_optional_rule_parameters_reach_convergence(tmp_path):
    from tools.attr_stability import RuleSource
    cfg = Config({"CAT": "Optional-US-202610", "DATA_ROOT": str(tmp_path), "DRY_RUN": True})
    ctx = StepContext(cfg, Paths(cfg))
    root = Path(ctx.paths.cat_root)
    root.mkdir(parents=True)
    definitions = {"attributes": [
        {"name": "耐热", "rule": "enum", "params": {"values": ["是"], "if_pattern": [{"pattern": r"\d+C", "then": "是"}]}},
        {"name": "尺寸", "rule": "open", "params": {"unit": "cm", "size_format": True}},
        {"name": "厚度", "rule": "numeric", "params": {"unit": "cm", "on_parse_fail": "未明确"}},
    ]}
    (root / "optional.rules.json").write_text(json.dumps(definitions, ensure_ascii=False), encoding="utf-8")
    ctx._llm = SimpleNamespace(chat_json=lambda *a, **kw: pytest.fail("声明规则可判断时不应调用模型"))
    res = SimpleNamespace(cells={f"2|attr.{name}": {"kind": "value", "values": [{"value": value, "by": ["a1"]}]}
                                for name, value in {"耐热": "耐热200C", "尺寸": "10x20x30 mm", "厚度": "加厚"}.items()})
    step = Step17AttrAnnotation()
    step._cat = cfg.cat
    values = step._converge_standardize(ctx, RuleSource.load(str(root), "optional"), res)
    assert values[(2, "耐热")] == "是"
    assert values[(2, "尺寸")] == "1x2x3 cm"
    assert values[(2, "厚度")] == "未明确"


def test_invalid_output_does_not_replace_previous_workbook(tmp_path, monkeypatch):
    cfg = Config({"CAT": "Atomic-US-202610", "DATA_ROOT": str(tmp_path), "DRY_RUN": True})
    ctx = StepContext(cfg, Paths(cfg))
    out = tmp_path / "delivery"
    out.mkdir()
    target = out / f"{cfg.cat}-属性标注-标准化交付.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["上一份有效交付"])
    wb.save(target)
    wb.close()
    previous = target.read_bytes()
    step = Step17AttrAnnotation()
    step._cat = cfg.cat
    def broken_save(*args, **kwargs):
        raise OSError("模拟写盘失败")
    monkeypatch.setattr(openpyxl.Workbook, "save", broken_save)
    result = SimpleNamespace(cells={}, deliverable=[{"xl_row": 2, "attributes": {"颜色": "红色"}}])
    with pytest.raises(OSError, match="模拟"):
        step._write_deliverables(ctx, str(out), [{"xl_row": 2, "标题": "商品"}], result, {(2, "颜色"): "红色"}, raw_too=False)
    assert target.read_bytes() == previous
    assert list(out.iterdir()) == [target]


def test_unrecognized_color_and_modes_are_reviewed_without_invented_counts():
    configs = rule_configs(_definitions())
    color = next(c for c in configs if c["rule"] == "color")
    output, status, _, issues = apply_rule(color, "red+unknown-color", {})
    assert output["主色"] == "未明确" and status == ST_REVIEW and issues
    modes = next(c for c in configs if c["rule"] == "count_modes")
    output, status, _, issues = apply_rule(modes, "喷泉+unknown-mode", {})
    assert output["模式数"] == "未明确" and status == ST_REVIEW and issues


def test_subcolumn_disagreement_does_not_invent_grade_or_remove_known_parent():
    from tools.attr_stability.rule_pipeline import project_columns
    config = rule_configs({"attributes": [{"name": "材质", "rule": "material",
        "subcols": ["主材质", "牌号", "明细"], "params": {
        "main_map": {"304钢": "不锈钢", "316钢": "不锈钢"},
        "token_map": {"304钢": "不锈钢", "316钢": "不锈钢"},
        "grade_hints": [["304", "304"], ["316", "316"]]}}]})
    result = SimpleNamespace(cells={"2|attr.材质": {"values": [
        {"value": "304钢", "by": ["a1"]}, {"value": "316钢", "by": ["a2"]}]}},
        deliverable=[{"xl_row": 2, "attributes": {"材质": "", "材质_来源": "标题", "材质_依据": "steel"}}])
    std = {(2, "材质"): "不锈钢"}
    report, _ = project_columns(config, result, [{"xl_row": 2}], std)
    assert std[(2, "材质")] == std[(2, "主材质")] == "不锈钢"
    assert std[(2, "牌号")] == "未明确"
    assert report[0]["输出候选分歧"]["牌号"] and report[0]["问题"]


def test_input_column_collision_preserves_values_and_ocr_is_literal_text(tmp_path):
    cfg = Config({"CAT": "Literal-US-202610", "DATA_ROOT": str(tmp_path), "DRY_RUN": True})
    ctx = StepContext(cfg, Paths(cfg))
    step = Step17AttrAnnotation()
    step._cat = cfg.cat
    step._delivery_rule_configs = [{"name": "文字", "subcols": ["文字标准"]}]
    result = SimpleNamespace(cells={}, deliverable=[{"xl_row": 2, "attributes": {
        "文字": "=ACME", "文字_来源": "图片", "文字_依据": "=ACME"}}])
    step._write_deliverables(ctx, str(tmp_path), [{"xl_row": 2, "文字标准": "原输入", "标题": "=PRODUCT"}], result,
                             {(2, "文字标准"): "=ACME"}, raw_too=False)
    wb = openpyxl.load_workbook(tmp_path / f"{cfg.cat}-属性标注-标准化交付.xlsx")
    try:
        ws = wb["属性标注"]
        headers = [c.value for c in ws[1]]
        assert ws.cell(2, headers.index("输入_文字标准") + 1).value == "原输入"
        cell = ws.cell(2, headers.index("文字标准") + 1)
        assert cell.value == "=ACME" and cell.data_type == "s"
        assert ws.cell(2, headers.index("标题") + 1).data_type == "s"
    finally:
        wb.close()


def test_interval_is_validated_as_interval_and_evidence_uses_converted_endpoints():
    from tools.attr_stability.num_check import check_cells
    spec = {"厚度": {"type": "numeric", "unit": "mm", "_物理量": "length"}}
    values = {(2, "厚度"): "5-7mm", (3, "厚度"): "0-7mm", (4, "厚度"): "8-9mm"}
    rows = {2: {"标题": "厚度0.5-0.7cm"}, 3: {"标题": "厚度0-0.7cm"},
            4: {"标题": "厚度0.5-0.7cm"}}
    report = check_cells(values, spec, rows)
    assert not [p for p in report["待重跑"] if p["行"] == 2]
    assert report["属性"]["厚度"]["统计"]["区间格数"] == 3
    assert any(p["行"] == 3 for p in report["可修"])
    assert any(p["行"] == 4 for p in report["待重跑"])


def test_interval_reaches_saved_delivery_without_false_numeric_retry(tmp_path):
    from tools.attr_stability import RuleSource
    from tools.attr_stability.rule_pipeline import project_columns
    cfg = Config({"CAT": "Interval-US-202610", "DATA_ROOT": str(tmp_path), "DRY_RUN": True})
    ctx = StepContext(cfg, Paths(cfg))
    root = Path(ctx.paths.cat_root)
    root.mkdir(parents=True)
    definition = {"attributes": [{"name": "厚度", "rule": "numeric", "params": {"unit": "mm"}}]}
    (root / "interval.rules.json").write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    result = SimpleNamespace(cells={"2|attr.厚度": {"kind": "value", "values": [
        {"value": "0.5-0.7cm", "by": ["a1"]}]}},
        deliverable=[{"xl_row": 2, "attributes": {"厚度": "0.5-0.7cm"}}])
    rows = [{"xl_row": 2, "标题": "厚度0.5-0.7cm"}]
    step = Step17AttrAnnotation()
    step._cat = cfg.cat
    std = step._converge_standardize(ctx, RuleSource.load(str(root), "interval"), result, rows)
    assert std[(2, "厚度")] == "5-7mm"
    configs = rule_configs(definition)
    _, step._delivery_provenance = project_columns(configs, result, rows, std)
    step._delivery_rule_configs = configs
    (root / "18_标准化").mkdir(exist_ok=True)
    step._write_deliverables(ctx, str(root / "18_标准化"), rows, result, std, raw_too=False)
    assert not list((root / "18_标准化").glob("*数值异常重跑指令.md"))
    wb = openpyxl.load_workbook(root / "18_标准化" / f"{cfg.cat}-属性标注-标准化交付.xlsx")
    try:
        headers = [c.value for c in wb["属性标注"][1]]
        assert "厚度_数值" not in headers and "厚度_档位" not in headers
        assert wb["属性标注"].cell(2, headers.index("厚度") + 1).value == "5-7mm"
    finally:
        wb.close()


def test_unknown_sentinels_and_negative_values_follow_shared_policy():
    configs = rule_configs({"unknown_values": ["无", "无法获取"], "attributes": [
        {"name": "无线", "rule": "enum", "values": ["有", "无"]},
        {"name": "备注", "rule": "open"}]})
    assert apply_rule(configs[0], "无", {})[0] == {"无线": "无"}
    for raw in ("未标注", "nan", "无法获取"):
        assert apply_rule(configs[1], raw, {})[0] == {"备注": "未明确"}


def test_declared_open_vocabulary_and_color_values_are_preserved():
    from tools.attr_stability.rule_pipeline import project_columns
    configs = rule_configs({"attributes": [
        {"name": "颜色", "rule": "color", "values": ["红色"]},
        {"name": "功能", "rule": "enum", "vocab_policy": "open"}]})
    result = SimpleNamespace(cells={}, deliverable=[{"xl_row": 2, "attributes": {
        "颜色": "红色", "功能": "开放功能"}}])
    std = {(2, "颜色"): "红色", (2, "功能"): "开放功能"}
    records, _ = project_columns(configs, result, [{"xl_row": 2}], std)
    assert std[(2, "颜色_标准")] == "红色"
    assert std[(2, "功能")] == "开放功能"
    assert all(not r["问题"] for r in records)


def test_invalid_column_arity_and_mode_count_mismatch_are_rejected():
    with pytest.raises(ValueError, match="输出列数"):
        rule_configs({"attributes": [{"name": "颜色", "rule": "color", "subcols": ["A", "B", "C"]}]})
    config = rule_configs({"attributes": [{"name": "模式", "rule": "count_modes", "params": {
        "strip_suffix": "模式$", "prefix_res": [r"\d+种"], "strip_parens": True,
        "mode_map": {"喷泉": "喷泉", "水龙头": "水龙头"}, "count_from_modes": True}}]})[0]
    output, status, _, _ = apply_rule(config, "1种（喷泉+水龙头）", {})
    assert output["模式_模式数"] == "未明确" and status == ST_REVIEW
    output, status, _, _ = apply_rule(config, "未知模式", {})
    assert output["模式_模式数"] == "未明确" and status == ST_REVIEW
