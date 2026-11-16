"""Conservative, category-neutral review of attribute definitions; never edits them."""
from __future__ import annotations

import json
import re

_SHAPE = re.compile(r"形态\s*[:：]\s*(是否|单选|多选|数值|尺寸原样)")
_GENERIC = re.compile(r"2\s*[-‐–]?\s*in\s*[-‐–]?\s*1|二合一|多合一|多功能|一体式|组合式", re.I)
_DIRECT_YES = re.compile(r"(?:出现|看到|见到|包含|含有|命中|任一|任意|等词|关键词)[^。；;\n]{0,180}(?:即|就|直接|便)?\s*(?:写|记|标|判定|判断|判为|视为|算作)[^。；;\n]{0,12}(?:是|成立)", re.I)


def definition_findings(attrs):
    findings = []
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        name, hint = str(attr.get('name') or '').strip(), str(attr.get('hint') or '').strip()
        if not name or not hint:
            continue
        shape = _SHAPE.search(hint)
        issues = []
        if name.startswith('是否') and shape and shape.group(1) != '是否':
            issues.append({'类型': '名称与取值形态冲突', '原因': '名称是是否题，但说明要求%s；需要确认保留三态、改为类别属性或拆分。' % shape.group(1)})
        elif name.startswith('是否'):
            examples = re.search(r'典型取值\s*[:：]?\s*([^；;。\n]+)', hint)
            values = [re.split(r'[（(]', v.strip())[0].strip() for v in examples.group(1).split('/')] if examples else []
            if len(values) >= 2 and any(v and v not in ('是', '否', '未明确', '不适用') for v in values):
                issues.append({'类型': '名称与典型取值冲突', '原因': '名称是是否题，但典型取值包含类别；需要确认三态、类别或拆分口径。'})
        if _GENERIC.search(hint) and _DIRECT_YES.search(hint):
            issues.append({'类型': '笼统词可能直接触发肯定', '原因': '说明可能把笼统营销或结构词直接作为肯定依据；应核对具体对象及关系，未说明时保留未明确。'})
        if issues:
            findings.append({'属性': name, '原说明': hint, '问题': issues, '建议': [], '建议状态': '待人工确认'})
    return findings


def review_definitions(attrs, llm=None):
    """Suggestions are untrusted display-only text; failures retain all findings."""
    findings = definition_findings(attrs)
    if not findings or llm is None:
        return findings
    candidates = findings[:30]
    prompt = (
        '你是跨类目属性定义审校员。仅针对列出的现有属性提出供人工确认的建议，不修改属性集。'
        '名称为是否而取值形态为多选、单选、数值时，判断保留三态、改为描述具体维度的属性或拆分；'
        '拆分须保留独立信息和未知状态，不得因为示例相似就合并。'
        '笼统词如多功能、二合一或组合本身不证明具体对象或关系；给出明确正反证据边界，'
        '对象或关系不明时为未明确，不得把未提及当否。不要推断商品事实。'
        '返回JSON：{"reviews":[{"属性":"原名","建议名称":"可为空","建议说明":"可为空",'
        '"处理建议":"供人选择的具体方案及原因"}]}；属性名必须来自输入。\n'
        + json.dumps(candidates, ensure_ascii=False))
    try:
        response = llm.chat_json(prompt, temperature=0, max_tokens=3500)
        rows = response.get('reviews') if isinstance(response, dict) else None
        if not isinstance(rows, list):
            return findings
        by_name = {x['属性']: x for x in candidates}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get('属性') or '').strip()
            if name not in by_name or name in seen:
                continue
            seen.add(name)
            suggestion = {k: str(row.get(k) or '').strip()[:1000] for k in ('建议名称', '建议说明', '处理建议')}
            if any(suggestion.values()):
                by_name[name]['建议'] = [suggestion]
                by_name[name]['建议状态'] = '模型建议，待人工确认'
    except Exception:
        # A failed review must not change or remove any attribute.
        pass
    return findings
