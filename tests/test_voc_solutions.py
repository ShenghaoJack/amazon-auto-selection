import json
from pathlib import Path
from threading import Event
import time

import openpyxl
import pytest

from core.errors import LLMError
from tests.test_voc_opportunity import FakeLLM, config, row
from tools.voc.opportunity import Analyzer
from tools.voc.solutions import load_catalog
from tools.voc.tasks import BatchQueue, DeadlineCalls, TaskDeferred


def make_catalog(root, cat="test", *, reorder=False):
    root = Path(root)
    folder = root / "18_标准化"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cat}-属性标注-标准化交付.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    header = ["变体ASIN", "父ASIN", "品牌", "新老品", "速度设置", "主体材质"]
    for name in ("属性标注", "来源", "依据"):
        ws = wb.create_sheet(name)
        ws.append(header)
        ids = ["V", "B", "C"]
        if reorder and name != "属性标注":
            ids.reverse()
        for asin in ids:
            attrs = ["双档" if asin == "V" else "可调" if asin == "B" else "单档", "未明确"]
            if name != "属性标注":
                attrs = [f"{asin}-{name}-速度", f"{asin}-{name}-材质"]
            ws.append([asin, "P", "Brand", "老品", *attrs])
    wb.save(path)
    wb.close()
    return load_catalog(root, cat, "US")


class FormalLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.stages = []

    def chat_json(self, prompt, **kwargs):
        p = json.loads(prompt.split("\n")[-1])
        if not isinstance(p, dict):
            return super().chat_json(prompt, **kwargs)
        if p.get("task") == "voc_need_only":
            self.stages.append("need")
            result = super().chat_json(prompt, **kwargs)
            result["groups"][0]["open"] = None
            return result
        if "columns" in p and "need" in p:
            self.stages.append("map")
            return {"columns": p["columns"][:1], "reason": "可核查能力，不能证明效果"}
        if "subject_id" in p and "cells" in p:
            self.stages.append("solve")
            pos = [a["evidence_id"] for a in p["evidence"] if a["polarity"] == "positive"]
            return {"status": "部分解决" if pos else "待验证", "cell_ids": [c["cell_id"] for c in p["cells"]],
                    "support_ids": pos, "counter_ids": [a["evidence_id"] for a in p["evidence"] if a["polarity"] == "negative"],
                    "boundary_ids": [a["evidence_id"] for a in p["evidence"] if a["polarity"] == "uncertain"],
                    "capability_summary": "已有可调能力", "result": "仅支持所述场景", "limitations": "缺少实测且存在反例"}
        if "solutions" in p and "need" in p:
            self.stages.append("audit" if "draft" in p else "open")
            if not any(a["polarity"] == "negative" for a in p["evidence"]):
                return {"open": None}
            return {"open": {"remaining_gap": "同款仍有未满足结果", "gap_type": "both",
                "solution_ids": [s["solution_id"] for s in p["solutions"]], "evidence_ids": p["need"]["evidence_ids"],
                "claimed_cells": sorted({c for s in p["solutions"] for c in s["annotation_ids"]}),
                "preserve": "已认可表现", "mechanisms": ["改善同工况表现"], "alternatives": ["其他实现方式"],
                "tradeoffs": ["性能取舍待测"], "hypotheses": ["是否能消除该结果缺口"], "validation_plan": "对照现有配置测试"}}
        return super().chat_json(prompt, **kwargs)


def test_standardized_join_is_by_asin_and_retains_provenance(tmp_path):
    catalog = make_catalog(tmp_path, reorder=True)
    v = next(p for p in catalog["products"] if p["product_key"] == "V")
    cell = v["attributes"]["速度设置"]
    assert cell["value"] == "双档" and cell["source"] == "V-来源-速度" and cell["basis"] == "V-依据-速度"
    assert cell["location"]["cell"] == "E2"
    assert cell["location"]["source_cell"] == "E4"
    assert catalog["columns"] == ["速度设置", "主体材质"]


