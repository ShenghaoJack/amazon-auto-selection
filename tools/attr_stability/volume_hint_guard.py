"""Detect unsafe mass/volume rules in generated attribute descriptions."""
from __future__ import annotations

import copy
import re

_VOLUME = re.compile(r'(?<![A-Za-z])(?:mL|L|liters?|litres?|gallons?|cups?)(?![A-Za-z])|毫升|升|容积|体积', re.I)
_BARE_OZ_EQ = re.compile(r'(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:oz|ounces?|盎司)\s*(?:≈|=|＝|约等于|等于)\s*\d+(?:\.\d+)?\s*(?:ml|l|毫升|升)(?![A-Za-z])', re.I)
_UNQUALIFIED_OZ = re.compile(r'(?:原文|遇到|出现|单位为|写成)\s*(?:oz|ounces?|盎司)\s*(?:时)?\s*(?:按|直接按|直接|一律|统一按)[^；;。\n]{0,100}?(?:换算|转换)', re.I)
_GENERIC_OZ_LIST = re.compile(r'(?<!液量)(?<!流体)(?<!fl )盎司(?=时(?:直接|统一)?(?:换算|转换))', re.I)
_MASS_EQ = re.compile(r'(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:kg|lbs?|公斤|千克|磅|g|克)\s*(?:≈|=|＝|约等于|等于)\s*\d+(?:\.\d+)?\s*(?:ml|l|毫升|升)(?![A-Za-z])', re.I)
_MASS_EXAMPLE = re.compile(r'(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:kg|lbs?|公斤|千克|磅|g|克)(?![A-Za-z])', re.I)
_NO_MASS_CONVERSION = re.compile(r'(?:质量|重量|kg|lbs?|公斤|千克|磅|克)[^。；;\n]{0,70}(?:不得|不能|不可|不应|禁止|不)[^。；;\n]{0,25}(?:换算|转换|转成|换成|等同)[^。；;\n]{0,15}(?:体积|容积|ml|升|L)', re.I)
_VOLUME_TARGET = re.compile(r'(?:统一|换算|转换|单位)[^。；;\n]{0,12}(?:mL|L|毫升|升|体积|容积)(?![A-Za-z])', re.I)
SAFE_VOLUME_RULE = '质量单位kg/lb/g不得直接换算为体积；只有明确为液量盎司(fl oz)才按液量换算，单写oz不可默认是液量；无法确认体积时写未明确，原文保留作依据。'


def inspect_volume_hint(name, hint):
    """Flag suspect rules only when the definition actually refers to volume."""
    hint = str(hint or '')
    if not hint or not _VOLUME.search(hint):
        return []
    risks = []
    if _BARE_OZ_EQ.search(hint) or _UNQUALIFIED_OZ.search(hint) or _GENERIC_OZ_LIST.search(hint):
        risks.append('未限定的盎司可能被直接当作液量盎司换算')
    if _MASS_EQ.search(hint):
        risks.append('质量与体积出现直接等价换算')
    elif (('容量' in str(name) or '容积' in str(name) or '体积' in str(name) or _VOLUME_TARGET.search(hint))
          and _MASS_EXAMPLE.search(hint) and not _NO_MASS_CONVERSION.search(hint)):
        risks.append('体积定义把质量数值列为依据但未说明不能直接换算')
    return risks


def prepare_volume_hints(attrs):
    """Safely adjust machine-generated definitions; preserve manual definitions."""
    out = copy.deepcopy(attrs)
    audit = []
    for attr in out:
        if not isinstance(attr, dict):
            continue
        name = str(attr.get('name') or '').strip()
        original = str(attr.get('hint') or '').strip()
        risks = inspect_volume_hint(name, original)
        if not risks:
            continue
        manual = attr.get('source') == 'manual' or bool(attr.get('modified'))
        corrected = original
        if not manual:
            corrected = _UNQUALIFIED_OZ.sub('仅明确液量盎司时按体积换算', corrected)
            corrected = _GENERIC_OZ_LIST.sub('液量盎司', corrected)
            corrected = _BARE_OZ_EQ.sub('未限定盎司不可直接换算体积', corrected)
            corrected = _MASS_EQ.sub('质量与体积不可直接等同', corrected)
            if SAFE_VOLUME_RULE not in corrected:
                corrected = corrected.rstrip() + '；' + SAFE_VOLUME_RULE
            attr['hint'] = corrected
            # The complete safety boundary must reach annotation even if summary is short.
            attr['use_full_hint'] = True
        audit.append({'属性': name, '原说明': original, '审查后的说明': corrected,
                      '问题': [{'类型': '质量与体积换算风险', '原因': '；'.join(risks)}],
                      '建议': [{'建议名称': '', '建议说明': corrected if not manual else '',
                               '处理建议': '人工修改的说明保持原样，请明确质量、体积和盎司口径。' if manual else '已移除明确错误的换算并补充边界，请人工核对。'}],
                      '建议状态': '人工说明待核' if manual else '已补充换算边界，待人工确认'})
    return out, audit
