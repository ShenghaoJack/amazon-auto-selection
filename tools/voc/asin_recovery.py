"""Explicit missing-ASIN audit and bounded cross-MCP acquisition, without guessing."""
from collections import Counter
import csv
import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from .dedupe import date_compatible
from .opportunity import digest, save
from .tasks import BatchQueue, worker_count

VERSION = "voc-missing-asin-v1"
SOURCES = ("pangolinfo", "sellersprite")


def missing_asin(row):
    return not str(row.get("variant_asin") or "").strip() or row.get("asin_role") in {"unknown", "conflict"}


def request_key(row):
    return (row.get("source"), row.get("parent_asin"), row.get("queried_asin") or row.get("parent_asin"),
            row.get("source_stratum") or "critical")


def csv_file(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            values = {key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (dict, list))
                      else row.get(key, "") for key in fields}
            writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
                             for k, v in values.items()})


class AsinRecovery:
    def __init__(self, collector, directory, catalog, plan):
        self.collector, self.directory, self.catalog = collector, Path(directory), catalog
        self.directory.mkdir(parents=True, exist_ok=True)
        self.plan = {s: (f, stars, pages) for s, f, stars, pages in plan}
        self.tasks = {}
        self.lock = threading.RLock()
        self.initial = []
        self.running = False
        self.summary = {"version": VERSION, "pending_tasks": 0}

    def cached(self, task):
        from .pipeline import no_review_scope
        key = (task["other_source"], task["parent_asin"], task["queried_asin"], task["stratum"])
        if key not in self.collector.done:
            return None
        record = self.collector.completed_records.get(key)
        return record if record and (int(record.get("pages") or 0) >= task["pages"] or no_review_scope(record)) else None

    def persist(self):
        with self.lock:
            snapshot = deepcopy(list(self.tasks.values()))
        save(self.directory / "ASIN跨源补抓任务.json", {"version": VERSION, "tasks": snapshot})

    def mark_redirect(self, task, notice):
        allowed = {p["variant_asin"] for p in self.catalog["products"]}
        outside = notice["destination_asin"] not in allowed
        task.update(status="redirected_out_of_scope" if outside else "redirected_manual_review",
            redirected_asin=notice["destination_asin"],
            billing_note="服务端声明已计费，金额未返回" if notice.get("billed_notice") else "计费信息未提供",
            reason="目标ASIN不在步骤18范围，未自动查询" if outside else "目标需核查，未自动重试旧ASIN或追跳")
        task.pop("error", None)

    @staticmethod
    def mark_checked(task, record, reused):
        from .pipeline import no_review_scope
        empty_scope = no_review_scope(record)
        task.update(status="checked", reused=reused, returned_reviews=record.get("rows", 0),
                    skipped_no_reviews=bool(empty_scope), empty_scope=empty_scope)
        if empty_scope:
            task["reason"] = "替代来源明确无评论，已跳过该查询范围；原缺失ASIN评论仍保留为未确认"
        task.pop("error", None)

    def run(self, rows):
        self.running = True
        self.initial = [dict(r) for r in rows if missing_asin(r)]
        parents = {p.get("parent_asin") for p in self.catalog["products"] if p.get("parent_asin")}
        for row in self.initial:
            source, parent, target, stratum = request_key(row)
            other = next((s for s in SOURCES if s != source), None) if source in SOURCES else None
            ident = digest([VERSION, source, parent, target, stratum])
            if ident not in self.tasks:
                _, _, pages = self.plan.get(stratum, self.plan.get("critical", ("critical", [1, 2, 3], 1)))
                self.tasks[ident] = {"task_id": ident, "source": source, "other_source": other,
                    "parent_asin": parent, "queried_asin": target, "stratum": stratum, "pages": pages,
                    "missing_reviews": 0, "attempts": 0,
                    "status": "queued" if parent in parents and target and other else "out_of_scope",
                    "reason": "评论未返回可确认ASIN；按同一查询目标和星级层换MCP补抓"}
            self.tasks[ident]["missing_reviews"] += 1
        self.persist()
        self.annotate(rows)  # Persist the missing rows before any paid calls.
        engine = SimpleNamespace(config=self.collector.config, directory=self.directory, log=self.collector.log)

        def work(batch):
            task = self.tasks[batch[0]]
            cached = self.cached(task)
            if cached is not None:
                return cached, True
            source = task["other_source"]
            # A confirmed absent review page is terminal for this source/ASIN,
            # including other star ranges. Do not spend another request on it.
            absent = getattr(self.collector, "empty_asin_records", {}).get(
                (source, task["parent_asin"], task["queried_asin"]))
            if absent is not None:
                return absent, True
            if source in getattr(self.collector, "exhausted_sources", set()):
                raise RuntimeError("替代MCP额度耗尽，待后续恢复后补试")
            if getattr(self.collector, "pango" if source == "pangolinfo" else "ss", None) is None:
                raise RuntimeError("替代MCP未配置，未使用查询ASIN猜测归属")
            p_filter, stars, _ = self.plan.get(task["stratum"], self.plan.get("critical", ("critical", [1, 2, 3], 1)))
            return self.collector.one_call(source, task["parent_asin"], task["queried_asin"], task["stratum"],
                                           p_filter, stars, task["pages"]), False

        def checked_work(batch):
            # Task fields below are owned by one worker per request key.
            task = self.tasks[batch[0]]
            with self.lock:
                task["attempts"] += 1
            try:
                return work(batch)
            except Exception as exc:
                from tools.voc.redirects import ASINRedirected
                if isinstance(exc, ASINRedirected):
                    return {"redirect_notice": exc.notice}, False
                with self.lock:
                    task.update(status="pending", error=str(exc)[:300])
                from core.mcp import MCPBusinessError
                if isinstance(exc, MCPBusinessError):
                    self.collector._switch_source(exc, task["other_source"])
                raise

        # Finish every primary recovery job and its deferred retries before
        # any new SellerSprite quality-recovery call is allowed.
        for source in SOURCES:
            batches = [[key] for key, t in self.tasks.items()
                       if t["status"] == "queued" and t["other_source"] == source]
            if not batches:
                continue
            self.collector.log(f"[VOC来源] ASIN补抓阶段：{source}，任务{len(batches)}组；Pangolinfo先完成及补试，卖家精灵最后兜底")
            queue = BatchQueue(engine, "ASIN跨源补抓", batches, split=False)
            for batch, (record, reused) in queue.parallel(checked_work, workers=worker_count(self.collector.config, "VOC_FETCH_WORKERS"), ordered=False):
                task = self.tasks[batch[0]]
                if record.get("redirect_notice"):
                    self.mark_redirect(task, record["redirect_notice"])
                else:
                    self.mark_checked(task, record, reused)
                self.persist()
        # Pending tasks do not block independent LLM work; the step completion
        # guard will request a resume after preserving successful outputs.
        self.persist()
        self.running = False
        return bool(self.tasks)

    def annotate(self, rows):
        # Late replies / the existing parent-level repair may have completed a
        # queued cross-source request. Reconcile against raw success, not flags.
        for task in self.tasks.values():
            notice = self.collector.redirect_notice(task["other_source"], task["queried_asin"])
            if notice:
                self.mark_redirect(task, notice)
                continue
            cached = self.cached(task) if task["status"] != "out_of_scope" else None
            if cached is not None:
                self.mark_checked(task, cached, True)
                task.pop("error", None)
        by_key = {(t["source"], t["parent_asin"], t["queried_asin"], t["stratum"]): t for t in self.tasks.values()}
        by_target = {(t["parent_asin"], t["queried_asin"], t["stratum"]): t for t in self.tasks.values()}
        final = {}
        for row in rows:
            final.setdefault(row.get("fallback_key"), []).append(row)
        audit = []
        for original in self.initial:
            matches = [r for r in final.get(original.get("fallback_key"), []) if not missing_asin(r)
                and date_compatible(original.get("review_date"), r.get("review_date"))
                and not (original.get("review_id") and r.get("review_id") and original["review_id"] != r["review_id"])]
            current = matches[0] if (matches and original.get("asin_role") != "conflict"
                                     and len({r["variant_asin"] for r in matches}) == 1) else None
            task = by_key.get(request_key(original), {})
            resolved = current is not None and not missing_asin(current)
            state = ("已通过评论去重匹配补齐" if resolved else "已重定向，未自动追跳" if task.get("status", "").startswith("redirected_")
                     else "替代来源无评论，已跳过" if task.get("skipped_no_reviews")
                     else "已跨源补抓，原评论ASIN仍未确认" if task.get("status") == "checked"
                     else "范围外，未补抓" if task.get("status") == "out_of_scope" else "待跨源补抓/重试")
            audit.append({"review_id": original.get("review_id"), "source": original.get("source"),
                "parent_asin": original.get("parent_asin"), "queried_asin": original.get("queried_asin"),
                "source_stratum": original.get("source_stratum"), "ASIN状态": "已补齐" if resolved else "冲突" if original.get("asin_role") == "conflict" else "缺失/未确认",
                "原始ASIN": original.get("variant_asin"), "确认ASIN": current.get("variant_asin") if resolved else "",
                "补抓状态": state, "替代MCP": task.get("other_source"), "task_id": task.get("task_id"),
                "无评论跳过范围": task.get("empty_scope", ""),
                "重定向目标ASIN": task.get("redirected_asin"), "计费提示": task.get("billing_note"),
                "原文标题": original.get("review_title"), "评论原文": original.get("review_body"),
                "处理说明": "仅精确评论匹配后采用明确ASIN；新抓到其他评论不代表原评论归属已补齐"})
        for row in rows:
            if missing_asin(row):
                key = request_key(row)
                task = by_key.get(key) or by_target.get(key[1:], {})
                row["asin_status"] = "冲突" if row.get("asin_role") == "conflict" else "缺失"
                row["asin_recovery_status"] = ("已重定向，未自动追跳" if task.get("status", "").startswith("redirected_")
                    else "替代来源无评论，已跳过" if task.get("skipped_no_reviews")
                    else "已跨源补抓仍未确认" if task.get("status") == "checked"
                    else "范围外未补抓" if task.get("status") == "out_of_scope" else "待跨源补抓/重试")
        csv_file(self.directory / "ASIN缺失评论明细.csv", audit, ["review_id", "source", "parent_asin", "queried_asin", "source_stratum",
            "ASIN状态", "原始ASIN", "确认ASIN", "补抓状态", "替代MCP", "task_id", "无评论跳过范围", "重定向目标ASIN", "计费提示", "原文标题", "评论原文", "处理说明"])
        pending = sum(t["status"] == "pending" for t in self.tasks.values())
        queued = sum(t["status"] == "queued" for t in self.tasks.values())
        self.summary = {"version": VERSION, "status": "running" if self.running else "pending" if pending or queued else "complete",
            "missing_reviews_before": len(self.initial), "request_groups": len(self.tasks),
            "pending_tasks": pending + queued, "queued_tasks": queued,
            "checked_groups": sum(t["status"] == "checked" for t in self.tasks.values()),
            "no_review_skipped_groups": sum(bool(t.get("skipped_no_reviews")) for t in self.tasks.values()),
            "redirected_groups": sum(t["status"].startswith("redirected_") for t in self.tasks.values()),
            "missing_reviews_after": sum(missing_asin(r) for r in rows),
            "review_recovery_states": dict(Counter(a["补抓状态"] for a in audit))}
        self.persist()
        save(self.directory / "ASIN跨源补抓结果.json", self.summary)
        return self.summary
