# 单元测试: LLM JSON 容错解析
import pytest

from core.llm import extract_json
from core.errors import LLMError


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_unquoted_keys_and_trailing_comma():
    assert extract_json('{a: 1, b: [1, 2,],}') == {"a": 1, "b": [1, 2]}


def test_repair_json_keeps_apostrophe_inside_words():
    """回归 2026-09-13: 单引号修复只能替换"定界用"的引号。

    原实现 `re.sub(r"\\'", '"')` 会把字符串值里的撇号也换掉(Amazon's → Amazon"s),
    把"本来能修好"的 JSON 反而改成永久无法解析。
    """
    from core.llm import _repair_json

    # 撇号在单词内 → 保留; 顺带修掉多余逗号
    fixed = _repair_json('{"title": "Amazon\'s Choice grinder",}')
    assert "Amazon's Choice" in fixed
    assert extract_json('{"title": "Amazon\'s Choice grinder",}')["title"] == "Amazon's Choice grinder"
    # 单引号定界(键与外层值) → 仍能被修成合法 JSON
    assert extract_json("{'keep': 'false', 'reason': 'it\\'s fine'}")["keep"] == "false"


def test_extract_json_embedded_text():
    text = "好的, 结果如下: {\"judgments\": [{\"index\": 0, \"keep\": true}]}"
    assert extract_json(text)["judgments"][0]["keep"] is True


def test_extract_json_list():
    assert extract_json('[1, 2, 3]') == [1, 2, 3]


def test_extract_json_invalid():
    with pytest.raises(LLMError):
        extract_json("这不是 JSON")
