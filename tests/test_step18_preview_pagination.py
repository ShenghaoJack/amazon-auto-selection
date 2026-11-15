"""Paged preview and selection semantics, using isolated workspace fixtures."""
import copy
import json

import pytest

from tests.test_step18_workspace import request, workspace, write
from tools.attr_stability.step18_workspace import preview_selection
from webui.step18_api import preview_page


def test_preview_pages_return_bounded_rows_and_cross_page_filter_selection(workspace):
    state = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    patch = next(p for p in proposal["patches"] if p["type"] == "cell")
    cells = [{**copy.deepcopy(patch), "row_id": str(i),
              "after": {**patch["after"], "basis": "test-%03d" % i}} for i in range(123)]
    job_id = "J" + "a" * 32
    write(workspace._path("jobs", job_id, "J"), {"id": job_id, "status": "done",
          "base_version": state["version"], "result": {**proposal, "patches": cells}})
    first = preview_page(workspace, job_id)
    assert first["total"] == 123 and len(first["items"]) == 50
    assert "patches" not in first["result"]
    assert first["selected_count"] == 123
    rules = [{"kind": "filter", "term": "test-1", "selected": False},
             {"kind": "cell", "index": 105, "selected": True}]
    second = preview_page(workspace, job_id, page=2, selection_rules=rules)
    assert second["page"] == 2 and len(second["items"]) == 23
    assert second["selected_count"] == 101  # 100..122 except 105 deselected
    assert next(x for x in second["items"] if x["index"] == 105)["selected"]
    searched = preview_page(workspace, job_id, term="test-10", selection_rules=rules)
    assert searched["matched"] == 10
    assert not next(x for x in searched["items"] if x["index"] == 104)["selected"]


def test_paged_selection_applies_only_selected_cells_and_can_undo(workspace):
    initial = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "塑料", "金属": "塑料"}}))
    rules = [{"kind": "filter", "term": "", "selected": False},
             {"kind": "filter", "term": "金属", "selected": True}]
    applied = workspace.apply(proposal["id"], initial["version"], selection_rules=rules)
    assert [r["attributes"]["材质"]["value"] for r in applied["rows"]] == ["塑料", "ABS"]
    assert applied["operation"]["omitted_cells"] == 1
    undo = workspace.restore_proposal(applied["version"], selective=True, expected=applied["version"])
    restored = workspace.apply(undo["id"], applied["version"])
    assert [r["attributes"]["材质"]["value"] for r in restored["rows"]] == ["金属", "ABS"]


def test_paged_selection_excludes_deleted_rows(workspace):
    initial = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "塑料", "金属": "塑料"}}))
    job_id = "J" + "b" * 32
    write(workspace._path("jobs", job_id, "J"), {"id": job_id, "status": "done",
          "base_version": initial["version"], "result": proposal})
    after_delete = workspace.delete_row("3", initial["version"])
    page = preview_page(workspace, job_id)
    assert page["selected_count"] == 1
    assert not next(x for x in page["items"] if x["patch"]["row_id"] == "3")["available"]
    applied = workspace.apply(proposal["id"], after_delete["version"], selection_rules=[])
    assert len(applied["rows"]) == 1 and applied["rows"][0]["attributes"]["材质"]["value"] == "塑料"


def test_selection_rules_reject_invalid_or_ambiguous_input():
    cells = [{"row_id": "2", "attribute": "材质", "before": {"value": "旧"}, "after": {"value": "新"}}]
    for rule in ({"kind": "cell", "index": True, "selected": True},
                 {"kind": "cell", "index": 2, "selected": True},
                 {"kind": "filter", "term": "x", "selected": "yes"}):
        with pytest.raises(ValueError):
            preview_selection(cells, [rule])


def test_cached_preview_not_returned_after_job_status_changes(workspace):
    state = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    job_id = "J" + "c" * 32
    path = workspace._path("jobs", job_id, "J")
    write(path, {"id": job_id, "status": "done", "base_version": state["version"], "result": proposal})
    assert preview_page(workspace, job_id)["total"] == 1
    write(path, {"id": job_id, "status": "cancelled", "base_version": state["version"], "result": proposal})
    with pytest.raises(ValueError, match="状态已变化"):
        preview_page(workspace, job_id)


def test_terminal_job_summary_survives_in_memory_cache_clear(workspace, monkeypatch):
    from webui import step18_api
    state = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    job_id = "J" + "d" * 32
    path = workspace._path("jobs", job_id, "J")
    write(path, {"id": job_id, "status": "done", "base_version": state["version"], "result": proposal})
    assert step18_api.list_jobs(workspace)[0]["proposal_id"] == proposal["id"]
    assert workspace._path("job-summaries", job_id, "J").exists()
    step18_api._JOB_LIST_CACHE.clear()
    def fail_read(*args, **kwargs):
        raise AssertionError("terminal job should be read from the compact sidecar")
    monkeypatch.setattr(step18_api, "job_status", fail_read)
    assert step18_api.list_jobs(workspace)[0]["changed_cells"] == 1


def test_large_preview_sends_only_one_page(workspace):
    state = workspace.open()
    proposal = workspace.propose(request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}}))
    patch = next(p for p in proposal["patches"] if p["type"] == "cell")
    cells = [{**copy.deepcopy(patch), "row_id": str(i)} for i in range(5000)]
    job_id = "J" + "e" * 32
    write(workspace._path("jobs", job_id, "J"), {"id": job_id, "status": "done",
          "base_version": state["version"], "result": {**proposal, "patches": cells}})
    page = preview_page(workspace, job_id)
    assert len(page["items"]) == 50 and page["selected_count"] == 5000
    assert len(json.dumps(page, ensure_ascii=False)) < len(json.dumps(cells, ensure_ascii=False)) / 20
