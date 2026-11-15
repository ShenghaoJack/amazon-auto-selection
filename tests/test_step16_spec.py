# 单元测试: 步骤16 Phase-0 属性集提取 → spec.json 固化(2026-09-21 重构)。
#
# 覆盖:
#   · tools.attr_stability.spec 纯函数(N 次抽取并集去重 / 同义归并提示词 / spec 读写)
#   · step16._extract_spec_attrs 的 N=3 抽取→合并→同义归并→说明补齐 流程
#   · step16.run() 产出 <cat>-attribute-spec.json, status=pending_confirm
import json
import os
from unittest import mock

import pandas as pd
import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step16_attribute_annotation import Step16AttributeAnnotation
from tools.attr_stability.spec import (
    SPEC_STATUS_PENDING,
    aggregate_hints,
    build_dedupe_merge_prompt,
    cat_rules_stem,
    create_rules_skeleton,
    load_spec,
    merge_extractions,
    save_spec,
)


class _FakeLLM:
    """按调用顺序返回 payload; 记录 prompt, 便于断言归并提示词不含 提取/标注。"""

    def __init__(self, payloads=None):
        self.payloads = list(payloads or [])
        self.prompts: list[str] = []

    def chat_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.payloads.pop(0) if self.payloads else {}


def _ctx(tmp_dir: str, extra: dict | None = None) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": tmp_dir, "DRY_RUN": True, "MARKET": "amazon.com"}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _step(ctx: StepContext) -> Step16AttributeAnnotation:
    st = Step16AttributeAnnotation()
    st.bind_cat(ctx)
    return st


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "父ASIN": ["B0P1", "B0P2"],
            "变体ASIN": ["B0V1", "B0V2"],
            "标题": ["Electric Salt and Pepper Grinder Set", "Manual Pepper Mill Wood"],
            "五点描述": ["automatic grinding | ceramic burr", "wood body | adjustable coarseness"],
        }
    )


def _attr(name: str, hint: str = "") -> dict:
    return {"name": name, "hint": hint, "source": "llm", "modified": False, "runs": 1}


# --------------------------- spec 纯函数 ---------------------------

def test_merge_extractions_unions_and_keeps_longest_hint():
    batches = [
        [_attr("产品类型", "短"), _attr("材质", "hint-A")],
        [_attr("产品类型", "这是一个更长的产品类型说明文字"), _attr("主体材质", "hint-B")],
        [_attr("材质", "hint-A-longer-than-A")],
    ]
    out = merge_extractions(batches)
    by_name = {a["name"]: a for a in out}
    assert set(by_name) == {"产品类型", "材质", "主体材质"}          # 并集去重
    assert by_name["产品类型"]["runs"] == 2                          # 出现 2 次
    assert by_name["材质"]["runs"] == 2
    assert by_name["产品类型"]["hint"] == "这是一个更长的产品类型说明文字"  # 同名取最长 hint


def test_aggregate_hints_skips_empty_name_and_counts_runs():
    out = aggregate_hints([_attr(""), _attr("材质", "x"), _attr("材质", "xy")])
    assert [a["name"] for a in out] == ["材质"]
    assert out[0]["runs"] == 2


def test_build_dedupe_merge_prompt_avoids_extract_words():
    prompt = build_dedupe_merge_prompt([_attr("材质", "h1"), _attr("主体材质", "h2")])
    for word in ("提取", "标注"):
        assert word not in prompt, "归并提示词应避免触发 dry-run mock 的 提取/标注 分支"
    assert "同义归并" in prompt
    assert "主体材质" in prompt and "材质" in prompt


def test_build_hint_summary_prompt_lists_attrs_and_demands_json():
    """属性说明精简提示词: 列全属性、要求原样保留 name、输出 summary JSON。"""
    from tools.attr_stability.spec import build_hint_summary_prompt

    prompt = build_hint_summary_prompt([_attr("材质", "hint-材质"), _attr("容量", "hint-容量")])
    assert "材质" in prompt and "容量" in prompt
    assert "hint-材质" in prompt and "hint-容量" in prompt
    assert "summary" in prompt                       # 输出字段为 summary
    assert "原样保留" in prompt                       # 要求 name 不变
    assert "≤ 40 字" in prompt or "40 字" in prompt  # 精简长度要求


