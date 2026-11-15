"""Narrow reference guidance and conditional-boundary validation.

Apply after the classifier has computed its cache key: completed checks remain
reusable, while calls for pending checks receive explicit, per-input ID lists.
Preserve cache keys and the existing validator while allowing a specifically
qualified positive review to be cited as a boundary for its own condition.
"""
from __future__ import annotations

import json
from functools import wraps


def conditional_boundaries(payload):
    """Only positive evidence with explicit, non-empty conditions qualifies."""
    result = set()
    for item in payload.get("evidence", []):
        if not isinstance(item, dict) or item.get("polarity") != "positive":
            continue
        ref, conditions = item.get("evidence_id"), item.get("conditions")
        if (isinstance(ref, str) and ref and isinstance(conditions, list)
                and any(isinstance(condition, str) and condition.strip() for condition in conditions)):
            result.add(ref)
    return result


def validate_conditional_boundaries(payload, response, validate):
    """Validate all original fields; extend only boundary IDs for these inputs."""
    try:
        return validate(response)
    except ValueError as exc:
        if not str(exc).startswith("boundary_ids引用越界/重复"):
            raise
        if not isinstance(response, dict) or not isinstance(response.get("boundary_ids"), list):
            raise
        ids = response["boundary_ids"]
        if not all(isinstance(ref, str) for ref in ids) or len(ids) != len(set(ids)):
            raise
        qualified = conditional_boundaries(payload)
        accepted = qualified.intersection(ids)
        if not accepted:
            raise
        # The original validator checks status, all other references and text,
        # and any remaining (normally uncertain) boundary references. Do not
        # mutate the saved result or present conditional evidence as support.
        adjusted = dict(response, boundary_ids=[ref for ref in ids if ref not in accepted])
        return validate(adjusted)


def solution_request(prompt: str, stage: str) -> str:
    if stage != "已有解法核查":
        return prompt
    header, separator, raw = prompt.rpartition("\n")
    if not separator:
        return prompt
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return prompt
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), list) or not isinstance(payload.get("cells"), list):
        return prompt
    allowed = {"support_ids": [], "counter_ids": [], "boundary_ids": [], "cell_ids": []}
    field_by_polarity = {"positive": "support_ids", "negative": "counter_ids", "uncertain": "boundary_ids"}
    for item in payload["evidence"]:
        if not isinstance(item, dict):
            continue
        field = field_by_polarity.get(item.get("polarity"))
        ref = item.get("evidence_id")
        if field and isinstance(ref, str) and ref not in allowed[field]:
            allowed[field].append(ref)
        if ref in conditional_boundaries({"evidence": [item]}) and ref not in allowed["boundary_ids"]:
            allowed["boundary_ids"].append(ref)
    for cell in payload["cells"]:
        if isinstance(cell, dict) and isinstance(cell.get("cell_id"), str) and cell["cell_id"] not in allowed["cell_ids"]:
            allowed["cell_ids"].append(cell["cell_id"])
    previous = str(payload.get("validation_feedback") or "")
    instructions = (
        "编号校验：每个字段只能选择下列本次输入中对应字段允许的编号，不能跨字段、跨变体、"
        "跨评论或自行编造；每个编号最多出现一次。允许列表为空时必须输出[]。"
        "SC开头的属性单元格编号只能放cell_ids，不能放评论证据字段；"
        "support_ids仅正向、counter_ids仅负向；boundary_ids可引用不确定评论，"
        "也可引用本次正向评论中明确列出的conditions作为条件边界，不可据此编造负面事实。"
        "没有正向支持时不能判部分解决；没有实际验证时不能判已解决。"
        "允许编号：" + json.dumps(allowed, ensure_ascii=False, separators=(",", ":"))
    )
    payload["validation_feedback"] = (previous + "\n" if previous else "") + instructions
    return header + separator + json.dumps(payload, ensure_ascii=False)


def install_solution_retry(engine) -> None:
    """Install a process-local hook without invalidating unrelated phase caches."""
    if getattr(engine, "_voc_solution_retry_installed", False):
        return
    original = engine.call

    def call(prompt, *, stage="其他"):
        return original(solution_request(prompt, stage), stage=stage)

    engine.call = call
    engine._voc_solution_retry_installed = True
    _install_classifier_hook()


def _install_classifier_hook():
    from tools.voc.classification import Classifier, CACHE_VERSION
    from tools.voc.opportunity import digest, save

    original = Classifier.ask
    if getattr(original, "_voc_conditional_boundary_hook", False):
        return

    @wraps(original)
    def ask(classifier, task, prompt, payload, validate):
        engine = classifier.engine
        if task != "已有解法核查" or not getattr(engine, "_voc_solution_retry_installed", False):
            return original(classifier, task, prompt, payload, validate)

        def checked(response):
            return validate_conditional_boundaries(payload, response, validate)

        # A previous failed response can now be valid under the narrower,
        # evidence-preserving extension. Revalidate every field before reuse;
        # never send a paid request just to reproduce an already-valid answer.
        key = digest([CACHE_VERSION, prompt, engine.config.get("LLM_MODEL"),
                      engine.config.get("LLM_BASE_URL"), payload])
        path = engine.directory / (task + "-" + key + ".json")
        failed = engine.directory / (task + "-失败-" + key + ".json")
        if not path.exists() and failed.exists():
            try:
                candidate = json.loads(failed.read_text(encoding="utf-8"))
                checked(candidate)
            except (ValueError, TypeError, KeyError, UnicodeError):
                pass
            else:
                save(path, candidate)
                engine.log(f"[VOC续跑] {task}：原失败结果经条件边界校验通过，已复用，不调用模型")
                return candidate
        return original(classifier, task, prompt, payload, checked)

    ask._voc_conditional_boundary_hook = True
    Classifier.ask = ask
