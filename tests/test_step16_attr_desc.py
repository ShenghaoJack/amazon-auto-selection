# 单元测试: FR-16 属性集提取(中文属性名/hint 详细度) + 属性含义补齐
#
# 覆盖 _extract_attrs(提取/中文名/数量上限 8-25/下限告警/旧英文 key 兼容)
# 与 _describe_attrs(为已有属性名补中文列名与详细 hint)。
import os
import re
from unittest import mock

import pandas as pd

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step16_attribute_annotation import (
    ATTR_ROUND_MAX,
    ATTR_ROUND_MIN,
    Step16AttributeAnnotation,
)


class _FakeLLM:
    """按调用顺序返回 payload; fail=True 时抛异常(模拟 LLM 不可用)。"""

    def __init__(self, payloads=None, fail: bool = False):
        self.payloads = list(payloads or [])
        self.fail = fail
        self.prompts: list[str] = []

    def chat_json(self, prompt, **kw):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("llm down")
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


def test_extract_attrs_accepts_legacy_english_key_reply(ws_tmp):
    """兼容: 模型只回旧 `key`(没给 name)时, 英文 key 仍被接受(老检查点/老模型不炸)。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM([{"attributes": [{"key": "product_type", "hint": "砧板 / 砧板套装"},
                                         {"key": "Grind Levels", "hint": "数字档"}]}])
    attrs, desc = st._extract_attrs(ctx, _df())
    assert attrs == ["product_type", "grind_levels"]         # 规范化(小写+下划线)
    assert desc["grind_levels"] == "Grind Levels(数字档)"


def test_describe_attrs_normalizes_and_drops_unknown(ws_tmp):
    """补齐调用的解析: key 对齐(可含空格/大小写) / name 作中文列名 / 入参之外丢弃。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    ctx._llm = _FakeLLM(
        [
            {
                "attributes": [
                    {"key": "Grind Levels", "name": "研磨档位", "hint": "数字"},
                    {"key": "burr_material", "name": "", "hint": "陶瓷/不锈钢"},
                    "不是字典",
                ]
            }
        ]
    )
    desc = st._describe_attrs(ctx, ["grind_levels", "burr_material"])
    # name 给中文名 → 作列名; 没给 name 时用属性名本身兜底(不再整条丢弃, 否则列名会退化成英文)
    assert desc == {"grind_levels": "研磨档位(数字)", "burr_material": "burr_material(陶瓷/不锈钢)"}


# --------------------------- 属性集仍按 8-25 + 含义写详细(2026-09-17 用户要求, 数量已改回 8-25)
def _payload(n: int) -> dict:
    # 2026-09-17 新口径: 只给中文属性名(name) + hint
    return {"attributes": [{"name": f"属性{i:02d}", "hint": f"典型取值 a{i} / b{i} / c{i}"}
                           for i in range(n)]}


def test_extract_attrs_caps_at_25(ws_tmp):
    """数量上限 = ATTR_ROUND_MAX: 20 个全收; 40 个候选截到 ATTR_ROUND_MAX(数量口径不变)。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st.bind_cat(ctx)

    n_ok = ATTR_ROUND_MAX - 2                     # 未到上限 → 一个不丢
    ctx._llm = _FakeLLM([_payload(n_ok)])
    attrs, desc = st._extract_attrs(ctx, _df())
    assert len(attrs) == n_ok
    assert attrs[0] == "属性00" and attrs[-1] == "属性%02d" % (n_ok - 1) and len(desc) == n_ok

    ctx._llm = _FakeLLM([_payload(ATTR_ROUND_MAX + 12)])   # 超过上限 → 截到 ATTR_ROUND_MAX
    attrs40, _ = st._extract_attrs(ctx, _df())
    assert len(attrs40) == ATTR_ROUND_MAX, "超过单轮上限只保留前 ATTR_ROUND_MAX 个"
    assert ATTR_ROUND_MIN == 8 and ATTR_ROUND_MAX >= ATTR_ROUND_MIN   # 单轮数量区间


def test_extract_prompt_requires_chinese_names(ws_tmp):
    """提示词必须要求**属性名用中文**(2026-09-17 用户要求), 且不得再要求英文 key。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM([_payload(10)])
    st._extract_attrs(ctx, _df())
    prompt = ctx._llm.prompts[0]
    assert "一律用中文表示" in prompt
    assert "中文属性名" in prompt
    assert "英文小写下划线" not in prompt, "属性名不应再要求英文 key"
    assert '"attributes": [{"name"' in prompt, "输出结构应为 name/hint(无 key)"


def test_extract_attrs_warns_when_fewer_than_8(ws_tmp):
    """少于下限(8)不失败, 但要留下可读告警(便于看出这次提取偏少)。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM([_payload(3)])
    logs: list[str] = []
    with mock.patch.object(st, "log", side_effect=lambda c, text: logs.append(text)):
        attrs, _ = st._extract_attrs(ctx, _df())
    assert len(attrs) == 3
    assert any("少于要求的 8 个" in t for t in logs), logs


def test_extract_prompt_requires_8_to_25_and_detailed_hint(ws_tmp):
    """提示词必须写明 8-25 个(用户要求改回), 且要求 hint 逐项覆盖判定依据/典型取值/易混区分/未明确。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM([_payload(10)])
    st._extract_attrs(ctx, _df())

    prompt = ctx._llm.prompts[0]
    assert ("%d-%d 个" % (ATTR_ROUND_MIN, ATTR_ROUND_MAX)) in prompt, "提示词数量区间必须与 ATTR_ROUND_MIN/ATTR_ROUND_MAX 一致"
    assert "25-50" not in prompt, "已改回 8-25, 不能再出现 25-50"
    for kw in ("判定依据", "典型取值", "易混属性区分", "未明确"):
        assert kw in prompt, f"hint 详细度要求缺少: {kw}"
    assert "不要只写一两个词" in prompt


def test_describe_attrs_prompt_also_requires_detailed_hint(ws_tmp):
    """补齐含义的那次调用也要按同样的详细度要求(否则老检查点补出来仍是一两个词)。"""
    ctx = _ctx(ws_tmp)
    st = _step(ctx)
    st.bind_cat(ctx)
    ctx._llm = _FakeLLM([{"attributes": [{"key": "grind_levels", "name": "研磨档位",
                                          "hint": "标题找 manual/electric; 取值 手动 / 电动 / 手动电动两用; "
                                                  "与 电源方式 区分; 看不出写 未明确"}]}])
    desc = st._describe_attrs(ctx, ["grind_levels"])
    prompt = ctx._llm.prompts[0]
    assert "30-120 字" in prompt and "典型取值" in prompt and "形态：" in prompt
    assert desc["grind_levels"].startswith("研磨档位(")
    # 列名只取括号前的中文名 → 详细 hint 不会撑大表头
    assert re.split(r"[（(]", desc["grind_levels"], maxsplit=1)[0].strip() == "研磨档位"
