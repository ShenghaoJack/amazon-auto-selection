"""Coverage for the scoped Step 18 preview and indexed job paths."""
import copy
import time

from tests.test_step18_workspace import request, workspace
from tools.attr_stability.step18_workspace import read_json
from webui.step18_api import job_status, mark_applied, start_preview


def test_scoped_copy_and_diff_preserve_other_rows_and_match_full_diff(workspace):
    base = workspace.open()
    original = copy.deepcopy(base)
    draft = workspace._scoped_copy(base, {"2"}, {"材质"})
    assert draft["rows"][1] is base["rows"][1]
    draft["rows"][0]["attributes"]["材质"]["value"] = "钢"
    definition = next(d for d in draft["definitions"]["attributes"] if d["name"] == "材质")
    definition["params"]["values"].append("钢")
    assert base == original
    assert workspace._scoped_diff(base, draft, {"2"}, {"材质"}) == workspace.diff(base, draft)


def test_background_preview_reuses_prepared_version_and_indexes_job(workspace, monkeypatch):
    req = request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}})
    calls = []
    original_open = workspace.open
    def counted():
        calls.append(1)
        return original_open()
    monkeypatch.setattr(workspace, "open", counted)
    job = start_preview(workspace, req, {})
    for _ in range(500):
        result = job_status(workspace, job["job_id"])
        if result["status"] not in ("pending", "running"):
            break
        time.sleep(.01)
    assert result["status"] == "done", result
    assert len(calls) == 1
    assert result["timings_ms"]["prepare"] >= 0
    assert result["timings_ms"]["processing"] >= 0
    assert result["result"]["job_id"] == job["job_id"]
    # An unrelated broken old job must not be read while applying an indexed job.
    broken = workspace._path("jobs", "J" + "f" * 32, "J")
    broken.write_text("{broken", encoding="utf-8")
    applied = workspace.apply(result["result"]["id"], req["expected_version"])
    mark_applied(workspace, result["result"]["id"], job_id=job["job_id"])
    assert applied["rows"][1]["attributes"]["材质"]["value"] == "ABS塑料"
    assert read_json(workspace._path("jobs", job["job_id"], "J"))["status"] == "applied"


def test_scoped_apply_rejects_tampered_before_value(workspace):
    req = request(workspace, ["材质"], mapping={"材质": {"ABS": "ABS塑料"}})
    proposal = workspace.propose(req)
    path = workspace._path("proposals", proposal["id"], "P")
    stored = read_json(path)
    patch = next(p for p in stored["operation"]["patches"] if p["type"] == "cell")
    patch["before"]["value"] = "错误原值"
    import json
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="不匹配|校验失败"):
        workspace.apply(proposal["id"], req["expected_version"])
    assert workspace.open()["version"] == req["expected_version"]
