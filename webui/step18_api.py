"""HTTP-independent facade and persisted background jobs for the Step18 panel."""
from __future__ import annotations

import threading
from contextlib import contextmanager
import hashlib
import json
import time
from collections import OrderedDict
from itertools import islice
from datetime import datetime, timezone
from pathlib import Path

from core.config import Config, apply_env_overrides
from core.llm import LLMClient
from core.usage import WEB_STEP18_RUN_PREFIX
from tools.attr_stability.step18_workspace import Workspace, token, stamp, read_json
from tools.attr_stability.spec_io import write_json_atomic, spec_lock

_JOB_GUARD = threading.Lock()
_ACTIVE = set()
_JOB_LIST_CACHE = {}
_JOB_LIST_CACHE_LOCK = threading.Lock()
_PREVIEW_CACHE = OrderedDict()
_PREVIEW_CACHE_LOCK = threading.Lock()
_AVAILABILITY_CACHE = OrderedDict()


class _TimedModel:
    """Measure model request time without changing the client's accounting."""
    def __init__(self, client):
        self._client = client
        self._lock = threading.Lock()
        self.calls = 0
        self.elapsed_ms = 0.0

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _invoke(self, method, *args, **kwargs):
        start = time.perf_counter()
        try:
            return getattr(self._client, method)(*args, **kwargs)
        finally:
            with self._lock:
                self.calls += 1
                self.elapsed_ms += (time.perf_counter() - start) * 1000

    def chat_json(self, *args, **kwargs):
        return self._invoke("chat_json", *args, **kwargs)

    def vision(self, *args, **kwargs):
        return self._invoke("vision", *args, **kwargs)


@contextmanager
def reserve_export(workspace):
    """Prevent export and preview/audit jobs for this workspace from overlapping."""
    key = (str(workspace.store), '__export__')
    with _JOB_GUARD:
        if any(item[0] == key[0] for item in _ACTIVE):
            raise ValueError('有标准化任务正在处理，请等待完成或取消后再确认导出')
        _ACTIVE.add(key)
    try:
        yield
    finally:
        with _JOB_GUARD:
            _ACTIVE.discard(key)


class JobCancelled(BaseException):
    pass


def cancel_job(workspace, job_id):
    path = workspace._path("jobs", job_id, "J")
    with spec_lock(workspace.store):
        job = read_json(path)
        if job.get("status") == "applied":
            raise ValueError("该任务已应用，请在操作记录中撤销")
        proposal_id = (job.get("result") or {}).get("id")
        if proposal_id and workspace.pointer.exists():
            current = workspace._current()
            if any((version.get("operation") or {}).get("proposal_id") == proposal_id
                   for version in workspace.chain(current)):
                raise ValueError("该任务已应用，请在操作记录中撤销")
        if job.get("status") in ("failed", "interrupted", "cancelled"):
            raise ValueError("任务已结束，无需取消")
        with _JOB_GUARD:
            alive = (str(workspace.store), job_id) in _ACTIVE
        job.update(status="cancelling" if alive else "cancelled",
                   message="正在取消，等待当前请求结束" if alive else "已取消，未应用数据",
                   updated_at=stamp())
        write_json_atomic(path, job)
    return {"ok": True, "status": job["status"]}


def retry_job(workspace, job_id, expected, config_values):
    job = job_status(workspace, job_id, include_current_rows=False)
    if job.get("status") in ("pending", "running", "cancelling"):
        raise ValueError("任务仍在运行，请先等待结束")
    if job.get("job_type") == "audit":
        return start_audit(workspace, {"expected_version": expected}, config_values)
    request = job.get("request")
    if not isinstance(request, dict):
        raise ValueError("旧任务未保存完整执行参数，请按原要求重新提交")
    resume = {"_resume_from": job_id} if expected == job.get("base_version") else {}
    return start_preview(workspace, {**request, "expected_version": expected, **resume}, config_values)


