"""Bounded classification inputs and evidence-addressed, restartable receipts."""
from __future__ import annotations

from collections import defaultdict, deque
import json
import re
import threading
import unicodedata

from .opportunity import digest, save
from .reference_categories import policy as reference_policy


FIELDS = ("review_key", "feature", "quote", "interpretation", "polarity", "scene", "user_object",
          "need_outcome", "evidence_role", "target_scope")
VERSION = "voc-classify-batched-v1"


def group_review_batches(aspects, limit):
    """Keep same-review aspects together where possible, without changing rows."""
    groups = {}
    for aspect in aspects:
        groups.setdefault(aspect["review_key"], []).append(aspect)
    batches = []
    for group in groups.values():
        for start in range(0, len(group), limit):
            part = group[start:start + limit]
            target = next((batch for batch in batches if len(batch) + len(part) <= limit), None)
            if target is None:
                batches.append(list(part))
            else:
                target.extend(part)
    baseline = [aspects[i:i + limit] for i in range(0, len(aspects), limit)]
    contexts = lambda batches: sum(len({a["review_key"] for a in batch}) for batch in batches)
    # Never add model calls merely to save a few repeated contexts.
    if len(batches) > len(baseline) or contexts(batches) >= contexts(baseline):
        return baseline
    return batches


class ContextIndex:
    def __init__(self, extracted):
        self.reviews = {r["review_key"]: r for r in extracted.get("reviews", [])}
        self.aspects = defaultdict(list)
        for aspect in extracted["aspects"]:
            self.aspects[aspect["review_key"]].append(aspect)

    def context(self, batch, *, compact=True, radius=5000):
        grouped = defaultdict(list)
        for a in batch:
            grouped[a["review_key"]].append(a)
        selected = {a["evidence_id"] for a in batch}
        result = []
        for key, local in grouped.items():
            review = self.reviews.get(key)
            if review is None:
                continue
            text = review["text"]
            if len(text) <= radius * 2:
                windows = [{"start": 0, "text": text}]
            elif not compact:
                starts = sorted({max(0, a["start"] - radius) for a in local})
                windows = [{"start": start, "text": text[start:start + radius * 2]} for start in starts]
            else:
                ranges = [(0, min(500, len(text))), (max(0, len(text) - 500), len(text))]
                ranges.extend((max(0, a["start"] - radius), min(len(text), a["end"] + radius))
                              for a in local if a.get("quote_field", "text") == "text")
                merged = []
                for start, end in sorted(ranges):
                    if merged and start <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                    else:
                        merged.append((start, end))
                windows = [{"start": start, "text": text[start:end]} for start, end in merged]
            item = {"review_key": key, "title": review["title"], "text_windows": windows,
                    "context_scope": "full_review" if len(text) <= radius * 2 else "quote_windows"}
            item["other_aspects" if compact else "all_aspects"] = [
                {k: a[k] for k in ("feature", "quote", "polarity", "interpretation")}
                for a in self.aspects[key] if not compact or a["evidence_id"] not in selected]
            result.append(item)
        return result


def node_id(node):
    from .classification import norm
    return digest([norm(node["category"]), norm(node["attribute"])])[:20]


def canonical_items(response, aliases, catalog_aliases, catalog):
    result = []
    for raw in response["items"]:
        reused = raw["existing_attribute_id"]
        node = catalog[catalog_aliases[reused]] if reused else {k: raw[k].strip() for k in ("category", "attribute")}
        evidence_id = aliases[raw["id"]]
        item = dict(raw, id=evidence_id, category=node["category"], attribute=node["attribute"],
                    existing_attribute_id="", draft_attribute_id=node_id(node))
        result.append({"evidence_id": evidence_id, "item": item})
    return result


def apply_items(items, catalog, assignments):
    for record in items:
        item = record["item"]
        catalog.setdefault(item["draft_attribute_id"], {k: item[k] for k in ("category", "attribute")})
        assignments[record["evidence_id"]] = item


