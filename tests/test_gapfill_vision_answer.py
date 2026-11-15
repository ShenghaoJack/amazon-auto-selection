# 单元测试: 视觉返回的剥壳 + 待补属性白名单 (2026-10-08, 改动清单 §20.6)
# ---------------------------------------------------------------------------
# 背景: `_vision_gapfill._fill_one` 原先只写 `out.get("追问", out)`。
# 模型回一层包装 `{"type":"json_object", …}` 时拿不到 `追问` → 把**整个信封**
# 当成属性表 → 记下 `type = "json_object"`, 而该行真正的答案被丢掉。
# 实测 Dog Dryer 在 temperature=1.0 下三次跑各出现 3 / 1 / 1 格这种脏数据, 0.2 档为 0。
#
# 本文件锁定 `parse_vision_answer` 的两道防线:
#   ① 剥壳  → 救回被包住的答案(含 content 是 JSON 字符串的情形)
#   ② 白名单 → 表外属性名(含模型编造的)永远进不来
import json

import pytest

from tools.attr_stability.gapfill import parse_vision_answer


WANT = ["材质", "颜色"]


def test_canonical_shape():
    rec = {"xl_row": 2,
           "追问": {"材质": "不锈钢", "颜色": "黑色"},
           "来源": {"材质": "图片视觉", "颜色": "图片视觉"}}
    vals, srcs = parse_vision_answer(rec, WANT)
    assert vals == {"材质": "不锈钢", "颜色": "黑色"}
    assert srcs == {"材质": "图片视觉", "颜色": "图片视觉"}


def test_unwraps_response_envelope():
    """① 救回被 `response` 包住的答案。"""
    rec = {"type": "json_object",
           "response": {"追问": {"材质": "不锈钢"}, "来源": {"材质": "图片视觉"}}}
    vals, srcs = parse_vision_answer(rec, WANT)
    assert vals == {"材质": "不锈钢"}
    assert srcs == {"材质": "图片视觉"}


def test_unwraps_content_json_string():
    """① `content` 是 JSON 字符串时也要解析出来。"""
    rec = {"type": "json_object",
           "content": json.dumps({"追问": {"颜色": "黑色"},
                                  "来源": {"颜色": "图片OCR"}}, ensure_ascii=False)}
    vals, srcs = parse_vision_answer(rec, WANT)
    assert vals == {"颜色": "黑色"}
    assert srcs == {"颜色": "图片OCR"}


def test_unwraps_nested_double_envelope():
    rec = {"data": {"response": {"追问": {"材质": "陶瓷"}, "来源": {}}}}
    vals, _ = parse_vision_answer(rec, WANT)
    assert vals == {"材质": "陶瓷"}


def test_lost_content_envelope_returns_empty():
    """关键回归: `{"type":"json_object"}` 内容已丢 → 必须返回空, 不能留下 `type` 脏格。"""
    vals, srcs = parse_vision_answer({"type": "json_object"}, WANT)
    assert vals == {}
    assert srcs == {}


def test_foreign_keys_are_filtered():
    """② 表外属性名(规格书里没有的)一律丢弃 —— 值层与来源层都要过滤。"""
    rec = {"追问": {"材质": "不锈钢", "type": "json_object", "电机类型": "无刷"},
           "来源": {"材质": "图片视觉", "type": "图片视觉"}}
    vals, srcs = parse_vision_answer(rec, WANT)
    assert vals == {"材质": "不锈钢"}
    assert srcs == {"材质": "图片视觉"}


def test_xl_row_never_leaks_into_values():
    """平铺形状下 `xl_row` 是元数据, 不是属性。"""
    vals, _ = parse_vision_answer({"xl_row": 7, "材质": "不锈钢"}, WANT)
    assert vals == {"材质": "不锈钢"}


def test_flattened_shape_without_wrapper():
    """模型直接把属性平铺出来(没有 `追问` 这一层)时也要能用。"""
    vals, _ = parse_vision_answer({"材质": "不锈钢", "颜色": "黑色"}, WANT)
    assert vals == {"材质": "不锈钢", "颜色": "黑色"}


def test_attr_name_whitespace_is_normalized():
    vals, srcs = parse_vision_answer(
        {"追问": {" 材质 ": "不锈钢"}, "来源": {" 材质 ": "图片视觉"}}, WANT)
    assert vals == {"材质": "不锈钢"}
    assert srcs == {"材质": "图片视觉"}


def test_only_requested_attrs_survive():
    """只问 `材质` 时, 同批返回的 `颜色` 不该被采纳。"""
    vals, _ = parse_vision_answer(
        {"追问": {"材质": "不锈钢", "颜色": "黑色"}, "来源": {}}, ["材质"])
    assert vals == {"材质": "不锈钢"}


@pytest.mark.parametrize("bad", [None, "字符串", 123, ["列表"], ("元组",)])
def test_non_mapping_returns_empty(bad):
    assert parse_vision_answer(bad, WANT) == ({}, {})


def test_empty_want_returns_empty():
    assert parse_vision_answer({"追问": {"材质": "不锈钢"}}, []) == ({}, {})


def test_blank_want_entries_ignored():
    assert parse_vision_answer({"追问": {"材质": "不锈钢"}}, ["", "  "]) == ({}, {})


def test_extra_metadata_keys_ignored():
    """`问法`/`依据` 之类模型自加的顶层键不得混进值里。"""
    rec = {"xl_row": 2, "问法": "看图",
           "追问": {"材质": "不锈钢"}, "来源": {"材质": "图片视觉"}}
    vals, _ = parse_vision_answer(rec, WANT)
    assert vals == {"材质": "不锈钢"}