class _TextBatchCheckpoint:
    """逐批落盘；只读相同基准版本的重试链，哈希匹配后复用。"""
    def __init__(self, workspace, job_id, parent=None):
        self.workspace = workspace
        self.job_id = job_id
        self.parents = []
        seen = {job_id}
        while parent and parent not in seen:
            seen.add(parent)
            try:
                record = read_json(workspace._path("jobs", parent, "J"))
            except (OSError, ValueError):
                break
            self.parents.append(parent)
            parent = record.get("checkpoint_parent")

    def key(self, prompt, scope, state, llm):
        config = getattr(llm, "config", None)
        model = str(config.get("LLM_MODEL", "")) if config is not None else type(llm).__name__
        endpoint = str(config.get("LLM_BASE_URL", "")) if config is not None else ""
        thinking = config.get("LLM_NO_THINK", True) if config is not None else None
        extra_body = config.get("LLM_EXTRA_BODY", {}) if config is not None else {}
        from tools.attr_stability import step18_model
        code_hash = hashlib.sha256(Path(step18_model.__file__).read_bytes()).hexdigest()
        payload = {"version": 1, "base": state.get("version"), "prompt": prompt,
                   "scope": {k: sorted(v) for k, v in (scope or {}).items()},
                   "model": model, "endpoint": endpoint, "thinking": thinking,
                   "extra_body": extra_body, "code": code_hash}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _path(self, job, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("无效的批次指纹")
        self.workspace._path("jobs", job, "J")  # 校验任务编号
        return self.workspace.store / "checkpoints" / job / (key + ".json")

    def load(self, key):
        for job in [self.job_id, *self.parents]:
            try:
                obj = read_json(self._path(job, key))
                if obj.get("key") != key or not isinstance(obj.get("cells"), list) or not isinstance(obj.get("warnings"), list):
                    continue
                cells = {(c["row_id"], c["attribute"]): c["cell"] for c in obj["cells"]}
                if len(cells) != len(obj["cells"]):
                    continue
                return cells, obj["warnings"]
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return None

    def save(self, key, cells, warnings):
        path = self._path(self.job_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"key": key, "cells": [{"row_id": r, "attribute": a, "cell": c}
                                                    for (r, a), c in cells.items()], "warnings": warnings})


def _job_progress(workspace, path, job, message):
    with spec_lock(workspace.store):
        current = read_json(path)
        if current.get("status") in ("cancelling", "cancelled"):
            raise JobCancelled()
        job.update(status="running", message=message, updated_at=stamp())
        write_json_atomic(path, job)


def _job_finish(workspace, path, job):
    with spec_lock(workspace.store):
        current = read_json(path)
        if current.get("status") in ("cancelling", "cancelled"):
            job.update(status="cancelled", message="已取消，未应用数据", result=None)
        job["updated_at"] = stamp()
        write_json_atomic(path, job)
        _save_job_summary(workspace, path, job)


def new_client(config_values):
    # Never persist the config or credentials in jobs/version files.
    config = Config(apply_env_overrides(dict(config_values)))
    if config.dry_run:
        raise ValueError("当前配置为离线试运行；大模型复核需要在配置中关闭DRY_RUN，同义值直接归并仍可使用")
    return LLMClient(config)


def _metered_client(workspace, config_values, job_id, client_factory):
    """每个面板任务独立账本，避免并发写入/覆盖流水线的账本和 CSV。"""
    client = client_factory(config_values)
    if not isinstance(client, LLMClient):
        return client, None  # 注入的测试替身或非 LLMClient 客户端
    from core.usage import build_meter
    from core.paths import STEP_DIR_NAMES
    from steps.ids import MONITOR_STEP
    meter = build_meter(client.config, run_id=WEB_STEP18_RUN_PREFIX + job_id)
    if bool(client.config.get("LLM_USAGE_LOG", True)):
        meter.jsonl_path = str(workspace.root / STEP_DIR_NAMES[MONITOR_STEP] /
                               ("llm-usage-web-step18-" + job_id + ".jsonl"))
    meter.begin_step(18)
    client._meter = meter
    return client, meter


