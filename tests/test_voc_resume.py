import json

import openpyxl
import pytest

from core.errors import LLMError
from tests.test_voc_opportunity import config, row
from tests.test_voc_solutions import FormalLLM, make_catalog
from tests.test_voc_mcp_recovery import collector, envelope
from tools.voc.opportunity import Analyzer
from tools.voc.solutions import load_catalog
from tools.voc import pipeline as p


def test_completed_phases_resume_and_missing_export_is_rebuilt(tmp_path):
    llm = FormalLLM()
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=llm, log=lambda _: None)
    engine.finalize([row()])
    calls = engine.calls
    (engine.directory / "逐条评论分类.csv").unlink()
    engine.finalize([row()])
    assert engine.calls == calls
    assert (engine.directory / "逐条评论分类.csv").exists()
    checkpoints = json.loads((engine.directory / "阶段断点.json").read_text(encoding="utf-8"))
    assert all(checkpoints["stages"][name]["reused"] for name in ("方面提取", "方面分类", "Need归并", "步骤18核查与Open"))


def test_interrupted_solution_resumes_prior_phases_and_successful_tasks(tmp_path):
    class Flaky(FormalLLM):
        block = True
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            if isinstance(payload, dict) and payload.get("subject_id") == "C" and self.block:
                raise LLMError("connection lost")
            return super().chat_json(prompt, **kwargs)
    llm = Flaky()
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=llm, log=lambda _: None)
    with pytest.raises(RuntimeError, match="待重试"):
        engine.finalize([row()])
    before = len(llm.stages)
    llm.block = False
    engine.finalize([row()])
    assert llm.stages[before:] == ["solve", "open", "audit"]
    checkpoint = json.loads((engine.directory / "阶段断点.json").read_text(encoding="utf-8"))
    assert checkpoint["stages"]["Need归并"]["reused"]


def test_new_step18_export_invalidates_solution_phase_only(tmp_path):
    llm = FormalLLM()
    catalog = make_catalog(tmp_path)
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=catalog, llm=llm, log=lambda _: None)
    engine.finalize([row()])
    path = catalog["source"]["path"]
    wb = openpyxl.load_workbook(path)
    wb["属性标注"]["E2"] = "三档"
    wb.save(path)
    wb.close()
    engine.catalog = load_catalog(tmp_path, "test", "US")
    before = len(llm.stages)
    engine.finalize([row()])
    assert "need" not in llm.stages[before:]
    cp = json.loads((engine.directory / "阶段断点.json").read_text(encoding="utf-8"))["stages"]
    assert all(cp[name]["reused"] for name in ("方面提取", "方面分类", "Need归并"))
    assert not cp["步骤18核查与Open"]["reused"]


def test_corrupt_phase_snapshot_is_rebuilt_not_trusted(tmp_path):
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=FormalLLM(), log=lambda _: None)
    engine.finalize([row()])
    cp = json.loads((engine.directory / "阶段断点.json").read_text(encoding="utf-8"))
    from pathlib import Path
    path = Path(cp["stages"]["方面提取"]["snapshot"])
    cached = json.loads(path.read_text(encoding="utf-8"))
    cached["result"]["reviews"][0]["text"] = "corrupt"
    path.write_text(json.dumps(cached), encoding="utf-8")
    engine.finalize([row()])
    chain = json.loads((engine.directory / "chain.json").read_text(encoding="utf-8"))
    assert chain["reviews"][0]["text"] == "Fast but noisy."


def test_export_failure_resumes_without_model_calls(tmp_path, monkeypatch):
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=FormalLLM(), log=lambda _: None)
    original = engine.export
    def broken(chain):
        raise OSError("export locked")
    monkeypatch.setattr(engine, "export", broken)
    with pytest.raises(OSError, match="export locked"):
        engine.finalize([row()])
    before = engine.calls
    cp = json.loads((engine.directory / "阶段断点.json").read_text(encoding="utf-8"))
    assert cp["stages"]["导出"]["status"] == "pending"
    monkeypatch.setattr(engine, "export", original)
    engine.finalize([row()])
    assert engine.calls == before


