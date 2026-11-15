# 单元测试: 步骤17 属性标注与标准化(2026-09-21 Phase 2, 文件仍名 step16b)
# ---------------------------------------------------------------------------
# 锁定: number=17 / 标题 / 两张 xlsx 产物 / validate_upstream 依赖已确认 spec.json + 步骤14
import json
import os
import re
from pathlib import Path

import pytest

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step17_attr_annotation import Step17AttrAnnotation


def _ctx(tmp_dir: str, extra: dict | None = None) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": tmp_dir, "DRY_RUN": True, "MARKET": "amazon.com"}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _write_spec(root: str, status: str = "confirmed") -> Path:
    p = Path(root) / "t" / "t-attribute-spec.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cat": "t", "status": status, "attributes": [
        {"name": "材质", "hint": "不锈钢/陶瓷"},
    ]}, ensure_ascii=False), encoding="utf-8")
    return p


def _write_split(root: str) -> Path:
    p = Path(root) / "t" / "14_流量拆分"
    p.mkdir(parents=True, exist_ok=True)
    f = p / "流量拆分数据-需人工补充.xlsx"
    f.write_text("x", encoding="utf-8")   # 仅存在性校验, 不真正读
    return f


def test_step17_number_and_title():
    st = Step17AttrAnnotation()
    assert st.number == 17
    assert st.title == "属性标注"


def test_step17_artifacts(ws_tmp):
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    arts = st.artifacts
    assert "17_属性标注/稳定性结果.json" in arts
    assert "17_属性标注/t-属性标注-单遍原始.xlsx" in arts
    assert "17_属性标注/唯一值统计.json" in arts


def test_step17_validate_upstream_requires_confirmed_spec(ws_tmp):
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    _write_split(ws_tmp)
    # 无 spec → 拒绝
    with pytest.raises(Exception, match="attribute-spec"):
        st.validate_upstream(ctx)
    # 未确认 → 拒绝
    _write_spec(ws_tmp, status="pending_confirm")
    with pytest.raises(Exception, match="未确认"):
        st.validate_upstream(ctx)
    # 确认 → 通过
    _write_spec(ws_tmp, status="confirmed")
    st.validate_upstream(ctx)
    assert [a["name"] for a in st._spec_attributes] == ["材质"]


def test_step17_validate_upstream_requires_split(ws_tmp):
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    _write_spec(ws_tmp, status="confirmed")
    with pytest.raises(Exception, match="14_流量拆分"):
        st.validate_upstream(ctx)
    _write_split(ws_tmp)
    st.validate_upstream(ctx)   # 通过


# --------------------------- Phase 2 增量: 归并标准化 + 视觉补充辅助 ---------------------------

def test_spec_attr_names(ws_tmp):
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    _write_spec(ws_tmp, status="confirmed")
    _write_split(ws_tmp)
    st.validate_upstream(ctx)
    assert st._spec_attr_names() == ["材质"]


def test_row_image_urls_dedup_and_clean():
    st = Step17AttrAnnotation()
    row = {"图片URL": "https://x/a._SL1500_.jpg|https://x/a.jpg; https://x/b.png\nhttps://x/a.jpg"}
    urls = st._row_image_urls(row, "图片URL")
    assert urls == ["https://x/a.jpg", "https://x/b.png"]
    assert st._row_image_urls({"标题": "无图"}, "图片URL") == []


def test_row_image_urls_keeps_all_images_no_cap():
    """视觉补充读全部图片: 不再 [:4] 截断(背面/标签细节在 5+ 张副图里)。"""
    st = Step17AttrAnnotation()
    many = "|".join("https://x/%d.jpg" % i for i in range(9))
    urls = st._row_image_urls({"图片URL": many}, "图片URL")
    assert len(urls) == 9


def test_is_text_reading_attr():
    st = Step17AttrAnnotation()
    for yes in ("图片OCR文字", "ocr_text", "雕刻文字", "认证标记", "logo", "印刷文字"):
        assert st._is_text_reading_attr(yes), yes
    for no in ("颜色", "design", "package", "材质", "尺寸"):
        assert not st._is_text_reading_attr(no), no