def job_status(workspace, job_id, *, include_current_rows=True, use_preview_cache=False):
    with spec_lock(workspace.store):
        path = workspace._path("jobs", job_id, "J")
        if use_preview_cache:
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
            with _PREVIEW_CACHE_LOCK:
                cached = _PREVIEW_CACHE.get(cache_key)
                if cached is not None:
                    _PREVIEW_CACHE.move_to_end(cache_key)
            if cached is None:
                cached = read_json(path)
                with _PREVIEW_CACHE_LOCK:
                    _PREVIEW_CACHE[cache_key] = cached
                    while len(_PREVIEW_CACHE) > 2:
                        _PREVIEW_CACHE.popitem(last=False)
            job = dict(cached)
            if isinstance(job.get("result"), dict):
                job["result"] = dict(job["result"])
        else:
            job = read_json(path)
        result = job.get("result")
        if include_current_rows and job.get("status") == "done" and isinstance(result, dict) and workspace.pointer.exists():
            pointer = read_json(workspace.pointer)
            if job.get("base_version") == pointer.get("version"):
                # A proposal is built from this exact version, so none of its
                # rows or attributes can have been deleted since its creation.
                result["unavailable_rows"] = []
                result["unavailable_attributes"] = []
            else:
                availability_key = (str(workspace.store), pointer.get("version"))
                with _PREVIEW_CACHE_LOCK:
                    available = _AVAILABILITY_CACHE.get(availability_key)
                if available is None:
                    current = workspace._current()
                    available = ({r["id"] for r in current["rows"]}, {a["name"] for a in current["attributes"]})
                    with _PREVIEW_CACHE_LOCK:
                        _AVAILABILITY_CACHE[availability_key] = available
                        while len(_AVAILABILITY_CACHE) > 8:
                            _AVAILABILITY_CACHE.popitem(last=False)
                present, attrs = available
                result["unavailable_rows"] = sorted({p["row_id"] for p in result.get("patches", [])
                                                       if p.get("type") == "cell" and p["row_id"] not in present})
                requested = set((result.get("selection") or {}).get("attributes") or [])
                requested.update(p["attribute"] for p in result.get("patches", [])
                                 if p.get("type") in ("cell", "definition"))
                result["unavailable_attributes"] = sorted(requested - attrs)
    with _JOB_GUARD:
        alive = (str(workspace.store), job_id) in _ACTIVE
    if job["status"] in ("running", "pending", "cancelling") and not alive:
        # Server restart: show an explicit interrupted job, never spin forever.
        job.update(status="cancelled" if job["status"] == "cancelling" else "interrupted", message="服务已重启，任务未继续；当前有效数据未改变")
    return {"ok": True, **job}


