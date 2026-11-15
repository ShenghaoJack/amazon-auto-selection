"""Regression tests for occurrence identity, semantic review, and E recount."""
import pytest

from scripts._plan_pilot.step16_test import run_semantic_review as experiment
from scripts._plan_pilot.step16_test import render_semantic_review as report


def definition(name="食盆材质", obj="食盆"):
    return {"name": name, "object": obj, "dimension": "材质", "value_kind": "category",
            "definition": f"{obj}的构成材料", "boundary": "不包含其他部件的材料"}


def records():
    return [{"id": f"r{number}", "band": "L01", "round": number,
             "name": name, "hint": "食盆接触食物部分的材料"}
            for number, name in enumerate(["碗材质", "食盆材质", "碗体材质"], 1)]


def test_synonyms_vote_by_round_not_raw_name_and_duplicates_count_once():
    rows = records() + [{**records()[0], "id": "duplicate"}]
    result = experiment.recount(rows, {r["id"]: "bowl" for r in rows}, {"bowl": definition()})
    assert result["stable"] == ["bowl"]
    assert result["by_band"]["L01"]["votes"] == {"bowl": 3}


def test_same_name_different_objects_do_not_automatically_share_votes():
    rows = [{**r, "name": "材质"} for r in records()]
    result = experiment.recount(rows, {"r1": "body", "r2": "bowl", "r3": "bowl"},
                                {"body": definition("机身材质", "机身"), "bowl": definition()})
    assert result["stable"] == []
    assert result["by_band"]["L01"]["votes"] == {"body": 1, "bowl": 2}


def test_global_alignment_recovers_intersection_from_all_local_candidates():
    rows = records()
    local = {"L01": {"proposal": {
        "definitions": {"g1": definition(), "g2": definition("碗体材质")},
        "assignments": {"r1": "g1", "r2": "g2", "r3": "g2"}, "uncertain": {}}}}
    inputs, members = experiment.global_items(rows, local)
    assert {x["id"] for x in inputs} == {"L01.g1", "L01.g2"}
    flattened = {rid: "bowl" for group in members.values() for rid in group}
    assert experiment.recount(rows, flattened, {"bowl": definition()})["stable"] == ["bowl"]


def test_uncertain_is_preserved_but_cannot_be_counted_as_a_third_vote():
    result = experiment.recount(records(), {"r1": "bowl", "r2": "bowl", "r3": None}, {"bowl": definition()})
    assert result["stable"] == [] and result["uncertain_occurrences"] == ["r3"]


@pytest.mark.parametrize("assignments,uncertain", [
    ({"r1": "g1", "r2": "g1"}, {}),
    ({"r1": "g1", "r2": "g1", "r3": "missing"}, {}),
    ({"r1": "g1", "r2": "g1", "r3": None}, {}),
])
def test_invalid_or_unexplained_partial_groupings_are_rejected(assignments, uncertain):
    with pytest.raises(ValueError):
        experiment.validate_grouping({"definitions": {"g1": definition()},
                                     "assignments": assignments, "uncertain": uncertain}, records())


def test_review_must_cover_every_input_and_cannot_pass_with_issues():
    with pytest.raises(ValueError, match="覆盖"):
        experiment.validate_review({"verdict": "pass", "checked_ids": ["r1"], "issues": []}, records())
    with pytest.raises(ValueError, match="结论"):
        experiment.validate_review({"verdict": "pass", "checked_ids": ["r1", "r2", "r3"], "issues": [{
            "kind": "missed_merge", "action": "merge", "ids": ["r1", "r2"], "reason": "同为食盆材料", "suggestion": "合并"}]}, records())


def test_unresolved_review_withholds_whole_disputed_groups(tmp_path, monkeypatch):
    proposal = {"definitions": {"g1": definition()},
                "assignments": {"r1": "g1", "r2": "g1", "r3": "g1"}, "uncertain": {}}
    monkeypatch.setattr(experiment, "propose", lambda *args: proposal)
    monkeypatch.setattr(experiment, "review", lambda *args: {
        "verdict": "revise", "checked_ids": ["r1", "r2", "r3"], "issues": [{
            "kind": "overmerge", "action": "split", "ids": ["r1"], "reason": "机身混进食盆", "suggestion": "拆分"}]})
    result = experiment.reviewed_grouping(None, tmp_path, "L01", records())
    assert result["status"] == "partial_withheld"
    assert set(result["proposal"]["uncertain"]) == {"r1", "r2", "r3"}
    assert result["proposal"]["definitions"] == {}


