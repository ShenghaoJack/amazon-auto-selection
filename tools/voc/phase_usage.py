"""Summarize actual provider usage for the current VOC process by stage."""
import json
from pathlib import Path


def summarize(path, run_id, pid):
    phases = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "call" or row.get("step") != 19 or row.get("run_id") != run_id or row.get("pid") != pid:
            continue
        purpose = str(row.get("purpose") or "")
        name = purpose[4:] if purpose.startswith("VOC:") else "其他或未标记"
        item = phases.setdefault(name, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                        "cost_cny": 0.0, "missing_usage": 0, "unpriced": 0})
        item["calls"] += 1
        item["input_tokens"] += int(row.get("in_hit") or 0) + int(row.get("in_miss") or 0)
        item["output_tokens"] += int(row.get("out") or 0)
        item["cost_cny"] += float(row.get("cost_cny") or 0)
        item["missing_usage"] += int(bool(row.get("usage_missing")))
        item["unpriced"] += int(not row.get("priced", False))
    return {"run_id": run_id, "pid": pid, "scope": "当前进程步骤19，基于供应商返回用量；未标记调用单列", "phases": phases}