def test_classify_prompt_contains_硬性要求():
    st = Step17AttrAnnotation()
    prompt = st._classify_prompt_text({"_任务": "x", "_硬性要求": ["必须来自词表"], "待归类": []})
    assert "语义归类" in prompt and "归类" in prompt
    assert "严格输出 JSON" in prompt


def test_step17_validate_artifacts_requires_xlsx(ws_tmp):
    """续跑前自愈校验: 两张交付 xlsx 缺失 → 抛错(重编号迁移后自动重置重跑)。"""
    from core.errors import StepDataError

    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    # 只写 稳定性结果.json, 缺两张 xlsx
    out = Path(ws_tmp) / "t" / "17_属性标注与标准化"
    out.mkdir(parents=True, exist_ok=True)
    (out / "稳定性结果.json").write_text(
        json.dumps({"metrics_delivery": {"value": {"rate": 90.0}}}), encoding="utf-8")
    with pytest.raises(StepDataError):
        st.validate_artifacts(ctx)


def test_write_deliverables_adds_source_sheet(ws_tmp):
    """标准化交付 xlsx 应有第二个 sheet「来源」(来源列 = 属性_来源); 单遍原始不追加。"""
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    out = Path(ws_tmp) / "t" / "17_属性标注与标准化"
    out.mkdir(parents=True, exist_ok=True)
    rows = [{"xl_row": 2, "父ASIN": "B0P1", "标题": "Pepper Mill"}]

    class _Res:
        deliverable = [{"xl_row": 2, "attributes": {
            "材质": "不锈钢", "材质_来源": "标题",
        }}]

    st._write_deliverables(ctx, str(out), rows, _Res())

    import openpyxl
    std = openpyxl.load_workbook(out / "t-属性标注-标准化交付.xlsx")
    raw = openpyxl.load_workbook(out / "t-属性标注-单遍原始.xlsx")
    # 2026-10-09：新增「依据」sheet（逐属性原文依据，见提示词 §3.3e）——
    # 「来源」是标签、「依据」是原文，人工复核要分开看。
    assert std.sheetnames == ["属性标注", "来源", "依据"]
    assert raw.sheetnames == ["属性标注"]
    # 来源 sheet 的材质格 = "标题"(新 schema 只存来源, header=[父ASIN,标题,材质], 材质在第3列)
    src_ws = std["来源"]
    assert src_ws.cell(row=2, column=3).value == "标题"


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, prompt, **kw):
        return self.payload


def test_review_vocab_auto_generates_verdict(ws_tmp):
    """半自动新词初审: AI 先过一遍 → 写 新词初审裁决.json(status=pending_review)。"""
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM({"items": [
        {"属性": "供电方式", "词": "太阳能充电", "结论": "采纳", "规范化写法": "太阳能供电", "理由": "真实存在"},
        {"属性": "材质", "词": "钛", "结论": "驳回", "规范化写法": "", "理由": "误读"},
    ]})

    class _Res:
        vocab_candidates = [{"属性": "供电方式", "词": "太阳能充电"}]

    out = Path(ws_tmp) / "t" / "17_属性标注与标准化"
    out.mkdir(parents=True, exist_ok=True)
    st._review_vocab(ctx, str(out), _Res())

    verdict = json.loads((out / "新词初审裁决.json").read_text(encoding="utf-8"))
    assert verdict["status"] == "pending_review"
    assert len(verdict["items"]) == 2