def test_formal_chain_order_and_review_writeback(tmp_path):
    catalog = make_catalog(tmp_path)
    llm = FormalLLM()
    out = tmp_path / "output"
    engine = Analyzer(config(), out, "US", catalog=catalog, llm=llm, log=lambda _: None)
    result = engine.finalize([row()])
    chain = json.loads((out / "chain.json").read_text(encoding="utf-8"))
    assert result["solution_check"]["status"] == "complete"
    assert llm.stages == ["need", "map", "solve", "solve", "solve", "open", "audit"]
    assert chain["solutions"][0]["status"] == "部分解决"
    assert all(not s["support_ids"] and not s["counter_ids"] for s in chain["solutions"][1:])
    assert all(c["evidence_type"] == "annotation" for c in chain["standardized_cells"])
    assert chain["claims"] == []
    view = json.loads((out / "逐条评论分类.json").read_text(encoding="utf-8"))[0]
    assert view["text"] == "Fast but noisy."
    assert "部分解决" in view["已有解法与解决程度"]
    assert "V-依据-速度" in view["步骤18功能属性与来源依据"]
    assert view["候选Need"] and view["候选Open"] and view["验证方法"]
    assert (out / "已有解法与解决程度.csv").exists()
    assert list(out.glob("步骤18标准化交付-*.xlsx"))


def test_conditional_positive_boundary_is_preserved_in_real_solution_chain(tmp_path):
    from tools.voc.solution_retry import install_solution_retry
    class ConditionalLLM(FormalLLM):
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            result = super().chat_json(prompt, **kwargs)
            if isinstance(payload, dict) and payload.get("task") == "voc_classify":
                polarity = {a["id"]: a["polarity"] for a in payload["evidence"]}
                for item in result["items"]:
                    if polarity[item["id"]] == "positive":
                        item["conditions"] = ["特定使用条件"]
            if isinstance(payload, dict) and "subject_id" in payload and "cells" in payload:
                conditional = [a["evidence_id"] for a in payload["evidence"]
                               if a["polarity"] == "positive" and a.get("conditions")]
                if conditional:
                    result.update(status="待验证", support_ids=[], boundary_ids=conditional)
            return result
    out = tmp_path / "output"
    analyzer = Analyzer(config(), out, "US", catalog=make_catalog(tmp_path), llm=ConditionalLLM(), log=lambda _: None)
    install_solution_retry(analyzer)
    analyzer.finalize([row()])
    chain = json.loads((out / "chain.json").read_text(encoding="utf-8"))
    evidence = {a["evidence_id"]: a for a in chain["aspects"]}
    boundaries = [ref for solution in chain["solutions"] for ref in solution["boundary_ids"]]
    assert boundaries and all(evidence[ref]["polarity"] == "positive" and evidence[ref]["conditions"] for ref in boundaries)
    assert all(not solution["support_ids"] for solution in chain["solutions"])


@pytest.mark.parametrize("source,parent,brand", [("sellersprite", "P", None), ("pangolinfo", "OTHER", None),
                                               ("pangolinfo", "P", "Conflicting")])
def test_unknown_or_conflicting_identity_does_not_inherit_solution(tmp_path, source, parent, brand):
    catalog = make_catalog(tmp_path)
    out = tmp_path / "output"
    engine = Analyzer(config(), out, "US", catalog=catalog, llm=FormalLLM(), log=lambda _: None)
    with pytest.raises(RuntimeError, match="准入后无可分析"):
        engine.finalize([row(source=source, parent_asin=parent, brand=brand)])
    assert engine.calls == 0
    admission = json.loads((out / "评论准入.json").read_text(encoding="utf-8"))
    assert admission["summary"]["excluded_reviews"] == 1
    assert not (out / "chain.json").exists()


