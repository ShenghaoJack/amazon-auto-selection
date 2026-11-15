import pytest
import json

from tools.kwgen_hybrid.weekly_fallback import complete_sif_rows, complete_sellersprite_rows, week_window


def test_october_full_weeks_match_across_providers():
    starts = [d.isoformat() for d in week_window("202610", 6)]
    assert starts == ["2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25"]
    sif, sm = complete_sif_rows({"keywords": [{"keyword": "dog bowls", "dates": starts,
                                               "volumes": [10, 20, 30, 40]}]}, ["dog bowls"], "202610")
    seller, mm = complete_sellersprite_rows([
        {"label": day, "searches": num} for day, num in zip(
            ["20261010", "20261017", "20261024", "20261031"], [10, 20, 30, 40])], "dog bowls", "202610")
    assert sif == seller == {"dog bowls": 100}
    assert sm["period_start"] == mm["period_start"] == "2026-10-04"
    assert sm["period_end"] == mm["period_end"] == "2026-10-31"


def test_month_with_three_complete_weeks_uses_previous_complete_week():
    starts = [d.isoformat() for d in week_window("202510", 6)]
    assert starts == ["2025-09-28", "2025-10-05", "2025-10-12", "2025-10-19"]
    _, meta = complete_sif_rows({"keywords": []}, [], "202510")
    assert meta["kind"] == "recent_four_weeks"


@pytest.mark.parametrize("vals", [[1, 2, None, 4], [1, 2, float('nan'), 4], [1, 2, -1, 4]])
def test_missing_or_invalid_week_is_not_treated_as_zero(vals):
    starts = [d.isoformat() for d in week_window("202610", 6)]
    rows, _ = complete_sif_rows({"keywords": [{"keyword": "dog bowls", "dates": starts,
                                                 "volumes": vals}]}, ["dog bowls"], "202610")
    assert rows == {}


def test_pipeline_uses_prior_recall_and_weekly_evidence_only_for_empty_month():
    from tools.kwgen_hybrid.engine import HybridPipeline
    class Model:
        def ask(self, stage, prompt):
            if stage == "discovery":
                return {"product_scope": "dog bowls", "languages": ["en"],
                        "roots": [{"text": "dog bowls", "language": "en"}], "candidates": [], "entities": []}
            if stage == "judge":
                rows = json.loads(prompt.split("DATA=", 1)[1])["keywords"]
                return {"judgments": [{"id": r["id"], "relation": "same_product", "brand": False,
                    "language": "en", "dimension": "属性", "is_longtail": False, "reason": "same", "entities": []} for r in rows]}
            raise AssertionError(stage)
    class Miner:
        def query(self, request):
            if request["historyDate"] == "202609":
                return {"items": [{"keyword": "dog bowls", "searches": 200}], "hasNextPage": False}
            return {"items": []}
    class Weekly:
        def query(self, words, site, month):
            assert (site, month) == ("US", "202610")
            return {"dog bowls": 100}, {"source": "SIF", "period_start": "2026-10-04",
                    "period_end": "2026-10-31", "weeks": 4, "kind": "complete_weeks"}
    result = HybridPipeline(Model(), Miner(), hint_limit=0, weekly=Weekly()).run(
        "Dog Bowls", "US", "202610", [{"title": "dog bowls"}])
    assert result["core"][0]["searches"] == 100
    assert result["core"][0]["kind"] == "complete_weeks"
    assert result["summary"]["weekly_fallback"]["recall_month"] == "202609"