def test_review_vocab_auto_onboard_writes_rules(ws_tmp):
    """全自动(ATTR_STAB_AUTO_ONBOARD=true): AI 初审后自动把「采纳」写回 rules.json。"""
    ctx = _ctx(ws_tmp, {"ATTR_STAB_AUTO_ONBOARD": "true"})
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM({"items": [
        {"属性": "供电方式", "词": "太阳能充电", "结论": "采纳", "规范化写法": "太阳能供电", "理由": "真实"},
        {"属性": "供电方式", "词": "solar pwoer", "结论": "驳回", "规范化写法": "", "理由": "笔误"},
    ]})

    class _Res:
        vocab_candidates = [{"属性": "供电方式", "词": "太阳能充电"}]

    # 类目规则文件(平铺)
    cat_dir = Path(ws_tmp) / "t"
    cat_dir.mkdir(parents=True, exist_ok=True)
    rules = cat_dir / "cat_x.rules.json"
    rules.write_text(json.dumps({"attributes": [
        {"name": "供电方式", "rule": "enum", "subcols": ["供电方式_标准"],
         "params": {"values": ["电池供电", "USB供电"]}},
    ]}, ensure_ascii=False), encoding="utf-8")

    out = cat_dir / "17_属性标注与标准化"
    out.mkdir(parents=True, exist_ok=True)
    st._review_vocab(ctx, str(out), _Res(), str(cat_dir), "cat_x")

    saved = json.loads(rules.read_text(encoding="utf-8"))
    assert saved["attributes"][0]["params"]["values"] == ["电池供电", "USB供电", "太阳能供电"]
    verdict = json.loads((out / "新词初审裁决.json").read_text(encoding="utf-8"))
    assert verdict["status"] == "auto_applied"


def test_generate_vocab_cold_start(ws_tmp):
    """冷启动: 无词表 enum 属性 → 收集唯一值 → LLM 聚类 → 写回 rules.json + 更新内存 rules。"""
    from tools.attr_stability import AttrSpec

    ctx = _ctx(ws_tmp, {"ATTR_STAB_GEN_VOCAB": "true"})
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM({"attributes": [
        {"属性": "供电方式", "规范值": ["电池供电", "USB供电", "太阳能供电"]},
    ]})

    rules = type("R", (), {"attrs": {
        "供电方式": AttrSpec(name="供电方式", values=[], type="enum", shape="single",
                             vocab_policy="closed"),
    }})()

    class _Res:
        cells = {"2|attr.供电方式": {"kind": "value", "values": [
            {"value": "电池"}, {"value": "太阳能充电"}, {"value": "USB"},
        ]}}

    cat_dir = Path(ws_tmp) / "t"
    cat_dir.mkdir(parents=True, exist_ok=True)
    rules_json = cat_dir / "cat_x.rules.json"
    rules_json.write_text(json.dumps({"attributes": [
        {"name": "供电方式", "rule": "enum", "subcols": ["供电方式_标准"], "params": {}},
    ]}, ensure_ascii=False), encoding="utf-8")

    st._generate_vocab(ctx, rules, _Res(), str(cat_dir), "cat_x")

    assert rules.attrs["供电方式"].values == ["电池供电", "USB供电", "太阳能供电"]
    saved = json.loads(rules_json.read_text(encoding="utf-8"))
    assert saved["attributes"][0]["params"]["values"] == ["电池供电", "USB供电", "太阳能供电"]


def test_generate_vocab_writes_mapping_report(ws_tmp):
    """冷启动聚类同时落「原值→规范值」归并报告(供人工审核 + Web 弹窗)。"""
    from tools.attr_stability import AttrSpec

    ctx = _ctx(ws_tmp, {"ATTR_STAB_GEN_VOCAB": "true"})
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM({"attributes": [
        {"属性": "供电方式", "规范值": ["电池供电", "USB供电"],
         "归并": [
             {"原值": "电池", "归入": "电池供电"},
             {"原值": "太阳能充电", "归入": "USB供电"},
             {"原值": "USB", "归入": "USB供电"},
         ]},
    ]})

    rules = type("R", (), {"attrs": {
        "供电方式": AttrSpec(name="供电方式", values=[], type="enum", shape="single",
                             vocab_policy="closed"),
    }})()

    class _Res:
        cells = {"2|attr.供电方式": {"kind": "value", "values": [
            {"value": "电池"}, {"value": "太阳能充电"}, {"value": "USB"},
        ]}}

    cat_dir = Path(ws_tmp) / "t"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / "cat_x.rules.json").write_text(json.dumps({"attributes": [
        {"name": "供电方式", "rule": "enum", "subcols": ["供电方式_标准"], "params": {}},
    ]}, ensure_ascii=False), encoding="utf-8")

    st._generate_vocab(ctx, rules, _Res(), str(cat_dir), "cat_x")

    rep_p = cat_dir / "18_标准化" / "词表聚类报告.json"
    assert rep_p.exists()
    rep = json.loads(rep_p.read_text(encoding="utf-8"))
    entries = rep["attributes"][0]["归并"]
    assert {e["原值"] for e in entries} == {"电池", "太阳能充电", "USB"}
    by_src = {e["原值"]: e["归入"] for e in entries}
    assert by_src["电池"] == "电池供电"
    assert by_src["太阳能充电"] == "USB供电"
    assert "hints" in rep   # 口径建议（数字粒度已删）