def preview_page(workspace, job_id, *, page=0, term="", selection_rules=None):
    """Return one page of cell changes and compact metadata for a job."""
    from tools.attr_stability.step18_workspace import preview_matches, preview_selection
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise ValueError("预览页码无效")
    if not isinstance(term, str) or len(term) > 500:
        raise ValueError("预览筛选条件无效")
    job = job_status(workspace, job_id, use_preview_cache=True)
    result = job.get("result")
    if job.get("status") != "done" or not isinstance(result, dict):
        raise ValueError("任务状态已变化，请重新加载")
    all_patches = result.get("patches") or []
    cells = [p for p in all_patches if p.get("type") == "cell"]
    selected, available = preview_selection(cells, [] if selection_rules is None else selection_rules,
        result.get("unavailable_rows") or [], result.get("unavailable_attributes") or [])
    matching = [i for i, p in enumerate(cells) if preview_matches(p, term)]
    pages = max(1, (len(matching) + 49) // 50)
    page = min(page, pages - 1)
    entries = [{"index": i, "patch": cells[i], "selected": selected[i], "available": available[i]}
               for i in matching[page * 50:(page + 1) * 50]]
    meta = {k: v for k, v in result.items() if k not in ("patches", "warnings")}
    meta["warnings"] = (result.get("warnings") or [])[:40]
    meta["warning_count"] = len(result.get("warnings") or [])
    meta["non_cell_patches"] = [p for p in all_patches if p.get("type") != "cell"]
    return {"ok": True, "result": meta, "items": entries, "page": page, "pages": pages,
            "matched": len(matching), "total": len(cells), "selected_count": sum(selected)}


def latest_job(workspace):
    paths = list((workspace.store / "jobs").glob("J*.json"))
    if not paths:
        return None
    path = max(paths, key=lambda p: p.stat().st_mtime_ns)
    return job_status(workspace, path.stem, include_current_rows=False)


def _job_list_item(job):
    job = dict(job)
    proposal = job.pop("result", None) or {}
    job["selection"] = job.get("selection") or proposal.get("selection") or {}
    job["requirement"] = job.get("requirement") or proposal.get("requirement") or ""
    job["changed_cells"] = proposal.get("changed_cells")
    job["plan"] = proposal.get("plan") or []
    job["proposal_id"] = proposal.get("id")
    job["sample_changes"] = [{"row_id": p["row_id"], "attribute": p["attribute"],
                              "before": p["before"]["value"], "after": p["after"]["value"],
                              "evidence_only": p["before"]["value"] == p["after"]["value"]}
                             for p in islice((p for p in (proposal.get("patches") or []) if p.get("type") == "cell"), 3)]
    return job


def _save_job_summary(workspace, path, job, expected_stat=None):
    if job.get("status") not in ("done", "applied", "failed", "cancelled"):
        return
    try:
        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if expected_stat is not None and fingerprint != expected_stat:
            return
        write_json_atomic(workspace._path("job-summaries", job["id"], "J"),
                          {"fingerprint": list(fingerprint), "summary": _job_list_item(job)})
    except (OSError, ValueError, KeyError, TypeError):
        pass  # A cache failure must never affect the task or its result.


def list_jobs(workspace):
    paths = sorted((workspace.store / "jobs").glob("J*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:30]
    result = []
    for path in paths:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        with _JOB_LIST_CACHE_LOCK:
            cached = _JOB_LIST_CACHE.get(cache_key)
        if cached is not None:
            result.append(cached)
            continue
        job = None
        try:
            sidecar = read_json(workspace._path("job-summaries", path.stem, "J"))
            if sidecar.get("fingerprint") == [stat.st_mtime_ns, stat.st_size] and isinstance(sidecar.get("summary"), dict):
                job = sidecar["summary"]
        except (OSError, ValueError, TypeError):
            pass
        if job is None:
            raw_job = job_status(workspace, path.stem, include_current_rows=False)
            job = _job_list_item(raw_job)
            _save_job_summary(workspace, path, raw_job, expected_stat=(stat.st_mtime_ns, stat.st_size))
        # A finished job is immutable until its file changes. Running and
        # interrupted jobs must be rechecked against the live worker registry.
        if job.get("status") in ("done", "applied", "failed", "cancelled"):
            try:
                current_stat = path.stat()
                unchanged = (current_stat.st_mtime_ns, current_stat.st_size) == (stat.st_mtime_ns, stat.st_size)
            except OSError:
                unchanged = False
            if unchanged:
                with _JOB_LIST_CACHE_LOCK:
                    if len(_JOB_LIST_CACHE) >= 256:
                        _JOB_LIST_CACHE.clear()
                    _JOB_LIST_CACHE[cache_key] = job
        result.append(job)
    return result


def mark_applied(workspace, proposal_id, omitted_cells=0, *, job_id=None):
    with spec_lock(workspace.store):
        paths = ([workspace._path("jobs", job_id, "J")] if job_id
                 else (workspace.store / "jobs").glob("J*.json"))
        for path in paths:
            job = read_json(path)
            if job.get("status") == "done" and (job.get("result") or {}).get("id") == proposal_id:
                message = ("已部分应用，%d格未应用；如需处理请重新预览" % omitted_cells
                           if omitted_cells else "已确认应用")
                job.update(status="applied", message=message, updated_at=stamp())
                write_json_atomic(path, job)
                _save_job_summary(workspace, path, job)


def start_audit(workspace, request, config_values, *, client_factory=new_client):
    """二审「否/不适用」的依据是否成立，结果变成**待确认的预览**（不改数据）。"""
    import copy as _copy

    from tools.attr_stability.step18_model import audit_negatives, negative_candidates

    current = workspace.open()
    workspace._fresh(current, request.get("expected_version"))
    items = negative_candidates(current)
    if not items:
        raise ValueError("当前没有需要二审的「否/不适用」格")
    ident = token("J")
    path = workspace._path("jobs", ident, "J")
    key = str(workspace.store), ident
    with _JOB_GUARD:
        if (key[0], '__export__') in _ACTIVE:
            raise ValueError('标准化结果正在导出，请等待完成')
        _ACTIVE.add(key)
    job = {"id": ident, "status": "pending", "base_version": current["version"],
           "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
           "message": "准备二审", "result": None, "job_type": "audit",
           "selection": {"attributes": sorted({i["attr"] for i in items})},
           "row_count": len({i["row"] for i in items}),
           "requirement": "二审「否」判定依据是否成立（不支持的置未明确）"}
    with spec_lock(workspace.store):
        write_json_atomic(path, job)

    def run():
        meter = None
        def progress(message):
            _job_progress(workspace, path, job, message)

        try:
            job["status"] = "running"
            progress("正在连接大模型")
            llm, meter = _metered_client(workspace, config_values, ident, client_factory)
            verdict = audit_negatives(llm, items, progress)
            rejected = [(r, a, v) for r, a, v in
                        {(i["row"], i["attr"], i["value"]) for i in items}
                        if verdict.get((r, a), (True, ""))[0] is False]
            after = _copy.deepcopy(current)
            cells = workspace._cells(after)
            for r, a, _v in rejected:
                cell = cells[r, a]
                ok, why = verdict[(r, a)]
                cell.clear()
                cell.update({"value": "未明确", "source": "",
                             "basis": "二审：原依据未直接支持「否」（%s）" % (why or "无直接证据")})
            op = {"kind": "audit", "requirement": job["requirement"], "mode": "text",
                  "selection": job["selection"], "warnings": [],
                  "reviewed_rows": len({i["row"] for i in items}),
                  "reviewed_attributes": sorted({i["attr"] for i in items})}
            summary = workspace._save_proposal(current, after, op)
            summary["audit_reviewed"] = len(items)
            summary["audit_rejected"] = len(rejected)
            summary["audit_unanswered"] = len(items) - len(verdict)
            job.update(status="done", message="二审完成，确认应用后才改变数据", result=summary)
        except JobCancelled:
            job.update(status="cancelled", message="已取消，未应用数据", result=None)
        except Exception as exc:
            job.update(status="failed", message=str(exc), result=None)
        finally:
            try:
                _job_finish(workspace, path, job)
            finally:
                if meter is not None:
                    meter.finish_step(18, "DONE" if job["status"] == "done" else "INTERRUPTED" if job["status"] == "cancelled" else "FAILED")
                with _JOB_GUARD:
                    _ACTIVE.discard(key)

    threading.Thread(target=run, name="step18-audit-" + ident[1:9], daemon=True).start()
    return {"ok": True, "job_id": ident, "candidate_cells": len(items)}


def start_preview(workspace, request, config_values, *, client_factory=new_client):
    started_at = time.perf_counter()
    current = workspace.open()
    workspace._fresh(current, request.get("expected_version"))
    attrs, rows = workspace.select(current, request.get("selection") or {})
    if not str(request.get("requirement") or "").strip():
        raise ValueError("请填写处理要求")
    ident = token("J")
    path = workspace._path("jobs", ident, "J")
    key = str(workspace.store), ident
    with _JOB_GUARD:
        if (key[0], '__export__') in _ACTIVE:
            raise ValueError('标准化结果正在导出，请等待完成')
        if sum(k[0] == key[0] for k in _ACTIVE) >= 4:
            raise ValueError("当前已有4个预览任务，请等待一个完成后再提交")
        _ACTIVE.add(key)
    job = {"id": ident, "status": "pending", "base_version": current["version"],
           "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "message": "准备处理", "result": None,
           "selection": request.get("selection"), "row_count": len(rows),
           "requirement": str(request.get("requirement") or ""), "job_type": "preview",
           "request": {key: request[key] for key in ("selection", "requirement", "mode", "mapping", "batch") if key in request}}
    job["timings_ms"] = {"prepare": round((time.perf_counter() - started_at) * 1000, 1)}
    parent = request.get("_resume_from")
    if parent:
        prior = job_status(workspace, parent, include_current_rows=False)
        if prior.get("base_version") == current["version"] and prior.get("status") not in ("pending", "running", "cancelling"):
            job["checkpoint_parent"] = parent
    try:
        with spec_lock(workspace.store):
            write_json_atomic(path, job)
    except BaseException:
        with _JOB_GUARD:
            _ACTIVE.discard(key)
        raise

    def run():
        meter = None
        timed_model = None
        processing_start = time.perf_counter()
        def progress(message):
            _job_progress(workspace, path, job, message)
        try:
            job["status"] = "running"
            from tools.attr_stability.step18_workspace import is_fill_unknown, selected_assignment_target, decimal_places_requirement, selected_numeric_range_request, noise_bound_request, explicit_unit_requirement, daily_pints_unit_request
            # 「把空值改成未明确」这类要求是确定性的，不需要模型，也不该因为未配模型而失败
            deterministic = (request.get("mapping") is None
                             and (is_fill_unknown(str(request.get("requirement") or "")) or
                                  selected_assignment_target(str(request.get("requirement") or "")) is not None or
                                  decimal_places_requirement(str(request.get("requirement") or "")) is not None or
                                  selected_numeric_range_request(str(request.get("requirement") or "")) is not None or
                                  noise_bound_request(str(request.get("requirement") or "")) or
                                  daily_pints_unit_request(str(request.get("requirement") or ""), attrs) or
                                  explicit_unit_requirement(str(request.get("requirement") or ""), attrs) is not None))
            progress("正在按要求处理选中值" if deterministic
                     else ("正在连接大模型" if request.get("mapping") is None
                           else "正在归并选中取值"))
            llm = None
            if request.get("mapping") is None and not deterministic:
                llm, meter = _metered_client(workspace, config_values, ident, client_factory)
                timed_model = _TimedModel(llm)
                llm = timed_model
            checkpoint = _TextBatchCheckpoint(workspace, ident, job.get("checkpoint_parent"))
            result = (workspace.propose_batch(request, llm=llm, progress=progress, checkpoint=checkpoint, _prepared_base=current, _job_id=ident)
                      if request.get("batch") is not None else workspace.propose(request, llm=llm, progress=progress, checkpoint=checkpoint, _prepared_base=current, _job_id=ident))
            job.update(status="done", message="预览完成，确认应用后才改变数据", result=result)
        except JobCancelled:
            job.update(status="cancelled", message="已取消，未应用数据", result=None)
        except Exception as exc:
            # Config/client errors can contain endpoints, but never store key values.
            message = str(exc)
            secrets = [*config_values.items(), *apply_env_overrides(dict(config_values)).items()]
            for k, v in secrets:
                if any(part in k.upper() for part in ("KEY", "TOKEN", "PASSWORD", "COOKIE")) and v:
                    message = message.replace(str(v), "[已隐藏]")
            job.update(status="failed", message=message, result=None)
        finally:
            job["timings_ms"]["processing"] = round((time.perf_counter() - processing_start) * 1000, 1)
            if timed_model is not None:
                job["timings_ms"]["model_cumulative"] = round(timed_model.elapsed_ms, 1)
                job["timings_ms"]["model_calls"] = timed_model.calls
            try:
                _job_finish(workspace, path, job)
            finally:
                if meter is not None:
                    meter.finish_step(18, "DONE" if job["status"] == "done" else "INTERRUPTED" if job["status"] == "cancelled" else "FAILED")
                with _JOB_GUARD:
                    _ACTIVE.discard(key)

    threading.Thread(target=run, name="step18-preview-" + ident[1:9], daemon=True).start()
    return {"ok": True, "job_id": ident}
