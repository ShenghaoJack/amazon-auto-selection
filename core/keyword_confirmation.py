"""持久化步骤 8 的关键词确认状态。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from core.paths import STEP_DIR_NAMES
from steps.ids import MONITOR_STEP


def path(root):
    return Path(root) / STEP_DIR_NAMES[MONITOR_STEP] / "keyword-confirmation.json"


def read(root):
    p = path(root)
    if not p.exists():
        return None
    # 损坏时不能悄悄放行。
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("status") not in ("pending", "confirmed"):
            raise ValueError("确认状态格式无效")
        return value
    except (OSError, ValueError) as exc:
        raise ValueError(f"关键词确认状态无法读取: {exc}") from exc


def write(root, value):
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                         prefix=p.name + ".", suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            json.dump(value, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def current(cat, data_root):
    # 与页面、步骤 9 共用生效词解析，避免比较自动候选池而非实际清单。
    from webui.server import keyword_lists_panel
    panel = keyword_lists_panel(cat, str(data_root))
    if not panel.get("ok"):
        raise ValueError(panel.get("error") or "无法读取关键词清单")
    tail = panel["longtail"]
    return {"core": panel["core"]["effective"],
            "longtail": tail["manual"] or tail["crawl"],
            "mode": panel["mode"], "stopped": bool(panel["stopped"])}


def revision(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def missing_metrics(root, cat, words):
    """已加入核心清单但尚未进入步骤 5 产物的词。"""
    p = Path(root) / f"{cat}-amazon-keyword-data.json"
    try:
        rows = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError("关键词指标格式无效")
    except (OSError, ValueError):
        return list(words)
    known = {str(row.get("keyword") or "").strip().casefold()
             for row in rows if isinstance(row, dict) and row.get("status") != "error"}
    return [word for word in words if str(word).strip().casefold() not in known]


def report_outdated(root, cat):
    """清单保存晚于报告时，确认后需刷新步骤 8。"""
    root = Path(root)
    report = root / f"{cat}-keyword-report.xlsx"
    if not report.is_file():
        return True
    timestamp = report.stat().st_mtime_ns
    names = (f"filter-{cat}-core-words.txt", f"filter-{cat}-longtail-words.txt")
    return any(p.is_file() and p.stat().st_mtime_ns > timestamp for p in (root / n for n in names))


def pending(root, baseline):
    value = {"status": "pending", "baseline": baseline,
             "created_at": datetime.now().isoformat(timespec="seconds")}
    write(root, value)
    return value
