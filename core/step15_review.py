"""Version-bound human review of the Step 15 workbook."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from openpyxl import load_workbook
from tools.attr_stability.spec_io import write_json_atomic


def marker(root): return Path(root) / "15_选品报告确认.json"
def report_path(root):
    root = Path(root)
    return root / (root.name + "-初步类目分析报告.xlsx")
def revision(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()
def mark_pending(root, settings_revision=None):
    from core.step15_settings import load, digest as settings_digest
    from tools.step15_market_assessment import ASSESSMENT_VERSION
    digest = revision(report_path(root))
    current = settings_digest(load(root))
    if settings_revision is not None and current != settings_revision:
        raise ValueError("选品标准在生成报告期间发生变化，请重新运行步骤15")
    write_json_atomic(marker(root), {"status": "pending", "revision": digest, "settings_revision": current, "assessment_version": ASSESSMENT_VERSION})
def settings_stale(root):
    from core.step15_settings import defaults, load, digest
    if not marker(root).exists(): return False
    try:
        data = json.loads(marker(root).read_text(encoding="utf-8-sig"))
        return data.get("settings_revision", digest(defaults())) != digest(load(root))
    except (OSError, ValueError, TypeError): return True
def decision_status(root):
    """Return current version-bound human decision: 'confirmed', 'rejected', or 'pending'."""
    path = marker(root)
    if not path.exists():
        return "pending"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("revision") != revision(report_path(root)) or settings_stale(root):
            return "pending"
        status = str(data.get("status") or "").strip().lower()
        if status in ("confirmed", "rejected"):
            return status
        return "pending"
    except (OSError, ValueError, TypeError):
        return "pending"
def rejected(root):
    return decision_status(root) == "rejected"
def needs_review(root):
    return decision_status(root) == "pending"
def pending(root):
    return decision_status(root) != "confirmed"


def current_report_rows(rows):
    """Recognize the complete current indicator schema, not just an old workbook."""
    from tools.step15_market_assessment import SPECS
    expected = {(section, name) for section, name, *_ in SPECS}
    actual = [(r.get("板块"), r.get("指标")) for r in rows]
    return len(actual) == len(expected) and set(actual) == expected and all("影响" not in r for r in rows)


def assessment_version_current(root):
    from tools.step15_market_assessment import ASSESSMENT_VERSION
    try:
        state = json.loads(marker(root).read_text(encoding="utf-8-sig"))
        return state.get("assessment_version") == ASSESSMENT_VERSION
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def decision_meta(root):
    """Return current decision metadata (status, decided_at, decided_by, history)."""
    status = decision_status(root)
    out = {"status": status, "decided_at": "", "decided_by": "", "history": []}
    path = marker(root)
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        hist = data.get("history")
        if isinstance(hist, list):
            out["history"] = [h for h in hist if isinstance(h, dict)]
        if status in ("confirmed", "rejected"):
            out["decided_at"] = str(data.get("decided_at") or "")
            out["decided_by"] = str(data.get("decided_by") or "")
    except (OSError, ValueError, TypeError):
        pass
    return out


def view(root):
    from core.step15_display import format_indicator_text, format_text
    from core.step15_trend_detail import season_google_trend_detail, trend_detail
    from tools.step15_indicator_assessment import BUSINESS_MEANINGS, summarize_four_dimensions
    path = report_path(root)
    before = revision(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if not all(s in wb for s in ("选品指标判定", "AI市场建议")):
            raise ValueError("当前报告为旧版或不完整，缺少指标或AI建议；请先单独重新运行步骤15")
        def rows(sheet):
            values = wb[sheet].iter_rows(values_only=True)
            headers = next(values, ())
            return [{str(k): (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                     for k, v in zip(headers, row) if k is not None} for row in values]
        result = {"指标": [{**(item := format_indicator_text(r)), "业务定义": BUSINESS_MEANINGS.get(item.get("指标"), "暂无业务定义")}
                          for r in rows("选品指标判定")],
                  "建议": [{**r, "内容": format_text(r.get("内容"))} for r in rows("AI市场建议")]}
    finally: wb.close()
    if before != revision(path): raise ValueError("报告读取时发生变化，请刷新")
    result["趋势明细"] = {r["指标"]: detail for r in result["指标"]
                          if r.get("指标") in ("趋势方向（站内）", "趋势方向（Google）")
                          if (detail := trend_detail(r)) is not None}
    result["四维度拆解"] = summarize_four_dimensions(result["指标"])
    season_google = season_google_trend_detail(root, result["指标"])
    if season_google is not None:
        result["季节Google趋势"] = season_google
    meta = decision_meta(root)
    decision = meta["status"]
    result["revision"] = before
    result["decision"] = decision
    result["rejected"] = decision == "rejected"
    result["decided_at"] = meta["decided_at"]
    result["decided_by"] = meta["decided_by"]
    result["decision_history"] = meta["history"]
    result["pending"] = decision != "confirmed"
    result["settings_stale"] = settings_stale(root)
    result["report_outdated"] = not current_report_rows(result["指标"]) or not assessment_version_current(root)
    return result

def confirm(root, expected, actor=""):
    if not marker(root).is_file(): raise ValueError("当前没有待确认的步骤15报告")
    from core.step15_settings import defaults, digest
    from tools.step15_market_assessment import ASSESSMENT_VERSION
    state = json.loads(marker(root).read_text(encoding="utf-8-sig"))
    reviewed_settings = state.get("settings_revision", digest(defaults()))
    if settings_stale(root): raise ValueError("选品标准已更改，请重新运行步骤15后再确认")
    if not isinstance(expected, str) or len(expected) != 64 or expected != revision(report_path(root)):
        raise ValueError("报告已变化，请刷新后重新确认")
    current = view(root)
    if current["revision"] != expected:
        raise ValueError("报告已变化，请刷新后重新确认")
    if current["report_outdated"]:
        raise ValueError("当前为旧版指标报告，请重新运行步骤15后再确认")
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who = str(actor or "").strip()
    history = [h for h in (state.get("history") or []) if isinstance(h, dict)]
    history.append({"status": "confirmed", "action": "继续深挖该类目", "decided_at": now_ts, "decided_by": who, "revision": expected})
    write_json_atomic(marker(root), {
        "status": "confirmed",
        "revision": expected,
        "settings_revision": reviewed_settings,
        "assessment_version": ASSESSMENT_VERSION,
        "decided_at": now_ts,
        "decided_by": who,
        "history": history[-20:],
    })
    if expected != revision(report_path(root)) or settings_stale(root):
        write_json_atomic(marker(root), {"status": "pending", "revision": expected, "settings_revision": reviewed_settings, "assessment_version": ASSESSMENT_VERSION})
        raise ValueError("确认期间报告或选品标准发生变化，请刷新后重新确认")


def reject(root, expected, reason="", actor=""):
    """人工否决步骤15报告（不推荐进入该类目），阻断后续深挖步骤。"""
    if not marker(root).is_file(): raise ValueError("当前没有可否决的步骤15报告")
    from core.step15_settings import defaults, digest
    from tools.step15_market_assessment import ASSESSMENT_VERSION
    state = json.loads(marker(root).read_text(encoding="utf-8-sig"))
    reviewed_settings = state.get("settings_revision", digest(defaults()))
    if settings_stale(root): raise ValueError("选品标准已更改，请重新运行步骤15后再操作")
    if not isinstance(expected, str) or len(expected) != 64 or expected != revision(report_path(root)):
        raise ValueError("报告已变化，请刷新后重新操作")
    current = view(root)
    if current["revision"] != expected:
        raise ValueError("报告已变化，请刷新后重新操作")
    if current["report_outdated"]:
        raise ValueError("当前为旧版指标报告，请重新运行步骤15后再操作")
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who = str(actor or "").strip()
    history = [h for h in (state.get("history") or []) if isinstance(h, dict)]
    entry = {"status": "rejected", "action": "否决（不推荐进入）", "decided_at": now_ts, "decided_by": who, "revision": expected}
    if str(reason or "").strip():
        entry["reason"] = str(reason).strip()
    history.append(entry)
    payload = {
        "status": "rejected",
        "revision": expected,
        "settings_revision": reviewed_settings,
        "assessment_version": ASSESSMENT_VERSION,
        "decided_at": now_ts,
        "decided_by": who,
        "history": history[-20:],
    }
    if str(reason or "").strip():
        payload["reason"] = str(reason).strip()
    write_json_atomic(marker(root), payload)
    if expected != revision(report_path(root)) or settings_stale(root):
        write_json_atomic(marker(root), {"status": "pending", "revision": expected, "settings_revision": reviewed_settings, "assessment_version": ASSESSMENT_VERSION})
        raise ValueError("操作期间报告或选品标准发生变化，请刷新后重试")
