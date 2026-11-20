"""Version-bound human review of the Step 15 workbook."""
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
def pending(root):
    path = marker(root)
    if not path.exists(): return False  # Older categories are not retroactively blocked.
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("status") != "confirmed" or data.get("revision") != revision(report_path(root)) or settings_stale(root)
    except (OSError, ValueError, TypeError): return True


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


def view(root):
    from core.step15_display import format_indicator_text, format_text
    from core.step15_trend_detail import trend_detail
    from tools.step15_indicator_assessment import BUSINESS_MEANINGS
    path = report_path(root)
    before = revision(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if not all(s in wb for s in ("选品指标判定", "AI市场建议")):
            raise ValueError("报告缺少指标或建议，请重新运行步骤15")
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
    result["revision"] = before
    result["pending"] = pending(root)
    result["settings_stale"] = settings_stale(root)
    result["report_outdated"] = not current_report_rows(result["指标"]) or not assessment_version_current(root)
    return result

def confirm(root, expected):
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
    write_json_atomic(marker(root), {"status": "confirmed", "revision": expected, "settings_revision": reviewed_settings, "assessment_version": ASSESSMENT_VERSION})
    if expected != revision(report_path(root)) or settings_stale(root):
        write_json_atomic(marker(root), {"status": "pending", "revision": expected, "settings_revision": reviewed_settings, "assessment_version": ASSESSMENT_VERSION})
        raise ValueError("确认期间报告或选品标准发生变化，请刷新后重新确认")
