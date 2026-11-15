"""Verify sampling and set logic before interpreting real model experiments."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts._plan_pilot.step16_test import run_real_comparison as comparison


def frame(n=120):
    return pd.DataFrame({"xl_row": range(2, n + 2), "标题": [f"产品{i}" for i in range(n)],
                         "五点描述": ["真实描述"] * n, "价格($)": range(1, n + 1)})


def test_abcd_fixed_samples_and_e_resampled_rounds_share_same_price_population():
    source = frame()
    _, groups = comparison.E.band_populations(source)
    tasks = comparison.tasks_for(source, groups)
    assert len(tasks) == 15 + len(groups) * 6
    for label in ["A", "B1", "B2", "B3", "C", *["D-" + x[0] for x in groups]]:
        rows = [t["selected_source_rows"] for t in tasks if t["label"] == label]
        assert len(rows) == 3 and rows[0] == rows[1] == rows[2]
    for name, _, group in groups:
        rows = [t for t in tasks if t["label"] == "E-" + name]
        assert len(rows) == 3
        for task in rows:
            assert task["temperature"] == 0 and len(task["complete_source_rows"]) == min(20, len(group))
            assert set(task["selected_source_rows"]) <= set(group["xl_row"])


def test_legacy_union_fallback_and_strict_intersection_are_distinguished():
    sets = [{"容量"}, {"材质"}, {"颜色"}]
    legacy = comparison.select_names(sets, 2, legacy_fallback=True)
    strict = comparison.select_names(sets, 3)
    assert legacy["gate_bypassed"] and len(legacy["stable"]) == 3
    assert not strict["gate_bypassed"] and strict["stable"] == []
    assert strict["candidates"] == sorted(["容量", "材质", "颜色"])


def test_aliases_vote_once_per_round_before_threshold():
    batches = [[{"name": "容量"}, {"name": "容积"}], [{"name": "材质"}], [{"name": "容积"}]]
    aligned = comparison.aligned_sets(batches, {"容量": "容量", "容积": "容量", "材质": "材质"})
    selected = comparison.select_names(aligned, 2)
    assert selected["counts"]["容量"] == 2 and selected["stable"] == ["容量"]


def test_truncated_partial_product_is_not_reported_as_complete():
    source = frame(3)
    result = comparison.build_sample(source, char_cap=10)
    assert len(result["sample"]) == 10
    assert result["partial_or_dropped_rows"] > 0


def test_resume_reuses_only_matching_successful_task_without_model_call(tmp_path):
    task = {"id": "A-r1", "label": "A", "sample": "sample", "temperature": 0.2}
    fingerprint = comparison.hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    comparison.save(tmp_path / "tasks/A-r1.json", {"status": "complete", "fingerprint": fingerprint,
                                                    "attributes": [{"name": "容量"}]})
    assert comparison.run_task(task, tmp_path, None)["attributes"] == [{"name": "容量"}]


def test_semantic_review_rejects_identity_mapping_for_verified_app_synonyms(tmp_path, monkeypatch):
    batches = [[{"name": "是否支持APP", "hint": "手机APP远程控制"}],
               [{"name": "是否支持APP控制", "hint": "手机APP远程控制"}]]
    original = tmp_path / "alignments/E-L01.json"
    comparison.save(original, {"status": "complete", "initial_review": True})
    client = SimpleNamespace(validation_feedback="")
    monkeypatch.setattr(comparison, "AuditedLLM", lambda *args, **kwargs: client)
    feedback = []

    def model_groups(*args):
        feedback.append(client.validation_feedback)
        if len(feedback) == 1:
            return ({"是否支持APP": "是否支持APP", "是否支持APP控制": "是否支持APP控制"},
                    {"是否支持APP": "手机APP远程控制", "是否支持APP控制": "手机APP远程控制"}, {})
        return ({"是否支持APP": "是否支持APP控制", "是否支持APP控制": "是否支持APP控制"},
                {"是否支持APP控制": "手机APP远程控制"}, {})

    monkeypatch.setattr(comparison.E, "synonym_groups", model_groups)
    mapping, _ = comparison.dedupe(batches, "E-L01", tmp_path, None)
    assert len(feedback) == 2 and "同义但仍被拆组" in feedback[1]
    assert len(set(mapping.values())) == 1
    assert comparison.read(original) == {"status": "complete", "initial_review": True}


def test_alignment_resume_retains_all_validation_feedback(tmp_path, monkeypatch):
    batches = [[{"name": "容量", "hint": "储粮体积"}]]
    client = SimpleNamespace(validation_feedback="")
    monkeypatch.setattr(comparison, "AuditedLLM", lambda *args, **kwargs: client)
    failures = iter(["遗漏容量", "材质重复归组", "遗漏容量"])

    def invalid_groups(*args):
        raise ValueError(next(failures))

    monkeypatch.setattr(comparison.E, "synonym_groups", invalid_groups)
    with pytest.raises(ValueError, match="同义归类失败"):
        comparison.dedupe(batches, "E-L01", tmp_path, None)

    def corrected_groups(*args):
        assert "遗漏容量" in client.validation_feedback
        assert "材质重复归组" in client.validation_feedback
        return {"容量": "容量"}, {"容量": "储粮体积"}, {}

    monkeypatch.setattr(comparison.E, "synonym_groups", corrected_groups)
    assert comparison.dedupe(batches, "E-L01", tmp_path, None)[0] == {"容量": "容量"}