def test_changed_reviews_invalidate_extraction_checkpoint(tmp_path):
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=FormalLLM(), log=lambda _: None)
    engine.finalize([row()])
    before = engine.calls
    engine.finalize([row(body="Fast but noisy. Update: new context.")])
    cp = json.loads((engine.directory / "阶段断点.json").read_text(encoding="utf-8"))
    assert not cp["stages"]["方面提取"]["reused"]
    assert engine.calls > before


def test_saved_raw_repairs_missing_derived_review_file_without_mcp(tmp_path, monkeypatch):
    calls = []
    def call(source, args):
        calls.append(source)
        return envelope(source)
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 1, "critical:1")
    from pathlib import Path
    Path(c.rev_path["pangolinfo"]).unlink()
    resumed = collector(tmp_path, monkeypatch, call)
    resumed.collect_parent("P", [], 1, "critical:1")
    assert calls == ["pangolinfo"]
    assert p.read_jsonl(resumed.rev_path["pangolinfo"])[0]["reviewId"] == "R1"


def test_sellersprite_page_checkpoint_reuses_paid_page_after_restart(tmp_path, monkeypatch):
    pages = []
    state = {"broken": True}
    def call(source, args):
        if source == "pangolinfo":
            raise RuntimeError("network timeout")
        page = args["page"]
        pages.append(page)
        if page == 2 and state["broken"]:
            raise RuntimeError("network timeout")
        result = envelope(source)
        result["data"]["items"][0]["content"] = f"page{page}"
        return result
    c = collector(tmp_path, monkeypatch, call)
    c.collect_parent("P", [], 3, "critical:3")
    assert c.failed_parents and c.points == 1
    state["broken"] = False
    resumed = collector(tmp_path, monkeypatch, call)
    resumed.collect_parent("P", [], 3, "critical:3")
    assert pages == [1, 2, 2, 3]
    assert resumed.points == resumed._spent_points() == 3
    _, stats = resumed.rebuild_audit(str(tmp_path / "audit.csv"))
    assert stats["points"] == stats["pages"] == 3
    from tools.voc.report import load_audit
    audit, coverage = load_audit(list(resumed.raw_path.values()))
    assert len([r for r in audit if r["status"] == "ok"]) == 3
    assert coverage["P"]["fetched"] == 3


def test_received_transport_reply_can_restore_missing_raw_commit(tmp_path, monkeypatch):
    tmp_path.mkdir(exist_ok=True)
    p.append_jsonl(str(tmp_path / "MCP完整调用回执.jsonl"), [{"source": "pangolinfo",
        "context": {"parent_asin": "P", "queried_asin": "P", "stratum": "critical", "pages": 1},
        "raw": envelope("pangolinfo")}])
    def unexpected(*args):
        pytest.fail("已返回成功回执不应重新调用MCP")
    c = collector(tmp_path, monkeypatch, unexpected)
    c.collect_parent("P", [], 1, "critical:1")
    assert c.points == 5
    assert p.read_jsonl(c.rev_path["pangolinfo"])


def test_truncated_jsonl_tail_does_not_swallow_next_checkpoint(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text('{"incomplete":', encoding="utf-8")
    p.append_jsonl(str(path), [{"status": "ok"}])
    assert p.read_jsonl(str(path)) == [{"status": "ok"}]


def test_incomplete_legacy_raw_is_not_a_false_completed_checkpoint(tmp_path, monkeypatch):
    p.append_jsonl(str(tmp_path / "sellersprite_raw.jsonl"), [{"source": "sellersprite", "parent_asin": "P",
        "queried_asin": "P", "stratum": "critical", "pages": 2, "rows": 20, "pointCost": 2,
        "status": "ok", "raw": envelope("sellersprite")}])
    c = collector(tmp_path, monkeypatch, lambda source, args: envelope(source))
    assert ("sellersprite", "P", "P", "critical") not in c.done
    assert (tmp_path / "历史回执恢复缺口.json").exists()
