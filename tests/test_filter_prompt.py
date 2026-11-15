"""FR-02 过滤提示词: 产品定义 + 5 类词(无"达标关键词"), 判定提示词回退与步骤10 注入。"""
from steps._filtering import (
    build_filter_prompt,
    build_judgment_prompt,
    enrich_prompt_with_keywords,
)


def _rec(title="Salt and Pepper Shaker Set"):
    return {"title": title, "bullets": ["a", "b"]}


def test_filter_prompt_keeps_product_and_related():
    fp = build_filter_prompt(
        "cutting-board-v2",
        "排除无关",
        {"product_scope": "砧板, 含竹/木/塑料",
         "related_title": ["cutting board", "chopping board"]},
    )
    assert fp["category"] == "cutting board"          # 展示名清洗
    assert fp["产品"] == "砧板, 含竹/木/塑料"
    assert fp["related_title"] == ["cutting board", "chopping board"]
    assert "达标关键词" not in fp                       # 步骤2 产物不写达标关键词


def test_filter_prompt_missing_scope_fallback_empty():
    fp = build_filter_prompt("x", "r", {"related_title": ["a"]})
    assert fp["产品"] == ""
    assert fp["related_title"] == ["a"]
    assert "达标关键词" not in fp


def test_judge_prompt_has_related_and_product():
    fp = build_filter_prompt(
        "salt-pepper-shaker-sets",
        "排除无关",
        {"product_scope": "调味瓶与研磨器",
         "related_title": ["pepper grinder", "pepper mill", "salt and pepper shakers"]},
    )
    p = build_judgment_prompt(fp, [_rec("Pepper Grinder Set")])
    assert "本类目产品: 调味瓶与研磨器" in p
    assert "相关标题词" in p          # 无达标关键词时标签为相关标题词
    assert "达标关键词" not in p
    assert "pepper grinder" in p


def test_judge_prompt_old_file_fallback():
    # 旧版提示词无 产品 字段: 用 related_title 兜底, 不抛错
    old = {"category": "x", "requirement": "r",
           "related_title": ["salt shaker"], "hard_exclude": [], "related_bullets": [],
           "accessories": [], "consumables": []}
    p = build_judgment_prompt(old, [_rec()])
    assert "salt shaker" in p
    assert "达标关键词" not in p


def test_step10_enrich_injects_final_keywords():
    fp = build_filter_prompt("shaker-sets", "r", {
        "related_title": ["salt and pepper shakers"],
        "hard_exclude": ["pepper grinder", "salt pig"],
        "accessories": ["shaker lid"],
    })
    fp["产品"] = "调味瓶与研磨器"
    out = enrich_prompt_with_keywords(fp, ["pepper grinder", "pepper mill", "electric salt and pepper grinder"])
    assert out["达标关键词"] == ["salt and pepper shakers", "pepper grinder", "pepper mill", "electric salt and pepper grinder"]
    assert "pepper grinder" not in out["hard_exclude"]   # 冲突词从硬排除移除
    assert "pepper mill" in out["related_title"]
    assert out["产品"] == "调味瓶与研磨器"
    # 注入后判定提示词含"达标关键词"标签
    p = build_judgment_prompt(out, [_rec("Electric Salt and Pepper Grinder")])
    assert "达标关键词" in p and "pepper mill" in p and "本类目产品: 调味瓶与研磨器" in p