def test_unsolved_or_fabricated_claim_is_rejected_and_deferred(tmp_path):
    class Fabricated(FormalLLM):
        def chat_json(self, prompt, **kwargs):
            result = super().chat_json(prompt, **kwargs)
            if "cell_ids" in result:
                result.update(status="已解决", cell_ids=["wrong-cell"])
            return result
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=Fabricated(), log=lambda _: None)
    with pytest.raises(RuntimeError, match="已有解法核查"):
        engine.finalize([row()])
    assert not (engine.directory / "chain.json").exists()
    assert json.loads((engine.directory / "需求机会链摘要.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_positive_only_need_keeps_solutions_without_forcing_open(tmp_path):
    class PraiseOnly(FormalLLM):
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            result = super().chat_json(prompt, **kwargs)
            if isinstance(payload, list):
                for part in result["parts"]:
                    part["aspects"] = [a for a in part["aspects"] if a["polarity"] == "positive"]
            return result
    llm = PraiseOnly()
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=llm, log=lambda _: None)
    result = engine.finalize([row()])
    assert result["needs"] == 1 and result["open_candidates"] == 0
    assert "solve" in llm.stages and "open" not in llm.stages
    chain = json.loads((engine.directory / "chain.json").read_text(encoding="utf-8"))
    assert not chain["open_screening"][0]["eligible"]


def test_solution_failure_runs_other_variants_before_retry(tmp_path):
    class Flaky(FormalLLM):
        failed = False
        calls_order = []
        def chat_json(self, prompt, **kwargs):
            payload = json.loads(prompt.split("\n")[-1])
            if isinstance(payload, dict) and "subject_id" in payload:
                self.calls_order.append(payload["subject_id"])
                if not self.failed:
                    self.failed = True
                    raise LLMError("connection lost")
            return super().chat_json(prompt, **kwargs)
    llm = Flaky()
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=make_catalog(tmp_path), llm=llm, log=lambda _: None)
    engine.finalize([row()])
    # Arrival order of independent calls is intentionally unspecified; failed
    # work must still retry only after all first-pass variants have run.
    assert set(llm.calls_order[:3]) == {"V", "B", "C"}
    assert llm.calls_order[3:] == [llm.calls_order[0]]


def test_step18_changed_during_solutions_does_not_publish_complete(tmp_path):
    catalog = make_catalog(tmp_path)
    class ChangesFile(FormalLLM):
        def chat_json(self, prompt, **kwargs):
            response = super().chat_json(prompt, **kwargs)
            if "cell_ids" in response:
                with Path(catalog["source"]["path"]).open("ab") as stream:
                    stream.write(b"changed")
            return response
    engine = Analyzer(config(), tmp_path / "out", "US", catalog=catalog, llm=ChangesFile(), log=lambda _: None)
    with pytest.raises(ValueError, match="发生变化"):
        engine.finalize([row()])
    assert not (engine.directory / "chain.json").exists()


def test_unexported_current_step18_version_is_rejected(tmp_path):
    make_catalog(tmp_path)
    store = tmp_path / "18_标准化/版本数据"
    store.mkdir()
    (store / "current.json").write_text('{"version":"new"}', encoding="utf-8")
    (store / "export.json").write_text('{"version":"old"}', encoding="utf-8")
    with pytest.raises(ValueError, match="尚未导出"):
        load_catalog(tmp_path, "test", "US")


def test_deadline_progresses_and_consumes_late_result_without_resending():
    release, started = Event(), Event()
    calls = []
    deadlines = DeadlineCalls(0.02, 2)
    def stalled():
        started.set()
        calls.append(1)
        assert release.wait(1)
        return "late-result"
    try:
        with pytest.raises(TaskDeferred):
            deadlines.call("same", stalled)
        assert started.is_set()
        assert deadlines.call("other", lambda: "other-result") == "other-result"
        with pytest.raises(TaskDeferred):
            deadlines.call("same", stalled)
        release.set()
        deadline = time.monotonic() + 1
        while not deadlines.active["same"]["done"].is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert deadlines.call("same", stalled) == "late-result"
        assert calls == [1]
    finally:
        release.set()


def test_pending_queue_does_not_hide_completed_independent_jobs(tmp_path):
    engine = Analyzer(config(), tmp_path, "US", llm=FormalLLM(), log=lambda _: None)
    queue = BatchQueue(engine, "isolated", [["bad"], ["good"]], split=False)
    order = []
    while queue:
        job = queue.pop()
        order.append(job[0])
        if job[0] == "bad":
            queue.fail(job, LLMError("timeout"))
        else:
            queue.record(job, "complete")
    assert order == ["bad", "good", "bad"]
    with pytest.raises(RuntimeError, match="待重试"):
        queue.finish()