class ProgressStore:
    def __init__(self, classifier, extracted, prompt):
        self.c = classifier
        engine = classifier.engine
        evidence = [{"evidence_id": a["evidence_id"], **{k: a.get(k) for k in (*FIELDS, "category_hint", "subcategory_hint")}}
                    for a in extracted["aspects"]]
        self.fp = digest([VERSION, prompt, reference_policy(), engine.config.get("LLM_MODEL"), engine.config.get("LLM_BASE_URL"),
                          sorted(evidence, key=lambda a: a["evidence_id"]),
                          sorted(extracted.get("reviews", []), key=lambda r: r["review_key"])])
        self.directory = engine.directory / "分类逐批断点" / self.fp[:24]
        self.state_path = engine.directory / "分类提速状态.json"
        self.known = {a["evidence_id"] for a in extracted["aspects"]}
        self.catalog, self.assignments = {}, {}
        self.lock = threading.RLock()
        self.import_done = False
        self.metrics = {"resumed_items": 0, "legacy_cache_requests": 0, "legacy_cache_items": 0}
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.import_done = state.get("fingerprint") == self.fp and state.get("legacy_import_complete", False)
        if self.directory.exists():
            paths = list(self.directory.glob("*.json")) + list(self.directory.glob("*.pending"))
            for path in sorted(paths):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if not self.valid(record):
                    continue
                canonical = self.directory / (digest(record) + ".json")
                if path.suffix == ".pending" and not canonical.exists():
                    save(canonical, record)
                self._apply(record["items"])
        self.metrics["resumed_items"] = len(self.assignments)

    def valid(self, record):
        if not isinstance(record, dict) or record.get("fingerprint") != self.fp:
            return False
        items = record.get("items")
        if not isinstance(items, list) or record.get("items_digest") != digest(items):
            return False
        seen = set()
        try:
            for row in items:
                ident, item = row["evidence_id"], row["item"]
                if ident not in self.known or ident in seen or item.get("existing_attribute_id") != "":
                    return False
                self.c.validate_items({"items": [dict(item, id="A1")]}, {"A1": ident}, {})
                if item.get("draft_attribute_id") != node_id(item):
                    return False
                seen.add(ident)
        except (ValueError, TypeError, KeyError):
            return False
        return True

    def _apply(self, items):
        for row in items:
            previous = self.assignments.get(row["evidence_id"])
            if previous is not None and previous != row["item"]:
                raise ValueError("同一证据存在冲突分类断点，未选择覆盖")
        apply_items(items, self.catalog, self.assignments)

    def commit(self, items):
        with self.lock:
            fresh = [r for r in items if r["evidence_id"] not in self.assignments]
            if not fresh:
                return
            record = {"fingerprint": self.fp, "items": fresh, "items_digest": digest(fresh)}
            if not self.valid(record):
                raise ValueError("分类逐批断点校验失败，未保存")
            save(self.directory / (digest(record) + ".json"), record)
            self._apply(fresh)

    def checkpoint(self, **extra):
        save(self.state_path, {"version": VERSION, "fingerprint": self.fp, "legacy_import_complete": self.import_done,
                               "classified_aspects": len(self.assignments), "metrics": self.metrics, **extra})


def make_task(batch, catalog, index, *, selected=None, compact=False, radius=5000):
    aliases = {f"A{i}": a["evidence_id"] for i, a in enumerate(batch, 1)}
    chosen = list(catalog) if selected is None else selected
    catalog_aliases = {f"T{i}": ident for i, ident in enumerate(chosen, 1)}
    evidence = [{"id": alias, **{k: a[k] for k in FIELDS},
                 "category_hint": a.get("category_hint", ""), "subcategory_hint": a.get("subcategory_hint", "")}
                for alias, a in zip(aliases, batch)]
    payload = {"task": "voc_classify", "reference_categories": reference_policy(),
               "catalog": [{"id": alias, **catalog[ident]} for alias, ident in catalog_aliases.items()],
               "evidence": evidence, "review_context": index.context(batch, compact=compact, radius=radius)}
    return aliases, catalog_aliases, payload