def test_sync_rules_from_spec_adds_removes_and_preserves(tmp_path):
    """rules.json 对齐 spec: 新增缺失、删除多余、保留已有的 rule/params。"""
    import json as _json
    from tools.attr_stability.spec import sync_rules_from_spec

    rp = str(tmp_path / "cat.rules.json")
    _json.dump({"category": "c", "attributes": [
        {"name": "保留项", "rule": "multi", "params": {"values": ["a", "b"]}},  # 已有词表, 应保留
        {"name": "待删除项", "rule": "enum", "params": {}},                     # spec 已删, 应移除
    ]}, open(rp, "w", encoding="utf-8"))

    spec_attrs = [
        {"name": "保留项", "hint": "x"},    # 同名 → 保留原 rule/params
        {"name": "新增项", "hint": "y"},    # 缺失 → 追加默认骨架
    ]
    rep = sync_rules_from_spec(rp, spec_attrs)

    assert rep["added"] == ["新增项"]
    assert rep["removed"] == ["待删除项"]
    assert rep["kept"] == 1

    rules = _json.load(open(rp, encoding="utf-8"))
    names = [a["name"] for a in rules["attributes"]]
    assert names == ["保留项", "新增项"]                    # 顺序 = spec 顺序
    by = {a["name"]: a for a in rules["attributes"]}
    assert by["保留项"]["rule"] == "multi"                  # 已有 rule 不丢
    assert by["保留项"]["params"]["values"] == ["a", "b"]    # 已有词表不丢
    assert by["新增项"]["rule"] == "enum"                   # 新增给默认骨架
    assert by["新增项"]["params"] == {}


def test_find_rules_file_locates_flat_and_categories(tmp_path):
    """find_rules_file 依次找 categories/、平铺; 多个或无返回 None。"""
    from tools.attr_stability.spec import find_rules_file

    # 无文件 → None
    assert find_rules_file(str(tmp_path)) is None
    # 平铺唯一 → 命中
    flat = tmp_path / "cat.rules.json"
    flat.write_text("{}", encoding="utf-8")
    assert find_rules_file(str(tmp_path)) == str(flat)
    # categories/ 优先(先找 categories)
    os.makedirs(tmp_path / "categories", exist_ok=True)
    cat_dir_file = tmp_path / "categories" / "cat2.rules.json"
    cat_dir_file.write_text("{}", encoding="utf-8")
    # categories/ 下唯一 → 命中它(不管平铺)
    assert find_rules_file(str(tmp_path)) == str(cat_dir_file)


def test_cat_rules_stem_snakecases_display_name():
    """类目名 → 小写蛇形文件名主干(去 -US/-YYYYMM 等末尾后缀)。"""
    assert cat_rules_stem("Cutting Boards-US-202608") == "cutting_boards"
    assert cat_rules_stem("Cat Fountains-US-202608") == "cat_fountains"
    assert cat_rules_stem("t") == "t"
    # 非法字符折叠为下划线, 全空回落 category
    assert cat_rules_stem("A/B:C") == "a_b_c"
    assert cat_rules_stem("") == "category"


def test_create_rules_skeleton_writes_minimal(tmp_path):
    """create_rules_skeleton 写最小骨架: rule=enum、params 留空、category 用展示名。"""
    import json as _json

    rp = str(tmp_path / "cutting_boards.rules.json")
    spec_attrs = [{"name": "产品类型", "hint": "x"}, {"name": "主体材质", "hint": "y"}, {"name": ""}]
    out = create_rules_skeleton(rp, "Cutting Boards-US-202608", spec_attrs)

    assert out["category"] == "Cutting Boards"
    names = [a["name"] for a in out["attributes"]]
    assert names == ["产品类型", "主体材质"]           # 空名被跳过
    assert all(a["rule"] == "enum" and a["params"] == {} for a in out["attributes"])
    # 已落盘
    loaded = _json.load(open(rp, encoding="utf-8"))
    assert loaded == out


