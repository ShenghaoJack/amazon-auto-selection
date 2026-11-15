"""GitHub's 22 tags are references, with open LLM parent/child categories."""
import copy
import json

from tests.test_voc_classification import TaxonomyLLM, engine, evidence
from tests.test_voc_opportunity import FakeLLM, config, row
from tools.voc.classification import Classifier, CLASSIFY, CONSOLIDATE
from tools.voc.opportunity import Analyzer, EXTRACT
from tools.voc.reference_categories import REFERENCE_TAGS, SOURCE, metadata, policy
from tools.voc import annotate


def test_reference_table_is_exactly_22_github_tags_and_open():
    assert len(REFERENCE_TAGS) == len(set(REFERENCE_TAGS)) == 22
    assert SOURCE.startswith("https://github.com/buluslan/review-analyzer-skill/")
    assert policy()["mode"] == "open_reference_categories_llm_subcategories"
    assert metadata("耐用性")["reference_category_tag"] == "质量_耐用性"
    assert metadata("研磨颗粒稳定性")["category_origin"] == "llm_new"


def test_every_semantic_prompt_allows_new_categories_and_llm_subtypes():
    for prompt in (EXTRACT, CLASSIFY, CONSOLIDATE, annotate.build_prompt([row()])):
        assert "人群_性别" in prompt and "情感_总体评价" in prompt
        assert "新增大类" in prompt and "自主" in prompt
        assert "不根据姓名" in prompt
    assert "结合该大类和原文生成小类" in CLASSIFY


def test_outside_reference_category_is_retained_in_aspects_and_review_writeback(tmp_path):
    a = Analyzer(config(), tmp_path, "US", llm=FakeLLM(), log=lambda _: None)
    a.finalize([row()])
    chain = json.loads((tmp_path / "chain.json").read_text(encoding="utf-8"))
    assert all(x["category_origin"] == "llm_new" for x in chain["aspects"])
    review = json.loads((tmp_path / "逐条评论分类.json").read_text(encoding="utf-8"))[0]
    assert review["LLM新增大类"] and review["大类参考版本"] == policy()["version"]
    assert len(json.loads((tmp_path / "GitHub大类参考.json").read_text(encoding="utf-8"))["categories"]) == 22


def test_reference_parent_and_original_text_both_reach_llm(tmp_path):
    class Model(TaxonomyLLM):
        def chat_json(self, prompt, **kwargs):
            data = json.loads(prompt.split("\n")[-1])
            result = super().chat_json(prompt, **kwargs)
            if data["task"] == "voc_classify":
                assert "耐用性" in [c["name"] for c in data["reference_categories"]["categories"]]
                assert data["evidence"][0]["quote"] == "broke in a week"
                for item in result["items"]:
                    item.update(category="耐用性", attribute="盖体连接寿命")
            elif data["task"] == "voc_consolidate":
                result["groups"][0].update(category="耐用性", attribute="盖体连接寿命")
            return result
    data = {"aspects": [evidence("E1", "broke in a week", "negative")], "reviews": []}
    Classifier(engine(tmp_path, Model())).run(data)
    assert data["aspects"][0]["canonical_attribute"] == "盖体连接寿命"
    assert data["aspects"][0]["reference_category_tag"] == "质量_耐用性"


def test_extraction_category_hints_preserve_quotes_and_go_to_classifier(tmp_path):
    class Model(FakeLLM):
        def chat_json(self, prompt, **kwargs):
            data = json.loads(prompt.split("\n")[-1])
            result = super().chat_json(prompt, **kwargs)
            if isinstance(data, list):
                for part in result["parts"]:
                    for aspect in part["aspects"]:
                        aspect.update(category_hint="自主新增性能大类", subcategory_hint="原文体现的具体表现")
            elif data.get("task") == "voc_classify":
                assert all(x["category_hint"] == "自主新增性能大类" for x in data["evidence"])
            return result
    a = Analyzer(config(), tmp_path, "US", llm=Model(), log=lambda _: None)
    result = a.extract([row()])
    before = copy.deepcopy(result["aspects"])
    Classifier(a).run(result)
    assert all(x["category_hint"] == "自主新增性能大类" for x in result["aspects"])
    assert [x["quote"] for x in before] == [x["quote"] for x in result["aspects"]]


def test_compatibility_subclass_consolidation_accepts_new_names(tmp_path, monkeypatch):
    class Model:
        def chat_json(self, prompt, **kwargs):
            assert "盖体连接" in prompt and "大类" in prompt
            return {"issues": {"盖体连接易断": "盖体连接寿命"}, "scenes": {"露营车": "露营车"}}
    monkeypatch.setattr(annotate, "LLMClient", lambda *a, **k: Model())
    labels = [{"attribute": "耐用性", "subcategory": "盖体连接", "issue": "盖体连接易断",
               "scene": "露营车", "quote": "lid connection broke"}]
    annotate.canonicalize(labels, [row()], config(), cache_path=str(tmp_path / "labels.json"), log=lambda _: None)
    assert labels[0]["issue_canon"] == "盖体连接寿命" and labels[0]["scene_canon"] == "露营车"
