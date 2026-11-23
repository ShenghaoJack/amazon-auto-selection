"""步骤17：只对 A1/A2 分歧单元格补充第三个独立判断。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Mapping

from .attr_stability import _key_of
from .spec_io import write_json_atomic

FIELDS = ("标题", "亮点", "五点描述", "详细参数", "SKU")
UNKNOWN = {"未明确", "未知", "待确认"}


def _accepted(cell, source_row):
    if not isinstance(cell, dict) or not isinstance(cell.get("value"), str):
        return None
    val = cell["value"].strip()
    basis = str(cell.get("basis") or "").strip()
    source = str(cell.get("source") or "").strip()
    if not val or not basis:
        return None
    if val in UNKNOWN:
        return {"value": "未明确", "source": "", "basis": basis}
    match = re.match(r"^(标题|亮点|五点描述|详细参数|SKU)\s*[：:]\s*", basis, re.I)
    quote = basis[match.end():].strip() if match else basis
    norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().casefold()
    needle = norm(quote)
    hits = [f for f in FIELDS if len(needle) >= 4 and needle in norm(source_row.get(f))]
    if source not in hits:
        source = hits[0] if len(hits) == 1 else ""
    return {"value": val, "source": source, "basis": basis} if source else None


def adjudicate(cells, runs, producer, rules, log=print):
    """仅修改产生第三票的分歧格；失败或三票不同保留待复核。"""
    conflict = {}
    for key, cell in cells.items():
        if cell.get("kind") != "value" or cell.get("agree"):
            continue
        rid, _, path = key.partition("|")
        name = path.removeprefix("attr.")
        if path.startswith("attr.") and name in rules.attrs and rid.isdigit():
            conflict.setdefault(int(rid), []).append(name)
    inputs = {int(row["xl_row"]): row for row in producer.rows if str(row.get("xl_row", "")).isdigit()}
    conflict = {rid: sorted(names) for rid, names in sorted(conflict.items()) if rid in inputs}
    total = sum(map(len, conflict.values()))
    if not total:
        return {"target_cells": 0, "target_rows": 0, "completed_cells": 0, "resolved_cells": 0}
    # 指纹包括两遍提名：同样的商品但不同的结果不得沿用第三票。
    entries = [(rid, name, cells["%s|attr.%s" % (rid, name)]["values"])
               for rid, names in conflict.items() for name in names]
    contract_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    fingerprint = hashlib.sha256((producer._input_key() + contract_hash + json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)).encode()).hexdigest()
    cache_dir = Path(producer.out_dir) / "a3_conflicts" / fingerprint
    cache_dir.mkdir(parents=True, exist_ok=True)
    hints = getattr(producer, "adjudication_hints", {})
    completed, resolved, calls = 0, 0, 0
    ids = list(conflict)
    for start in range(0, len(ids), 4):
        batch = ids[start:start + 4]
        path = cache_dir / ("%06d.json" % (start // 4))
        cached = {}
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("fingerprint") == fingerprint and isinstance(saved.get("answers"), dict):
                for rid in batch:
                    values = saved["answers"].get(str(rid))
                    if isinstance(values, dict):
                        for name in conflict[rid]:
                            accepted = _accepted(values.get(name), inputs[rid])
                            if accepted is not None:
                                cached.setdefault(str(rid), {})[name] = accepted
        except (OSError, ValueError, TypeError):
            pass
        tasks = []
        for rid in batch:
            names = [n for n in conflict[rid] if n not in (cached.get(str(rid)) or {})]
            if names:
                tasks.append({"xl_row": rid, "待复核属性": names,
                              "资料": {f: inputs[rid].get(f, "") for f in FIELDS}})
        if tasks:
            requested = {n for item in tasks for n in item["待复核属性"]}
            definitions = {n: {"hint": hints.get(n, ""), "values": list(rules.attrs[n].values or [])}
                           for n in sorted(requested)}
            prompt = ("对每行指定属性独立重判，只根据本行标题、五点、参数、SKU和属性定义；"
                      "不要猜测，不因没提及填否。具体值需引用连续原文并写来源；证据不足填未明确并说明。"
                      "每行仅返回列出的属性，不能遗漏、跨商品引用或新增属性。\n"
                      "属性定义：" + json.dumps(definitions, ensure_ascii=False, separators=(",", ":")) +
                      "\n商品资料：" + json.dumps(tasks, ensure_ascii=False, separators=(",", ":")) +
                      '\n严格返回JSON：{"rows":[{"xl_row":1,"attributes":{"属性名":{"value":"值","source":"标题","basis":"原文"}}}]}')
            try:
                calls += 1
                result = producer.fn(prompt)
                rows = result.get("rows") if isinstance(result, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("未返回 rows 列表")
                requested_by_id = {str(t["xl_row"]): set(t["待复核属性"]) for t in tasks}
                seen = set()
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    rid_s = str(row.get("xl_row", ""))
                    if rid_s not in requested_by_id or rid_s in seen:
                        continue
                    seen.add(rid_s)
                    attrs = row.get("attributes")
                    if not isinstance(attrs, Mapping):
                        continue
                    for name, answer in attrs.items():
                        if name not in requested_by_id[rid_s]:
                            continue
                        accepted = _accepted(answer, inputs[int(rid_s)])
                        if accepted is not None:
                            cached.setdefault(rid_s, {})[name] = accepted
                if cached:
                    write_json_atomic(path, {"fingerprint": fingerprint, "answers": cached})
            except Exception as exc:
                log("   冲突复核批次 %d 失败，保留原提名待复核: %s" % (start // 4 + 1, str(exc)[:120]))
        for rid in batch:
            for name in conflict[rid]:
                answer = (cached.get(str(rid)) or {}).get(name)
                if not isinstance(answer, dict):
                    continue
                completed += 1
                cell = cells["%s|attr.%s" % (rid, name)]
                groups = cell["values"]
                candidate = _key_of(answer["value"])
                group = next((g for g in groups if _key_of(g["value"]) == candidate), None)
                if group is None:
                    group = {"value": answer["value"], "by": []}
                    groups.append(group)
                if "a3_conflict" not in group["by"]:
                    group["by"].append("a3_conflict")
                groups.sort(key=lambda g: -len(g["by"]))
                has_majority = len(groups[0]["by"]) >= 2
                cell.update(mode=groups[0]["value"] if has_majority else "", n_votes=len(groups),
                            no_majority=not has_majority, needs_review=not has_majority,
                            conflict_resolution="多数确定" if has_majority else "三次不同待复核",
                            third_judgment=answer)
                if has_majority:
                    resolved += 1
                    # 第三票与多数值相同，其来源与依据绑定到该值。
                    for suffix, content in (("_来源", answer["source"]), ("_依据", answer["basis"])):
                        ref = cells.get("%s|attr.%s%s" % (rid, name, suffix))
                        if ref is not None:
                            ref["mode"] = content
        if log:
            log("   冲突复核 %d/%d 行，已完成 %d/%d 格" % (min(start + 4, len(ids)), len(ids), completed, total))
    return {"target_cells": total, "target_rows": len(ids), "completed_cells": completed,
            "resolved_cells": resolved, "unresolved_cells": total - resolved, "model_calls": calls}
