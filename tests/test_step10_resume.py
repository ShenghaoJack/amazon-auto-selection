# 单元测试: 步骤10 子变体抓取的断点续爬台账口径(2026-09-14)
# ---------------------------------------------------------------------------
# 用户报障: "步骤10 中子变体抓取没有断点重跑吗, 为什么 3000 多个子变体重新跑了"
# 实测真相(2026-09-14 Grilling): 断点续爬**是好的** —— 上一轮抓了 3410 个子变体(13:04→18:35 中断),
#   本轮 `_fetch_round` 按台账剔掉已 ok 的, 只补 98 个, 且本轮抓的 50 条与上一轮 3410 条**零重复**。
# 但日志那行数字是错的: `_fetch_variant_texts` 只从**首轮请求台账**(results_path)算 existing_ok,
#   而子变体记录写在 `<CAT>-variant-texts.jsonl` → 日志报"已有自身记录 238 / 本轮待抓 3510"
#   (真实: 已有 3410+ / 待抓 98), 用户据此以为重跑。现两份台账都读。
import json
from pathlib import Path

from steps.step10_variants import Step10Variants


def _step(cat: str = "Cat") -> Step10Variants:
    """构造带 _cat 的步骤实例(框架会注入 _cat, 单测里手工补上)。"""
    s = Step10Variants()
    s._cat = cat
    return s


class _Logger:
    """step 的 `self.log(ctx, msg)` 内部调 `ctx.log.info(...)` —— 单测里收集消息。"""

    def __init__(self, sink: list):
        self._sink = sink

    def info(self, msg, *a, **k):
        self._sink.append(str(msg))

    def warning(self, msg, *a, **k):
        self._sink.append(str(msg))

    def error(self, msg, *a, **k):
        self._sink.append(str(msg))


class _Ctx:
    """最小 StepContext 替身: 只要 config/resolve_artifact/dry_run/log。"""

    def __init__(self, root: Path, logs: list, cap: str = "0"):
        self._root = root
        self._logs = logs
        self.dry_run = True
        self.config = {"VARIANT_TEXT_CAP": cap}
        self.log = _Logger(logs)

    def resolve_artifact(self, name: str) -> str:
        return str(self._root / name)


def _write_jsonl(path: Path, recs: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def _rec(asin: str, children: list, ok: bool = True) -> dict:
    return {"request_asin": asin, "ok": ok, "status": "ok" if ok else "failed",
            "variant_asins": [asin] + list(children)}


def test_already_fetched_children_are_skipped_via_variant_texts_ledger(tmp_path, monkeypatch):
    """核心回归: 子变体台账里已 ok 的子变体不得再抓(want 里不能出现)。"""
    results = tmp_path / "Cat-results.jsonl"            # 首轮请求台账(1209 个收集 ASIN 的角色)
    vtexts = tmp_path / "Cat-variant-texts.jsonl"       # 子变体台账(真正的断点续爬依据)
    _write_jsonl(results, [_rec("B0500CF105", ["B002534720", "B0BB650BBF", "B0B237E153"])])
    _write_jsonl(vtexts, [_rec("B002534720", ["B002534720"]),
                          _rec("B0BB650BBF", ["B0BB650BBF"])])

    ctx = _Ctx(tmp_path, [])
    want_calls = {}

    def fake_fetch_all(c, asins, path):
        want_calls["asins"] = list(asins)
        want_calls["path"] = path
        return []

    monkeypatch.setattr(Step10Variants, "_fetch_all", lambda self, c, a, p: fake_fetch_all(c, a, p))
    step = _step()
    step._fetch_variant_texts(ctx, [_rec("B0500CF105", ["B002534720", "B0BB650BBF", "B0B237E153"])],
                              str(results))

    assert want_calls["asins"] == ["B0B237E153"], "已抓过的子变体 1/2 必须跳过, 只补 3"
    assert want_calls["path"].endswith("Cat-variant-texts.jsonl"), "补抓仍写子变体台账"
    log = " ".join(ctx._logs)
    assert "发现子变体 3 个" in log
    assert "已有自身记录 2 个" in log, f"数字要反映真实台账(原实现只数首轮台账): {log}"
    assert "本轮待抓 1 个" in log


def test_log_numbers_reflect_both_ledgers(tmp_path, monkeypatch):
    """原实现只读首轮台账 → 数字虚报; 现两份台账合并计数。"""
    results = tmp_path / "Cat-results.jsonl"
    vtexts = tmp_path / "Cat-variant-texts.jsonl"
    _write_jsonl(results, [_rec("B0500CF105", ["B002534720", "B0BB650BBF"])])
    # 首轮台账里 B0500CF105 自己不是子变体 → 原实现会报"已有 0 / 待抓 2"
    _write_jsonl(vtexts, [_rec("B002534720", ["B002534720"])])

    ctx = _Ctx(tmp_path, [])
    monkeypatch.setattr(Step10Variants, "_fetch_all", lambda self, c, a, p: [])
    _step()._fetch_variant_texts(
        ctx, [_rec("B0500CF105", ["B002534720", "B0BB650BBF"])], str(results))
    log = " ".join(ctx._logs)
    assert "已有自身记录 1 个" in log and "本轮待抓 1 个" in log, log


def test_failed_children_are_retried(tmp_path, monkeypatch):
    """子变体台账里失败(ok=false)的仍要重试(不能当成已完成)。"""
    results = tmp_path / "Cat-results.jsonl"
    vtexts = tmp_path / "Cat-variant-texts.jsonl"
    _write_jsonl(results, [_rec("B0500CF105", ["B002534720", "B0BB650BBF"])])
    _write_jsonl(vtexts, [{"request_asin": "B002534720", "ok": False, "status": "blocked",
                           "variant_asins": ["B002534720"]}])

    ctx = _Ctx(tmp_path, [])
    seen = {}
    monkeypatch.setattr(Step10Variants, "_fetch_all",
                        lambda self, c, a, p: seen.update({"a": list(a)}) or [])
    _step()._fetch_variant_texts(
        ctx, [_rec("B0500CF105", ["B002534720", "B0BB650BBF"])], str(results))
    assert seen["a"] == ["B002534720", "B0BB650BBF"], "失败过的子变体要重试"


def test_first_pass_requests_count_as_existing(tmp_path, monkeypatch):
    """既在首轮台账 ok、又是子变体的 ASIN 不该重抓(两份台账求并集)。"""
    results = tmp_path / "Cat-results.jsonl"
    vtexts = tmp_path / "Cat-variant-texts.jsonl"
    _write_jsonl(results, [_rec("B0500CF105", ["B02230DC41"]), _rec("B02230DC41", ["B0500CF105"])])
    _write_jsonl(vtexts, [])
    ctx = _Ctx(tmp_path, [])
    seen = {}
    monkeypatch.setattr(Step10Variants, "_fetch_all",
                        lambda self, c, a, p: seen.update({"a": list(a)}) or [])
    _step()._fetch_variant_texts(ctx, [_rec("B0500CF105", ["B02230DC41"])], str(results))
    assert seen.get("a", []) == [], "B02230DC41 已有自身 ok 记录, 不应再抓"
