"""Conservative conversion of structured cm values while preserving their layout."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_N = r"\d+(?:\.\d+)?"
_SEP = r"[-–—~～至]"
_UNIT = r"(?:英寸|inches|inch|in|''|\"|″)"
_PURE = re.compile(rf"^\s*(?:{_N}|缺)(?:\s*[xX×，,、]\s*(?:{_N}|缺))+\s*cm\s*$", re.I)
_PART = re.compile(rf"^(?P<label>(?:[^\d（）()]*|\d+(?:\.\d+)?\s*oz\s*[:：])?)(?P<a>{_N})(?:(?P<sep>\s*{_SEP}\s*)(?P<b>{_N}))?(?P<space>\s*)cm(?P<suffix>\s*(?:[（(]\s*\d+\s*档(?:可调)?\s*[）)])?\s*)$", re.I)
_PAIR = re.compile(rf"^(?P<label>[^\d（）()]*)(?P<a>{_N})(?P<u1>\s*{_UNIT})?(?:(?P<sep>\s*{_SEP}\s*)(?P<b>{_N})(?P<u2>\s*{_UNIT}))?(?P<open>\s*[（(]\s*(?:约|約|大约|≈)?\s*)(?P<ca>{_N})(?:(?P<csep>\s*{_SEP}\s*)(?P<cb>{_N}))?(?P<space>\s*)cm(?P<close>\s*[）)]\s*)$", re.I)
_STEP = re.compile(rf"^(?P<label>可调\s*[，,]\s*最高)(?P<max>{_N})(?P<s1>\s*)cm(?P<open>\s*[（(])(?P<count>\d+)(?P<middle>\s*档\s*[，,]\s*每档)(?P<step>{_N})(?P<s2>\s*)cm(?P<close>\s*[）)])$", re.I)
_CM_FIRST = re.compile(rf"^(?P<label>[^\d（）()]*)(?P<cm>{_N})(?P<space>\s*)cm(?P<open>\s*[（(][^\d（）()]*)(?P<inch>{_N})(?P<unit>\s*{_UNIT})(?P<close>\s*[）)]\s*)$", re.I)
_IN_PURE = re.compile(rf"^\s*(?:{_N}|缺)(?:\s*[xX×，,、/]\s*(?:{_N}|缺))+\s*{_UNIT}\s*$", re.I)
_IN_PART = re.compile(rf"^(?P<label>(?:[^\d（）()]*|\d+(?:\.\d+)?\s*oz\s*[:：])?)(?P<a>{_N})(?:(?P<u1>\s*{_UNIT})?(?P<sep>\s*{_SEP}\s*)(?P<b>{_N}))?(?P<space>\s*)(?:{_UNIT})(?P<suffix>\s*(?:[（(](?:[^\d（）()]*|\d+\s*档(?:可调)?)[）)])?\s*(?:可调|等)?\s*)$", re.I)
_IN_NESTED = re.compile(rf"^(?P<label>[^\d（）()]*)(?P<main>{_N})(?P<space>\s*){_UNIT}(?P<open>\s*[（(][^\d（）()]*)(?P<a>{_N})(?P<sep>\s*{_SEP}\s*)(?P<b>{_N})(?P<inner_space>\s*){_UNIT}(?P<close>\s*[）)]\s*)$", re.I)
_IN_CHAIN = re.compile(rf"^\s*{_N}\s*{_UNIT}(?:\s*-\s*{_N}\s*{_UNIT}){{2,}}\s*$", re.I)


def convert_composite_cm_inches(raw, places=2):
    """Return None on ambiguity or conflicting existing inch/cm values."""
    text = str(raw)
    if len(text) > 500 or not 0 <= places <= 6:
        return None
    quantum = Decimal(1).scaleb(-places)
    def fmt(n):
        return f'{Decimal(n).quantize(quantum, rounding=ROUND_HALF_UP):.{places}f}'
    def cm(n):
        return fmt(Decimal(n) / Decimal('2.54'))
    def clean_label(label):
        return re.sub(r'^\s*(?:固定|大约|約|约)\s*', '', label)
    try:
        step = _STEP.fullmatch(text)
        if step:
            return (step.group('label') + cm(step.group('max')) + step.group('s1') + 'inch'
                    + step.group('open') + step.group('count') + step.group('middle')
                    + cm(step.group('step')) + step.group('s2') + 'inch' + step.group('close'))
        nested = _IN_NESTED.fullmatch(text)
        if nested:
            if Decimal(nested.group('a')) > Decimal(nested.group('b')):
                return None
            return (clean_label(nested.group('label')) + fmt(nested.group('main')) + nested.group('space') + 'inch'
                    + nested.group('open') + fmt(nested.group('a')) + nested.group('sep') + fmt(nested.group('b'))
                    + nested.group('inner_space') + 'inch' + nested.group('close'))
        if _IN_CHAIN.fullmatch(text):
            result = re.sub(_N, lambda m: fmt(m.group()), text)
            return re.sub(_UNIT, 'inch', result, flags=re.I)
        pair = _PAIR.fullmatch(text)
        if pair and (pair.group('u1') or pair.group('u2')):
            a, b, ca, cb = (pair.group(k) for k in ('a', 'b', 'ca', 'cb'))
            if bool(b) != bool(cb) or (b and (Decimal(a) > Decimal(b) or Decimal(ca) > Decimal(cb))):
                return None
            if abs(Decimal(a) - Decimal(ca) / Decimal('2.54')) > Decimal('0.06'):
                return None
            if b and abs(Decimal(b) - Decimal(cb) / Decimal('2.54')) > Decimal('0.06'):
                return None
            # Existing inch measurements are authoritative; retain both clauses.
            original = clean_label(pair.group('label')) + fmt(a) + (pair.group('u1') or '')
            inside = fmt(a)
            if b:
                original += pair.group('sep') + fmt(b) + pair.group('u2')
                inside += pair.group('csep') + fmt(b)
            opening = re.sub(r'(?:大约|約|约|≈)\s*', '', pair.group('open'))
            return original + opening + inside + pair.group('space') + 'inch' + pair.group('close')
        if _PURE.fullmatch(text):
            if not re.search(_N, text):
                return None
            result = re.sub(_N, lambda m: cm(m.group()), text)
            return re.sub(r'cm(?=\s*$)', 'inch', result, flags=re.I)
        if _IN_PURE.fullmatch(text):
            if not re.search(_N, text):
                return None
            result = re.sub(_N, lambda m: fmt(m.group()), text)
            return re.sub(rf'{_UNIT}(?=\s*$)', 'inch', result, flags=re.I)
        parts = re.split(r'([、，,/]|或)', text)
        output = []
        for index, part in enumerate(parts):
            if index % 2:
                output.append(part)
                continue
            paired = _CM_FIRST.fullmatch(part)
            if paired:
                if abs(Decimal(paired.group('inch')) - Decimal(paired.group('cm')) / Decimal('2.54')) > Decimal('0.06'):
                    return None
                n = fmt(paired.group('inch'))
                output.append(clean_label(paired.group('label')) + n + paired.group('space') + 'inch'
                              + paired.group('open') + n + paired.group('unit') + paired.group('close'))
                continue
            existing = _IN_PART.fullmatch(part)
            if existing:
                if existing.group('b') and Decimal(existing.group('a')) > Decimal(existing.group('b')):
                    return None
                val = fmt(existing.group('a'))
                if existing.group('b'):
                    val += (('inch' if existing.group('u1') else '') + existing.group('sep') + fmt(existing.group('b')))
                output.append(clean_label(existing.group('label')) + val + existing.group('space') + 'inch' + existing.group('suffix'))
                continue
            m = _PART.fullmatch(part)
            if not m or (m.group('b') and Decimal(m.group('a')) > Decimal(m.group('b'))):
                return None
            val = cm(m.group('a'))
            if m.group('b'):
                val += m.group('sep') + cm(m.group('b'))
            output.append(clean_label(m.group('label')) + val + m.group('space') + 'inch' + m.group('suffix'))
        return ''.join(output)
    except (InvalidOperation, ZeroDivisionError):
        return None