def import_legacy(classifier, extracted, index, store, prompt):
    """Recreate frozen legacy waves using cached responses only; no API path."""
    if store.import_done:
        return
    from .classification import CACHE_VERSION
    from .tasks import worker_count
    workers = worker_count(classifier.engine.config)
    audit = classifier.engine.directory / "方面分类-任务队列.json"
    if audit.exists():
        events = json.loads(audit.read_text(encoding="utf-8" )).get("events", [])
        width, run = 0, 0
        for event in events:
            run = run + 1 if event.get("status") == "running" else 0
            width = max(width, run)
        if width:
            workers = max(1, min(16, width))
    aspects = extracted["aspects"]
    ready = deque(aspects[i:i + 24] for i in range(0, len(aspects), 24))
    catalog = {}
    while ready:
        wave = []
        while ready and len(wave) < workers:
            batch = ready.popleft()
            task = make_task(batch, catalog, index)
            if len(json.dumps(task[2], ensure_ascii=False)) > 30000 and len(batch) > 1:
                mid = len(batch) // 2
                ready.extendleft(reversed([batch[:mid], batch[mid:]]))
            else:
                wave.append(task)
        missing = False
        for aliases, cat_aliases, payload in wave:
            key = digest([CACHE_VERSION, prompt, classifier.engine.config.get("LLM_MODEL"),
                          classifier.engine.config.get("LLM_BASE_URL"), payload])
            path = classifier.engine.directory / ("方面分类-" + key + ".json")
            if not path.exists():
                missing = True
                continue
            try:
                response = json.loads(path.read_text(encoding="utf-8"))
                classifier.validate_items(response, aliases, cat_aliases)
            except (ValueError, TypeError, KeyError):
                missing = True
                continue
            items = canonical_items(response, aliases, cat_aliases, catalog)
            store.commit(items)
            # Legacy results were applied in input order after each frozen
            # wave. Preserve overwrites of normalized label spellings too.
            for record in items:
                item = record["item"]
                catalog[item["draft_attribute_id"]] = {k: item[k] for k in ("category", "attribute")}
            store.metrics["legacy_cache_requests"] += 1
            store.metrics["legacy_cache_items"] += len(items)
        if missing:
            break  # Later legacy aliases depend on the unknown wave results.
    store.import_done = True
    store.checkpoint(status="running")


class CatalogCandidates:
    def __init__(self):
        self.units = {}

    @staticmethod
    def terms(value):
        text = unicodedata.normalize("NFKC", value).casefold()
        words = set(re.findall(r"\w+", text))
        for word in list(words):
            words.update(word[i:i + 2] for i in range(len(word) - 1))
        return words

    def select(self, catalog, batch, limit):
        queries = [self.terms(" ".join(str(a.get(k) or "") for k in
                   ("feature", "category_hint", "subcategory_hint", "need_outcome"))) for a in batch]
        scored = []
        for ident, node in catalog.items():
            signature = (node["category"], node["attribute"])
            if ident not in self.units or self.units[ident][0] != signature:
                self.units[ident] = (signature, self.terms(signature[0]), self.terms(signature[1]))
            _, category, attribute = self.units[ident]
            score = sum(3 * len(q & attribute) + len(q & category) for q in queries)
            scored.append((-score, ident))
        return [ident for _, ident in sorted(scored)[:limit]]


def prepare_bounded(batch, catalog, index, candidates, prompt, input_limit, catalog_limit):
    from .extraction_budget import estimate_tokens
    from .tasks import BatchTooLarge
    selected = candidates.select(catalog, batch, catalog_limit)
    task = make_task(batch, catalog, index, selected=selected, compact=True)
    while estimate_tokens(prompt + json.dumps(task[2], ensure_ascii=False)) + 2048 > input_limit:
        if selected:
            selected = selected[:len(selected) // 2]
            task = make_task(batch, catalog, index, selected=selected, compact=True)
        elif len(batch) > 1:
            raise BatchTooLarge("分类输入超过字节预算，需拆批")
        else:
            for radius in (1500, 500):
                task = make_task(batch, catalog, index, selected=[], compact=True, radius=radius)
                if estimate_tokens(prompt + json.dumps(task[2], ensure_ascii=False)) + 2048 <= input_limit:
                    break
            else:
                raise ValueError("单条分类证据超过输入预算，未发送、未截断原句")
            break
    aliases, cat_aliases, payload = task
    return aliases, cat_aliases, payload, {ident: dict(catalog[ident]) for ident in cat_aliases.values()}