def test_apply_vocab_acceptance_new_format_params_values(tmp_path):
    """apply_vocab_acceptance 写回新格式 params.values, 且去重/驳回不写。"""
    from tools.attr_stability.onboard import apply_vocab_acceptance

    rules = tmp_path / "cat.rules.json"
    rules.write_text(json.dumps({"attributes": [
        {"name": "供电方式", "rule": "enum", "subcols": ["供电方式_标准"],
         "params": {"values": ["电池供电", "USB供电"]}},
    ]}, ensure_ascii=False), encoding="utf-8")
    out = apply_vocab_acceptance(str(rules), [
        {"属性": "供电方式", "词": "太阳能充电", "结论": "采纳", "规范化写法": "太阳能供电"},
        {"属性": "供电方式", "词": "USB供电", "结论": "采纳", "规范化写法": "USB供电"},
        {"属性": "供电方式", "词": "无线", "结论": "驳回", "规范化写法": ""},
    ])
    assert [a["词"] for a in out["已新增"]] == ["太阳能供电"]
    assert [a["词"] for a in out["跳过重复"]] == ["USB供电"]
    assert [a["词"] for a in out["驳回"]] == ["无线"]
    rules2 = json.loads(rules.read_text(encoding="utf-8"))
    assert rules2["attributes"][0]["params"]["values"] == ["电池供电", "USB供电", "太阳能供电"]


def test_resolve_rules_flat_location(ws_tmp):
    """_resolve_rules 找到平铺的 data/<CAT>/<snake>.rules.json。"""
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    (Path(ws_tmp) / "t" / "cat_x.rules.json").write_text(
        json.dumps({"attributes": []}, ensure_ascii=False), encoding="utf-8")
    d, catid = st._resolve_rules(ctx)
    assert catid == "cat_x"


def test_ensure_rules_autocreates_skeleton_when_missing(ws_tmp):
    """_ensure_rules: 目录下无 rules.json → 按已确认属性集自动建骨架再定位。"""
    ctx = _ctx(ws_tmp)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    st._cat = "t"
    st._spec_attributes = [{"name": "材质", "hint": "不锈钢/陶瓷"}]

    d, catid = st._ensure_rules(ctx)

    assert catid == "t"
    rules_path = Path(ws_tmp) / "t" / "t.rules.json"
    assert rules_path.exists()
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    assert [a["name"] for a in rules["attributes"]] == ["材质"]
    assert rules["attributes"][0]["rule"] == "enum"


# --------------------------- 视觉补充: 并发 + 断点续跑 ---------------------------

def _vision_cells(gaps: dict):
    """构造 find_gaps 需要的 cells: 值格(attr.<属性>)全为「未明确」→ 待补格。"""
    cells = {}
    for xl, attrs in gaps.items():
        for a in attrs:
            cells["%s|attr.%s" % (xl, a)] = {
                "kind": "value", "agree": True, "mode": "未明确",
                "values": [{"value": "未明确", "by": ["a1", "a2"]}],
            }
    return cells


def _vision_ctx(tmp_dir):
    ctx = _ctx(tmp_dir, {"ATTR_STAB_WORKERS": 3})
    return ctx


class _VisionRes:
    def __init__(self, cells):
        self.cells = cells


def _vision_step(tmp_dir):
    ctx = _vision_ctx(tmp_dir)
    st = Step17AttrAnnotation()
    st.bind_cat(ctx)
    st._cat = "t"
    st._spec_attributes = [{"name": "材质"}, {"name": "颜色"}]
    return ctx, st


