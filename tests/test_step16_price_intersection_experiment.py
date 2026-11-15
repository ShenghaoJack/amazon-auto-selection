"""Offline acceptance for the isolated price-band/three-round experiment."""
import json

import pandas as pd
import pytest

from core.config import Config
from core.errors import LLMError
from core.llm import LLMClient
from scripts._plan_pilot.step16_test import step16_price_intersection_test as experiment


class FakeModel:
    def __init__(self, rounds, groups):
        self.responses = [*rounds, groups]
        self.calls = []

    def chat_json(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return {"attributes": self.responses.pop(0)}


def _items(*names):
    return [{"name": name, "hint": f"{name}的判断依据和典型取值"} for name in names]


def _frame(n=60):
    return pd.DataFrame({"标题": [f"product-{i}" for i in range(n)],
                         "详细参数": [f"parameter-{i}" for i in range(n)],
                         "__source_row": list(range(2, n + 2))})


def _read(out):
    return json.loads((out / "审计记录.json").read_text(encoding="utf-8"))


def test_each_round_resamples_twenty_and_intersects_after_synonym_alignment(tmp_path):
    fake = FakeModel([_items("容量", "容积", "材质"), _items("容积", "颜色"), _items("容量")], [
        {"name": "容量", "aliases": ["容积"], "hint": "统一的容量说明"},
        {"name": "材质", "aliases": [], "hint": "材质说明"},
        {"name": "颜色", "aliases": [], "hint": "颜色说明"}])
    result = experiment.run_band(fake, "Test-US-202610", _frame(), tmp_path / "L01")
    assert result["status"] == "complete"
    assert result["属性集名单"] == ["容量"]
    assert result["候选名单"] == ["材质", "颜色"]
    assert result["runs明细"]["容量"] == 3, "同一轮的同义名只算一票"
    draws = [row["源Excel行号"] for row in result["逐轮"]]
    assert all(len(draw) == len(set(draw)) == 20 for draw in draws)
    assert len({frozenset(draw) for draw in draws}) == 3
    assert all(row["完整进入样本数"] == 20 for row in result["逐轮"])
    assert all("标题:" in row["样本文本"] and "详细参数:" in row["样本文本"] for row in result["逐轮"])
    assert [row["seed"] for row in result["逐轮"]] == [1000, 1001, 1002]
    assert "统一的容量说明" in (tmp_path / "L01/属性集表.csv").read_text(encoding="utf-8-sig")
    assert len(fake.calls) == 4


def test_true_empty_intersection_is_not_replaced_by_union(tmp_path):
    fake = FakeModel([_items("容量"), _items("材质"), _items("颜色")], [
        {"name": name, "aliases": [], "hint": name} for name in ("容量", "材质", "颜色")])
    out = tmp_path / "L01"
    result = experiment.run_band(fake, "Test", _frame(), out)
    assert result["status"] == "complete" and result["交集为空"]
    assert result["属性集名单"] == [] and result["gate_bypassed"] is False
    assert len((out / "属性集表.csv").read_text(encoding="utf-8-sig").splitlines()) == 1
    assert len(result["候选名单"]) == 3


def test_failed_round_is_not_a_valid_empty_intersection(tmp_path):
    fake = FakeModel([_items("容量"), [], _items("容量")], [])
    out = tmp_path / "L01"
    with pytest.raises(ValueError, match="抽取失败/响应为空"):
        experiment.run_band(fake, "Test", _frame(), out)
    assert _read(out)["status"] == "failed"
    assert len(fake.calls) == 2
    assert not (out / "属性集表.csv").exists()


@pytest.mark.parametrize("groups", [
    [], [{"name": "容量", "aliases": ["不存在"]}],
    [{"name": "容量", "aliases": []}],
    [{"name": "容量", "aliases": ["材质"]}, {"name": "材质", "aliases": []}],
])
def test_invalid_or_incomplete_synonym_groups_fail_instead_of_raw_name_fallback(tmp_path, groups):
    fake = FakeModel([_items("容量", "材质")] * 3, groups)
    out = tmp_path / "L01"
    with pytest.raises(ValueError, match="同义|分组|原名"):
        experiment.run_band(fake, "Test", _frame(), out)
    assert _read(out)["status"] == "failed"
    assert not (out / "属性集表.csv").exists()


def test_global_name_alignment_keeps_source_hints_without_requesting_rewritten_hints():
    fake = FakeModel([], [{"name": "容量", "aliases": ["容量", "容积"]}])
    mapping, hints, _ = experiment.synonym_groups(fake, [_items("容量", "容积")], include_hints=False)
    assert mapping == {"容量": "容量", "容积": "容量"}
    assert hints == {"容量": "容量的判断依据和典型取值"}


def test_global_assignment_dictionary_aligns_each_name_once():
    class MappingModel:
        def chat_json(self, *args, **kwargs):
            return {"mapping": {"容量": "容量", "容积": "容量", "材质": "材质"}}

    mapping, hints, _ = experiment.synonym_groups(
        MappingModel(), [_items("容量", "容积", "材质")], include_hints=False)
    assert mapping == {"容量": "容量", "容积": "容量", "材质": "材质"}
    assert set(hints) == {"容量", "材质"}


def test_alignment_reports_unknown_aliases_and_missing_names_in_one_feedback():
    fake = FakeModel([], [{"name": "容量", "aliases": ["不存在"]}])
    with pytest.raises(ValueError) as error:
        experiment.synonym_groups(fake, [_items("容量", "材质")])
    assert "不存在" in str(error.value) and "遗漏输入原名" in str(error.value)
    assert "材质" in str(error.value)


def test_small_band_uses_all_available_rows_and_records_shortage(tmp_path):
    fake = FakeModel([_items("容量")] * 3, [{"name": "容量", "aliases": [], "hint": "容量"}])
    result = experiment.run_band(fake, "Test", _frame(7), tmp_path / "L01")
    assert all(row["抽中数"] == 7 and row["不足20"] for row in result["逐轮"])
    assert result["属性集名单"] == ["容量"]


def test_twenty_long_products_are_not_silently_cut_by_global_character_cap(tmp_path):
    frame = _frame(20)
    for column in ("标题", "五点描述", "详细参数", "SKU"):
        frame[column] = [f"产品{i}-" + "长" * 500 for i in range(20)]
    fake = FakeModel([_items("容量")] * 3, [{"name": "容量", "aliases": [], "hint": "容量"}])
    result = experiment.run_band(fake, "Test", frame, tmp_path / "L01")
    assert all(row["完整进入样本数"] == 20 and len(row["样本文本"]) > 20000
               for row in result["逐轮"])


def test_actual_low_temperature_is_fixed_during_client_retries(monkeypatch):
    temperatures = []
    def request(self, base_url, api_key, model, messages, temperature, max_tokens, json_mode, **kwargs):
        temperatures.append(temperature)
        if len(temperatures) < 5:
            raise LLMError("fixture invalid response")
        return '{"attributes": []}', "stop"
    monkeypatch.setattr(LLMClient, "_request", request)
    monkeypatch.setattr(LLMClient, "_cool_down", lambda *args: None)
    llm = experiment.FixedTemperatureLLM(Config({"DRY_RUN": False, "LLM_MODEL": "test"}))
    llm.chat_json("test", temperature=0.4)
    assert len(temperatures) == 5 and set(temperatures) == {0.0}


def test_price_bands_cover_valid_positive_prices_once_and_keep_source_rows():
    frame = pd.DataFrame({"价格($)": list(range(1, 121)) + [0, -1, None, float("inf")],
                          "标题": ["商品"] * 124})
    info, groups = experiment.band_populations(frame)
    rows = [rid for _, _, group in groups for rid in group["__source_row"]]
    assert info["有效行数"] == 120 and info["无效价格行数"] == 4
    assert len(rows) == len(set(rows)) == 120
    assert set(rows) == set(range(2, 122))


def test_existing_output_is_never_silently_reused(tmp_path):
    with pytest.raises(FileExistsError):
        experiment.run_band(None, "Test", _frame(), tmp_path)


def test_insufficient_price_population_fails_clearly():
    with pytest.raises(ValueError, match="少于5"):
        experiment.band_populations(pd.DataFrame({"价格": [1, 2, 3]}))


def test_configuration_failure_is_recorded_as_failed_plan(tmp_path, monkeypatch):
    source = tmp_path / "source.xlsx"
    pd.DataFrame({"价格": list(range(1, 31)), "标题": ["商品"] * 30}).to_excel(source, index=False)
    out = tmp_path / "result"
    monkeypatch.setattr("sys.argv", ["experiment", "--input", str(source),
                                    "--output", str(out), "--config", str(tmp_path / "missing.yaml")])
    with pytest.raises(FileNotFoundError):
        experiment.main()
    assert json.loads((out / "试验计划.json").read_text(encoding="utf-8"))["status"] == "failed"


@pytest.mark.parametrize("sheet,expected", [(None, "原始"), ("AI已补充", "AI已补充")])
def test_plan_only_records_explicit_input_sheet_without_model_calls(tmp_path, monkeypatch, sheet, expected):
    source = tmp_path / "source.xlsx"
    frame = pd.DataFrame({"价格": list(range(1, 31)), "标题": ["商品"] * 30})
    with pd.ExcelWriter(source) as writer:
        frame.to_excel(writer, sheet_name="原始", index=False)
        frame.iloc[:15].to_excel(writer, sheet_name="AI已补充", index=False)
    out = tmp_path / "result"
    argv = ["experiment", "--plan-only", "--input", str(source), "--output", str(out)]
    if sheet:
        argv += ["--sheet", sheet]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(experiment, "FixedTemperatureLLM", lambda *a: pytest.fail("规划不能调用模型"))
    experiment.main()
    plan = json.loads((out / "试验计划.json").read_text(encoding="utf-8"))
    assert plan["status"] == "plan_only" and plan["工作表"] == expected
    assert plan["轮数"] == 3 and plan["每轮每段目标样本数"] == 20 and plan["温度"] == 0
