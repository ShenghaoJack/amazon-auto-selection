"""VOC-only bounded calls and deferred batch retries; no dependency skipping."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import json
from pathlib import Path
import threading
import time

from core.errors import LLMError


# These stages consist of independent, separately cached tasks. Their queue
# audit is not the durable model-result cache, so snapshotting can be batched.
# Keep legacy ordered queues (especially Need归并) synchronous: their event
# sequence is also used to migrate older checkpoints.
_BATCHED_AUDIT_STAGES = frozenset({"Need功能字段关联", "已有解法核查", "剩余Open生成", "Open独立复查"})
_AUDIT_BATCH_EVENTS = 100
_AUDIT_INTERVAL_SECONDS = 2.0
_PROGRESS_INTERVAL_SECONDS = 30.0


def worker_count(config, key="VOC_LLM_WORKERS"):
    """Bound concurrency at the VOC boundary, including orphaned deadline calls."""
    return max(1, min(16, int(config.get(key, 4) or 1)))


class TaskDeferred(LLMError):
    pass


class BatchTooLarge(ValueError):
    """Split a request before sending it; not a paid-call failure."""


class DeadlineCalls:
    """Keep late results addressable; never resend an identical in-flight call."""
    def __init__(self, timeout=120, max_active=4):
        self.timeout = float(timeout)
        self.max_active = max(1, int(max_active))
        if self.timeout <= 0:
            raise ValueError("VOC调用超时必须大于0")
        self.active = {}
        self.lock = threading.Lock()

    def call(self, key, function):
        with self.lock:
            box = self.active.get(key)
            if box is not None and not box["done"].is_set():
                raise TaskDeferred("同一请求仍在执行，已延后；不重复发送")
            # The original waiter may have yielded just before the client
            # finished with a timeout. A retry must send a new request rather
            # than spend its sole queue round reading that stale timeout again.
            if box is not None:
                error = box.get("error")
                if isinstance(error, LLMError) and ("请求超时" in str(error)
                        or "request timed out" in str(error).lower()):
                    self.active.pop(key, None)
                    box = None
            start = box is None
            if start:
                if sum(not b["done"].is_set() for b in self.active.values()) >= self.max_active:
                    raise TaskDeferred("未返回调用已达并发保护上限，已延后")
                box = {"done": threading.Event()}
                self.active[key] = box
        if start:
            def invoke():
                try:
                    box["value"] = function()
                except Exception as exc:
                    box["error"] = exc
                finally:
                    box["done"].set()
            threading.Thread(target=invoke, daemon=True, name="voc-deadline").start()
            deadline = time.monotonic() + self.timeout
            while not box["done"].wait(min(0.1, max(0, deadline - time.monotonic()))):
                if time.monotonic() >= deadline:
                    raise TaskDeferred(f"VOC调用超过{self.timeout:g}秒无结果，先运行其他任务")
        with self.lock:
            if self.active.get(key) is box:
                self.active.pop(key, None)
        if "error" in box:
            raise box["error"]
        return box["value"]


class BatchQueue:
    """Finish independent batches first, then retry failures, then shrink if useful.

    Worker caches, not this audit, decide reusable success. A pending stage never
    counts as complete, even if other batches have succeeded.
    """
    def __init__(self, engine, stage, batches, *, split=True):
        self.engine, self.stage = engine, stage
        self.ready, self.deferred = deque(batches), []
        self.round = 0
        self.rounds = max(0, int(engine.config.get("VOC_RETRY_ROUNDS", 1)))
        self.split = split
        self.pending = []
        self.events = []
        self.inflight = set()
        self._batched_audit = stage in _BATCHED_AUDIT_STAGES
        self._dirty_events = 0
        self._last_audit = None
        self._last_progress = time.monotonic()
        self._completed_batches = 0

    def __bool__(self):
        if not self.ready and self.deferred:
            self.ready.extend(self.deferred)
            self.deferred = []
            self.round += 1
        return bool(self.ready)

    def pop(self):
        return self.ready.popleft()

    def prepend(self, batches):
        self.ready.extendleft(reversed(batches))

    def record(self, batch, status, error=None):
        from tools.voc.opportunity import digest
        self.events.append({"task_id": digest(batch), "round": self.round, "status": status,
                            **({"error": str(error)[:300]} if error is not None else {})})
        self._dirty_events += 1
        if status == "complete":
            self._completed_batches += 1
        now = time.monotonic()
        if (not self._batched_audit or self._last_audit is None
                or self._dirty_events >= _AUDIT_BATCH_EVENTS
                or now - self._last_audit >= _AUDIT_INTERVAL_SECONDS):
            self._flush_audit()
        if self._batched_audit and now - self._last_progress >= _PROGRESS_INTERVAL_SECONDS:
            self.engine.log(f"[VOC进度] {self.stage}: 已完成 {self._completed_batches} 批，"
                            f"待处理 {len(self.ready)} 批，运行中 {len(self.inflight)} 批，"
                            f"待补试 {len(self.deferred)} 批，待重试 {len(self.pending)} 批")
            self._last_progress = now

    def _flush_audit(self):
        if not self._dirty_events:
            return
        from tools.voc.opportunity import save
        save(self.engine.directory / (self.stage + "-任务队列.json"),
             {"stage": self.stage, "round": self.round, "ready": len(self.ready),
              "deferred": len(self.deferred), "pending": len(self.pending),
              "in_flight": sorted(self.inflight), "events": self.events})
        self._dirty_events = 0
        self._last_audit = time.monotonic()

    def fail(self, batch, exc):
        if self.round < self.rounds:
            self.deferred.append(batch)
            self.record(batch, "deferred", exc)
        elif self.split and len(batch) > 1 and not isinstance(exc, (LLMError, TaskDeferred)):
            mid = len(batch) // 2
            self.ready.extend((batch[:mid], batch[mid:]))
            self.record(batch, "split", exc)
        else:
            self.pending.append((batch, str(exc)[:300]))
            self.record(batch, "pending", exc)
        self.engine.log(f"[VOC任务] {self.stage}失败，其他独立任务继续，稍后补试：{str(exc)[:160]}")

    def finish(self):
        self._flush_audit()
        if self.pending:
            raise RuntimeError(f"VOC {self.stage}仍有{len(self.pending)}个任务待重试，已保留成功缓存与队列")

    def parallel(self, work, *, prepare=None, workers=None, ordered=True):
        """Bounded waves; workers cache results, coordinator owns queue and commits.

        Prepare an immutable catalog snapshot for the entire wave before starting
        any calls. Yield in input order, so catalog IDs never depend on latency.
        Deferred retries begin only after all first-pass jobs have been attempted.
        """
        try:
            yield from self._parallel(work, prepare=prepare, workers=workers, ordered=ordered)
        finally:
            # Some callers deliberately do not call finish(); an audit is still
            # required on normal exhaustion, generator close, or exceptions.
            self._flush_audit()

    def _parallel(self, work, *, prepare=None, workers=None, ordered=True):
        workers = workers or worker_count(self.engine.config)
        self.engine.log(f"[VOC并行] {self.stage}：并发{workers}")
        if not ordered:
            yield from self._rolling(work, prepare, workers)
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="voc-batch") as pool:
            first_dispatch = True
            while self:
                wave = []
                while self.ready and len(wave) < workers:
                    batch = self.pop()
                    try:
                        item = prepare(batch) if prepare else batch
                    except BatchTooLarge:
                        if len(batch) <= 1:
                            raise
                        mid = len(batch) // 2
                        self.prepend([batch[:mid], batch[mid:]])
                        self.record(batch, "split_before_call")
                        continue
                    except Exception as exc:
                        self.fail(batch, exc)
                        continue
                    wave.append((batch, item))
                from tools.voc.opportunity import digest
                for batch, _ in wave:
                    self.inflight.add(digest(batch))
                    self.record(batch, "running")
                if first_dispatch and wave:
                    self._flush_audit()
                    first_dispatch = False
                futures = {pool.submit(work, item): i for i, (_, item) in enumerate(wave)}
                completed = {}
                for future in as_completed(futures):
                    i = futures[future]
                    batch = wave[i][0]
                    self.inflight.discard(digest(batch))
                    try:
                        completed[i] = future.result()
                    except OSError:
                        # A failed durable commit must not resend a paid request.
                        raise
                    except Exception as exc:
                        self.fail(batch, exc)
                    else:
                        self.record(batch, "complete")
                for i, (batch, _) in enumerate(wave):
                    if i in completed:
                        yield batch, completed[i]

    def _rolling(self, work, prepare, workers):
        """Independent tasks refill free slots without waiting for a slow peer."""
        from tools.voc.opportunity import digest
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="voc-batch") as pool:
            active = {}
            first_dispatch = True
            while self.ready or self.deferred or active:
                if not self.ready and not active:
                    bool(self)  # Start retries only after the whole first pass finishes.
                while self.ready and len(active) < workers:
                    batch = self.pop()
                    try:
                        item = prepare(batch) if prepare else batch
                    except BatchTooLarge:
                        if len(batch) <= 1:
                            raise
                        mid = len(batch) // 2
                        self.prepend([batch[:mid], batch[mid:]])
                        self.record(batch, "split_before_call")
                        continue
                    except Exception as exc:
                        self.fail(batch, exc)
                        continue
                    self.inflight.add(digest(batch))
                    self.record(batch, "running")
                    active[pool.submit(work, item)] = batch
                if first_dispatch and active:
                    self._flush_audit()
                    first_dispatch = False
                if not active:
                    continue
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    batch = active.pop(future)
                    self.inflight.discard(digest(batch))
                    try:
                        result = future.result()
                    except OSError:
                        raise
                    except Exception as exc:
                        self.fail(batch, exc)
                    else:
                        self.record(batch, "complete")
                        yield batch, result


class PhaseStore:
    """Input-addressed phase snapshots, checked independently of DONE markers."""
    def __init__(self, engine):
        self.engine = engine
        self.path = engine.directory / "阶段断点.json"
        self.events = []
        self.stages = {}

    @staticmethod
    def logic(*names):
        import hashlib
        return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}

    def run(self, name, inputs, function):
        from tools.voc.opportunity import digest, save
        key = digest(["voc-phase-v1", name, inputs])
        path = self.engine.directory / "断点数据" / (name + "-" + key[:24] + ".json")
        stage = {"fingerprint": key, "snapshot": str(path), "status": "running", "reused": False}
        self.stages[name] = stage
        def write():
            save(self.path, {"version": "voc-phase-v1", "stages": self.stages, "events": self.events})
        try:
            cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            if (cached and cached.get("fingerprint") == key
                    and digest(cached["result"]) == cached.get("result_digest")):
                stage.update(status="complete", reused=True)
                self.engine.log(f"[VOC断点] 复用已完成阶段：{name}")
                write()
                return cached["result"]
        except (OSError, ValueError, KeyError, TypeError):
            self.events.append({"phase": name, "event": "snapshot_invalid_rebuild"})
        write()
        try:
            result = function()
            save(path, {"fingerprint": key, "result_digest": digest(result), "result": result})
        except Exception as exc:
            stage.update(status="pending", error=type(exc).__name__ + ": " + str(exc)[:300])
            write()
            raise
        stage.update(status="complete")
        write()
        return result

    def exported(self, chain):
        from tools.voc.opportunity import digest, save
        self.stages["导出"] = {"status": "complete", "fingerprint": digest(chain), "reused": False,
                              "note": "每次重建派生交付，恢复缺失CSV；不新增模型或MCP调用"}
        save(self.path, {"version": "voc-phase-v1", "stages": self.stages, "events": self.events})

    def exporting(self, error=None):
        from tools.voc.opportunity import save
        self.stages["导出"] = {"status": "pending" if error is not None else "running",
                              **({"error": str(error)[:300]} if error is not None else {})}
        save(self.path, {"version": "voc-phase-v1", "stages": self.stages, "events": self.events})