def test_independent_reviewer_cannot_claim_already_merged_ids_are_missed():
    proposal = {"assignments": {"r1": "bowl", "r2": "bowl", "r3": "bowl"}}
    with pytest.raises(ValueError, match="误报漏合并"):
        experiment.validate_review({"verdict": "revise", "checked_ids": ["r1", "r2", "r3"], "issues": [{
            "kind": "missed_merge", "action": "merge", "ids": ["r1", "r2"], "reason": "名称不同", "suggestion": "合并"}]},
            records(), proposal)


def test_independent_reviewer_cannot_claim_separate_groups_are_overmerged():
    proposal = {"assignments": {"r1": "body", "r2": "bowl", "r3": "other"}}
    with pytest.raises(ValueError, match="误合并"):
        experiment.validate_review({"verdict": "revise", "checked_ids": ["r1", "r2", "r3"], "issues": [{
            "kind": "overmerge", "action": "split", "ids": ["r1", "r2"],
            "reason": "不同部件", "suggestion": "拆分"}]}, records(), proposal)


@pytest.mark.parametrize("tamper", [None, "votes", "binding"])
def test_report_recomputes_votes_and_follows_the_actual_mapping_chain(tmp_path, tamper):
    spec = definition()
    gid = "attr_" + experiment.fingerprint(spec)[:12]
    mapped = {r["id"]: gid for r in records()}
    if tamper == "binding":
        mapped["r3"] = None
    result = experiment.recount(records(), mapped, {gid: spec})
    if tamper == "votes":
        result["by_band"]["L01"]["votes"][gid] = 2
    experiment.save(tmp_path / "result.json", result)
    experiment.save(tmp_path / "occurrences.json", records())
    experiment.save(tmp_path / "groupings/global.json", {"proposal": {
        "definitions": {"g1": spec}, "assignments": {"local": "g1"}}})
    experiment.save(tmp_path / "global-input-members.json", {"local": ["r1", "r2", "r3"]})
    experiment.save(tmp_path / "isolation.json", {
        "changed_business_files": [], "changed_previous_experiment_files": []})
    if tamper:
        with pytest.raises(AssertionError):
            report.check(tmp_path)
    else:
        assert report.check(tmp_path)[0]["stable"] == [gid]


def test_failed_review_is_unknown_coverage_not_a_successful_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "propose", lambda *args: {})
    def failed_review(*args):
        raise ValueError("复核三次均未通过")
    monkeypatch.setattr(experiment, "review", failed_review)
    result = experiment.reviewed_grouping(None, tmp_path, "L01", records())
    assert result["status"] == "review_failed" and result["error"]
    assert set(result["proposal"]["uncertain"]) == {"r1", "r2", "r3"}
    assert set(result["proposal"]["assignments"].values()) == {None}


def test_unreferenced_model_definition_cannot_enter_the_attribute_pool():
    raw = {"definitions": {"bowl": definition(), "unsupported": definition("无来源属性")},
           "assignments": {r["id"]: "bowl" for r in records()}, "uncertain": {}}
    normalized = experiment.validate_grouping(raw, records())
    assert set(normalized["definitions"]) == {"bowl"}
    assert normalized["assignments"] == raw["assignments"]
    assert "unsupported" in raw["definitions"], "原始响应不能被原地改写"
    assert normalized["format_repairs"][0]["ids"] == ["unsupported"]


def test_global_duplicate_labels_reach_semantic_review_but_identical_groups_cannot_pass():
    proposal = {"definitions": {"g1": definition(), "g2": definition()},
                "assignments": {"r1": "g1", "r2": "g2", "r3": "g2"}, "uncertain": {}}
    valid_structure = experiment.validate_grouping(proposal, records(), allow_duplicate_names=True)
    with pytest.raises(ValueError, match="重复组"):
        experiment.validate_review({"verdict": "pass", "checked_ids": ["r1", "r2", "r3"], "issues": []},
                                   records(), valid_structure)