def test_vision_gapfill_concurrent_and_fallback(ws_tmp):
    """视觉补充: 并发补全待补格; 单行失败 → 逐行重试兜底; 失败行跳过不炸整步。"""
    ctx, st = _vision_step(ws_tmp)
    rows = [
        {"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"},
        {"xl_row": 3, "标题": "r3", "图片链接": "https://x/b.jpg"},
        {"xl_row": 4, "标题": "r4", "图片链接": "https://x/c.jpg"},
    ]
    calls = {"n": 0}
    fail3 = {"remaining": 1}   # 行3 第一次调用失败 → 触发逐行重试兜底

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            assert kw.get("detail") == "low"
            calls["n"] += 1
            m = re.search(r'"xl_row":\s*(\d+)', prompt)
            xl = int(m.group(1)) if m else 0
            if xl == 3 and fail3["remaining"] > 0:
                fail3["remaining"] -= 1
                raise RuntimeError("vision 超时(模拟)")
            return {"追问": {"材质": "不锈钢", "颜色": "黑色"},
                    "来源": {"材质": "图片视觉", "颜色": "图片视觉"}}

    ctx._llm = _VisionLLM()
    res = _VisionRes(_vision_cells({2: ["材质", "颜色"], 3: ["材质"], 4: ["颜色"]}))

    filled = st._vision_gapfill(ctx, res, rows)

    # 行2/3/4 都有产出; 行3 第一次失败后重试成功
    assert calls["n"] >= 4                      # 3 行 + 至少 1 次重试
    assert (2, "材质") in filled
    assert (3, "材质") in filled
    assert (4, "颜色") in filled
    # 检查点已清(全部完成)
    ckpt = Path(ws_tmp) / "t" / "17_属性标注" / "视觉补充.checkpoint.json"
    assert not ckpt.exists()


def test_vision_gapfill_resumes_from_checkpoint(ws_tmp):
    """视觉补充断点续跑: 检查点已补的行读回, 只跑剩余行。"""
    ctx, st = _vision_step(ws_tmp)
    rows = [
        {"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"},
        {"xl_row": 3, "标题": "r3", "图片链接": "https://x/b.jpg"},
    ]
    # 预置检查点: 行2 已完成
    ckpt_dir = Path(ws_tmp) / "t" / "17_属性标注"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    from tools.attr_stability.gapfill import build_vision_prompt, find_gaps
    res = _VisionRes(_vision_cells({2: ["材质"], 3: ["颜色"]}))
    plan = find_gaps(res.cells, st._spec_attr_names())
    key = st._vision_input_key(
        plan, "图片链接", rows=rows,
        context={**st._annotation_cache_context(ctx), "detail": "low", "notes": {}, "values": {},
                 "prompts": {str(item["xl_row"]): build_vision_prompt(
                     next(r for r in rows if r["xl_row"] == item["xl_row"]),
                     item["待补属性"], n_images=1, notes={}, values_by_attr={})
                     for item in plan.items}})
    high_context = {**st._annotation_cache_context(ctx), "detail": "high", "notes": {}, "values": {},
                    "prompts": {str(item["xl_row"]): build_vision_prompt(
                        next(r for r in rows if r["xl_row"] == item["xl_row"]),
                        item["待补属性"], n_images=1, notes={}, values_by_attr={})
                        for item in plan.items}}
    assert key != st._vision_input_key(plan, "图片链接", rows=rows, context=high_context)
    ckpt = ckpt_dir / "视觉补充.checkpoint.json"
    ckpt.write_text(json.dumps({
        "input_key": key,
        "done": [{"xl_row": 2, "追问": {"材质": "不锈钢"}, "来源": {"材质": "图片视觉"}}],
    }, ensure_ascii=False), encoding="utf-8")

    calls = {"n": 0}

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            calls["n"] += 1
            return {"追问": {"颜色": "黑色"}, "来源": {"颜色": "图片视觉"}}

    ctx._llm = _VisionLLM()
    res = _VisionRes(_vision_cells({2: ["材质"], 3: ["颜色"]}))

    filled = st._vision_gapfill(ctx, res, rows)

    assert calls["n"] == 1                      # 只补跑剩余的行3, 行2 从检查点读回
    assert (2, "材质") in filled                 # 检查点读回的值
    assert (3, "颜色") in filled                 # 新补的值
    assert not ckpt.exists()                    # 完成后清检查点