def test_sync_rules_autocreates_skeleton_when_missing(ws_tmp):
    """step16._sync_rules: 目录下无 rules.json → 按 spec 自动建骨架(choice A)。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st._cat = "t"
    attrs = [_attr("材质", "hint-材质"), _attr("容量", "hint-容量")]

    st._sync_rules(ctx, attrs)

    rp = os.path.join(ws_tmp, "t", "t.rules.json")
    assert os.path.exists(rp)
    rules = json.load(open(rp, encoding="utf-8"))
    assert [a["name"] for a in rules["attributes"]] == ["材质", "容量"]
    assert all(a["rule"] == "enum" for a in rules["attributes"])


def test_sync_rules_aligns_existing_rules(ws_tmp):
    """step16._sync_rules: 已有 rules.json → 走 sync_rules_from_spec 对齐(不重建)。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st._cat = "t"
    rp = os.path.join(ws_tmp, "t", "t.rules.json")
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump({"category": "t", "attributes": [
            {"name": "材质", "rule": "material", "params": {"values": ["不锈钢"]}},
        ]}, fh, ensure_ascii=False)

    st._sync_rules(ctx, [_attr("材质", "hint-材质"), _attr("容量", "hint-容量")])

    rules = json.load(open(rp, encoding="utf-8"))
    by = {a["name"]: a for a in rules["attributes"]}
    assert set(by) == {"材质", "容量"}
    assert by["材质"]["rule"] == "material"                  # 已有 rule 不丢
    assert by["材质"]["params"]["values"] == ["不锈钢"]       # 已有词表不丢
    assert by["容量"]["rule"] == "enum"                      # 新增给默认骨架


def test_spec_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "t-attribute-spec.json")
    spec = {"cat": "t", "status": SPEC_STATUS_PENDING, "attributes": [_attr("材质", "hint")]}
    assert save_spec(path, spec) == path
    loaded = load_spec(path)
    assert loaded == spec
    assert loaded["status"] == "pending_confirm"



def test_validate_artifacts_requires_spec(ws_tmp):
    """续跑前自愈校验: attribute-spec.json 缺失 → 抛错(重编号迁移后自动重置重跑)。"""
    from core.errors import StepDataError

    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    with pytest.raises(StepDataError):
        st.validate_artifacts(ctx)
    save_spec(ctx.resolve_artifact("t-attribute-spec.json"),
              {"cat": "t", "status": "pending_confirm", "attributes": []})
    st.validate_artifacts(ctx)   # 存在则通过
def test_final_boolean_review_merges_duplicates_but_protects_manual_and_incomplete():
    from steps.step16_attribute_annotation import _review_boolean_duplicates
    attrs = [
        {"name": "是否带夜视", "hint": "夜视", "runs": 3, "aliases": []},
        {"name": "是否有夜视", "hint": "夜视", "runs": 2, "aliases": []},
        {"name": "是否防水", "hint": "防水", "source": "manual", "runs": 3},
        {"name": "容量", "hint": "容积", "runs": 3},
    ]
    def merge(candidates):
        assert {x["name"] for x in candidates} == {"是否带夜视", "是否有夜视"}
        return [{"name": "是否带夜视", "aliases": ["是否有夜视"]}]
    out, count = _review_boolean_duplicates(attrs, merge)
    assert count == 1
    assert [x["name"] for x in out] == ["是否带夜视", "是否防水", "容量"]
    assert "是否有夜视" in out[0]["aliases"]
    assert _review_boolean_duplicates(attrs, lambda _: [{"name": "是否带夜视", "aliases": []}]) == (attrs, 0)