def test_vision_gapfill_unwraps_envelope_and_rejects_foreign_keys(ws_tmp):
    """§20.6 回归: 模型回 `{"type":"json_object","response":{…}}` 时

    ① 要剥壳救回真正的答案;
    ② **绝不**把 `type` 这类表外属性名写进候选池(实测 1.0 档会出现 `type=json_object`)。
    """
    ctx, st = _vision_step(ws_tmp)
    rows = [{"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"}]

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            # 值被包在 response 里, 同时夹带一个规格书里不存在的 `type`
            return {"type": "json_object",
                    "response": {"追问": {"材质": "不锈钢", "type": "json_object"},
                                 "来源": {"材质": "图片视觉", "type": "图片视觉"}}}

    ctx._llm = _VisionLLM()
    res = _VisionRes(_vision_cells({2: ["材质"]}))

    filled = st._vision_gapfill(ctx, res, rows)

    assert (2, "材质") in filled, "被包在 response 里的答案必须救回"
    assert filled[(2, "材质")] == "不锈钢"
    assert all(attr in ("材质", "颜色") for _r, attr in filled), \
        "表外属性名不得进入候选池: %r" % (list(filled),)
    assert not [k for k in filled if k[1] == "type"]


def test_vision_gapfill_drops_lost_content_envelope(ws_tmp):
    """§20.6 回归: `{"type":"json_object"}`(内容已丢) 不得留下 `type=json_object` 脏格。

    这一格进不了交付表(列名取自规格书), 所以症状很隐蔽 —— 表现为"白跑一次"。
    正确行为: 当作该行没补出, 交给逐行重试兜底。
    """
    ctx, st = _vision_step(ws_tmp)
    rows = [{"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"}]
    calls = {"n": 0}

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            calls["n"] += 1
            return {"type": "json_object"}          # 内容已丢

    ctx._llm = _VisionLLM()
    res = _VisionRes(_vision_cells({2: ["材质"]}))

    filled = st._vision_gapfill(ctx, res, rows)

    assert filled == {}, "不得把信封键当属性: %r" % (filled,)
    assert calls["n"] == 4, "内容丢失应初次请求后最多再试三轮"


@pytest.mark.parametrize("bad", [{}, {"追问": {}}, {"追问": {"材质": ""}},
                                  {"type": "json_object"}])
def test_vision_empty_response_retries_and_can_recover(ws_tmp, bad):
    ctx, st = _vision_step(ws_tmp)
    rows = [{"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"}]
    calls = {"n": 0}

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return bad
            return {"追问": {"材质": "不锈钢"}, "来源": {"材质": "图片视觉"}}

    ctx._llm = _VisionLLM()
    filled = st._vision_gapfill(ctx, _VisionRes(_vision_cells({2: ["材质"]})), rows)
    assert calls["n"] == 2
    assert filled[(2, "材质")] == "不锈钢"


def test_vision_explicit_unknown_is_not_retried(ws_tmp):
    ctx, st = _vision_step(ws_tmp)
    rows = [{"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"}]
    calls = {"n": 0}

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            calls["n"] += 1
            return {"追问": {"材质": "未明确"}, "来源": {"材质": "图片视觉"}}

    ctx._llm = _VisionLLM()
    filled = st._vision_gapfill(ctx, _VisionRes(_vision_cells({2: ["材质"]})), rows)
    assert filled == {}
    assert calls["n"] == 1


def test_vision_gapfill_parses_string_payload_in_content(ws_tmp):
    """§20.6 回归: `content` 是 JSON 字符串时也要能解析出来。"""
    ctx, st = _vision_step(ws_tmp)
    rows = [{"xl_row": 2, "标题": "r2", "图片链接": "https://x/a.jpg"}]

    class _VisionLLM:
        def vision(self, prompt, urls, **kw):
            return {"type": "json_object",
                    "content": json.dumps(
                        {"追问": {"颜色": "黑色"}, "来源": {"颜色": "图片视觉"}},
                        ensure_ascii=False)}

    ctx._llm = _VisionLLM()
    res = _VisionRes(_vision_cells({2: ["颜色"]}))

    filled = st._vision_gapfill(ctx, res, rows)

    assert filled.get((2, "颜色")) == "黑色"

